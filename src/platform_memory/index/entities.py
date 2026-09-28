"""EntityIndex — эмбеддинги сущностей видов с ``searchable`` (pgvector).

MEM-ADR-020, амендмент 2026-09-28, п.2 (поиск по смыслу по сущностям). Строка —
открытая версия сущности индексируемого вида в namespace: ``(namespace, kind,
natural_key)``, текст (``KindSpec.search_text``) и его хэш, вектор той же размерности
``CB_EMBEDDING_DIM``, что у чанков, scopes видимости и цитата версии (``source_path``,
снимок). Индекс держит только **текущее** состояние: наполняет и чистит его сверка
(``domain/searchable.py``), состояние на ``as_of`` проверяет вызывающий.

Поиск — только вектор (без FTS): сущность находится по смыслу описания, а не по
совпадению слов; точные ключи и псевдонимы разрешает журнал сверки.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.types.json import Jsonb

from platform_memory.core.namespaces import resolve_namespaces
from platform_memory.core.scopes import observation_visibility_sql

_COLUMNS = (
    "namespace, kind, natural_key, title, text, text_hash, source_path, source, scope, "
    "snapshot_id, valid_from, scopes"
)


@dataclass(slots=True)
class EntityRow:
    """Строка индекса без вектора: что индексировано и откуда (цитата версии)."""

    namespace: str
    kind: str
    natural_key: str
    title: str
    text: str
    text_hash: str
    source_path: str = ""
    source: str = ""
    scope: str = ""
    snapshot_id: str = ""
    valid_from: str = ""
    scopes: list[str] = field(default_factory=list)

    @property
    def ident(self) -> tuple[str, str]:
        return (self.kind, self.natural_key)

    def values(self) -> tuple:
        return (
            self.namespace,
            self.kind,
            self.natural_key,
            self.title,
            self.text,
            self.text_hash,
            self.source_path,
            self.source,
            self.scope,
            self.snapshot_id,
            self.valid_from,
            Jsonb(list(self.scopes)),
        )


@dataclass(slots=True)
class EntityHit:
    """Найденная сущность: строка индекса и косинусная близость 0..1."""

    row: EntityRow
    score: float


def _row(raw: Sequence[Any]) -> EntityRow:
    scopes = raw[11] if isinstance(raw[11], list) else []
    return EntityRow(*raw[:11], scopes=[str(s) for s in scopes])


class EntityIndex:
    """Таблица эмбеддингов сущностей: запись по ключу, удаление, векторный поиск."""

    def __init__(self, conn: psycopg.Connection, table: str, dim: int, default_namespace: str = ""):
        self.conn = conn
        self.table = table
        self.dim = dim
        self.default_namespace = default_namespace
        register_vector(conn)

    @property
    def _tbl(self) -> sql.Identifier:
        return sql.Identifier("public", self.table)

    def _ix(self, suffix: str) -> sql.Identifier:
        return sql.Identifier(f"{self.table}_{suffix}")

    # --- схема ---

    def ensure_schema(self) -> None:
        """Создать таблицу и индексы (идемпотентно; миграция индекса сущностей)."""
        self._check_dim()
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {tbl} (
                        namespace   text NOT NULL,
                        kind        text NOT NULL,
                        natural_key text NOT NULL,
                        title       text NOT NULL DEFAULT '',
                        text        text NOT NULL,
                        text_hash   text NOT NULL,
                        source_path text NOT NULL DEFAULT '',
                        source      text NOT NULL DEFAULT '',
                        scope       text NOT NULL DEFAULT '',
                        snapshot_id text NOT NULL DEFAULT '',
                        valid_from  text NOT NULL DEFAULT '',
                        scopes      jsonb NOT NULL DEFAULT '[]'::jsonb,
                        embedding   vector({dim}) NOT NULL,
                        updated_at  timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, kind, natural_key)
                    )
                    """
                ).format(tbl=self._tbl, dim=sql.Literal(self.dim))
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {ix} ON {tbl} "
                    "USING hnsw (embedding vector_cosine_ops)"
                ).format(ix=self._ix("emb_hnsw"), tbl=self._tbl)
            )

    def table_exists(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{self.table}",))
            return cur.fetchone()[0] is not None

    def _check_dim(self) -> None:
        """Таблица с другой размерностью — понятная ошибка вместо сырой ошибки pgvector."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.atttypmod
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = %s AND a.attname = 'embedding'
                """,
                (self.table,),
            )
            row = cur.fetchone()
        if row and row[0] not in (None, -1) and int(row[0]) != self.dim:
            raise RuntimeError(
                f"Таблица {self.table} создана с размерностью {row[0]}, а конфиг ждёт "
                f"{self.dim}. Удалите таблицу индекса сущностей и переиндексируйте "
                "namespace (PUT /api/memory/namespaces/{ns}/kinds) или верните CB_EMBEDDING_DIM."
            )

    # --- запись ---

    def rows(self, namespace: str, kinds: Iterable[str] | None = None) -> dict[tuple, EntityRow]:
        """Строки namespace (опц. только заданных видов): (вид, ключ) -> строка без вектора."""
        cond, params = "namespace = %s", [namespace]
        if kinds is not None:
            cond += " AND kind = ANY(%s)"
            params.append(sorted(set(kinds)))
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(f"SELECT {_COLUMNS} FROM {{tbl}} WHERE " + cond).format(tbl=self._tbl),
                params,
            )
            return {r.ident: r for r in (_row(raw) for raw in cur.fetchall())}

    def upsert(self, rows: Sequence[EntityRow], embeddings: Sequence[list[float]]) -> int:
        """Записать строки с векторами (конфликт — по ``(namespace, kind, natural_key)``)."""
        if len(rows) != len(embeddings):
            raise ValueError("Число строк и эмбеддингов не совпадает")
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    f"INSERT INTO {{tbl}} ({_COLUMNS}, embedding) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (namespace, kind, natural_key) DO UPDATE SET "
                    "title=EXCLUDED.title, text=EXCLUDED.text, text_hash=EXCLUDED.text_hash, "
                    "source_path=EXCLUDED.source_path, source=EXCLUDED.source, "
                    "scope=EXCLUDED.scope, snapshot_id=EXCLUDED.snapshot_id, "
                    "valid_from=EXCLUDED.valid_from, scopes=EXCLUDED.scopes, "
                    "embedding=EXCLUDED.embedding, updated_at=now()"
                ).format(tbl=self._tbl),
                [(*r.values(), emb) for r, emb in zip(rows, embeddings, strict=True)],
            )
        return len(rows)

    def update_meta(self, rows: Sequence[EntityRow]) -> int:
        """Обновить цитату и scopes строк, чей текст не изменился (вектор прежний)."""
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    "UPDATE {tbl} SET title=%s, source_path=%s, source=%s, scope=%s, "
                    "snapshot_id=%s, valid_from=%s, scopes=%s, updated_at=now() "
                    "WHERE namespace=%s AND kind=%s AND natural_key=%s"
                ).format(tbl=self._tbl),
                [
                    (
                        r.title,
                        r.source_path,
                        r.source,
                        r.scope,
                        r.snapshot_id,
                        r.valid_from,
                        Jsonb(list(r.scopes)),
                        r.namespace,
                        r.kind,
                        r.natural_key,
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def delete(self, namespace: str, idents: Iterable[tuple[str, str]]) -> int:
        """Снять сущности ``(вид, ключ)`` из индекса namespace; вернуть число строк."""
        idents = sorted(set(idents))
        if not idents:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "DELETE FROM {tbl} t USING unnest(%s::text[], %s::text[]) AS d(kind, key) "
                    "WHERE t.namespace = %s AND t.kind = d.kind AND t.natural_key = d.key"
                ).format(tbl=self._tbl),
                ([k for k, _ in idents], [key for _, key in idents], namespace),
            )
            return cur.rowcount

    # --- поиск ---

    def search(
        self,
        query_embedding: list[float],
        k: int = 5,
        *,
        namespaces: Sequence[str] = (),
        kinds: Iterable[str] | None = None,
        scopes: Sequence[str] = (),
        allowed_scopes: Sequence[str] | None = None,
    ) -> list[EntityHit]:
        """Ближайшие по косинусу сущности namespaces (опц. только заданных видов).

        ``scopes`` — запрошенные scopes (пересечение непусто или у сущности scopes нет);
        ``allowed_scopes`` — видимость по principal (MEM-ADR-019), как у журнала сверки.
        """
        self._check_dim()
        nss = resolve_namespaces(namespaces, self.default_namespace)
        cond, params = ["namespace = ANY(%s)"], [nss]
        if kinds is not None:
            cond.append("kind = ANY(%s)")
            params.append(sorted(set(kinds)))
        if scopes:
            cond.append("(scopes = '[]'::jsonb OR scopes ?| %s)")
            params.append(list(scopes))
        if allowed_scopes is not None:
            cond.append(observation_visibility_sql("scopes"))
            params.append(list(allowed_scopes))
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    f"SELECT {_COLUMNS}, 1 - (embedding <=> %s::vector) AS score FROM {{tbl}} "
                    "WHERE " + " AND ".join(cond) + " ORDER BY embedding <=> %s::vector LIMIT %s"
                ).format(tbl=self._tbl),
                (query_embedding, *params, query_embedding, int(k)),
            )
            rows = cur.fetchall()
        return [
            EntityHit(row=_row(r[:12]), score=round(max(0.0, min(1.0, float(r[12]))), 6))
            for r in rows
        ]


__all__ = ["EntityHit", "EntityIndex", "EntityRow"]
