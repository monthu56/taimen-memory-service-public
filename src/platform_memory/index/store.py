"""VectorIndex — таблица чанков в pgvector + гибридный поиск (вектор + русский FTS).

Мультитенантность (ADR-054): каждый чанк принадлежит ровно одному ``namespace``;
уникальность — ``(namespace, node_key, chunk_order)``; поиск фильтрует по списку
namespaces реальным WHERE-предикатом. Колонка ``meta jsonb`` хранит фильтруемые
теги потребителя (например, ``{"collection": "licenses"}``) с containment-поиском.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.types.json import Jsonb

from platform_memory.core.models import Chunk
from platform_memory.core.scopes import chunk_visibility_sql
from platform_memory.core.namespaces import resolve_namespace, resolve_namespaces

# Документ для полнотекстового поиска: имя сущности + секция + текст (русская конфигурация).
_FTS_DOC = (
    "to_tsvector('russian', coalesce(title,'') || ' ' || coalesce(heading,'') || ' ' || text)"
)
# OR-запрос по стеммированным лексемам вопроса (стоп-слова отбрасываются автоматически).
_FTS_QUERY = (
    "to_tsquery('russian', array_to_string(tsvector_to_array(to_tsvector('russian', %s)), ' | '))"
)


@dataclass(slots=True)
class SearchHit:
    """Найденный фрагмент текста с метаданными и итоговой релевантностью."""

    chunk_id: int
    node_key: str
    source_path: str
    title: str
    heading: str
    text: str
    score: float
    namespace: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    rerank_score: float | None = (
        None  # семантическая релевантность 0..1 (ADR-005); None — не реранкован
    )


class VectorIndex:
    """Хранилище и поиск текстовых фрагментов с эмбеддингами."""

    def __init__(self, conn: psycopg.Connection, table: str, dim: int, default_namespace: str = ""):
        """Инициализировать индекс на соединении, таблице, размерности и default-namespace."""
        self.conn = conn
        self.table = table
        self.dim = dim
        self.default_namespace = resolve_namespace(default_namespace)
        register_vector(conn)

    @property
    def _tbl(self) -> sql.Identifier:
        return sql.Identifier("public", self.table)

    # --- схема ---

    def ensure_schema(self) -> None:
        """Создать таблицу чанков и индексы; идемпотентно домигрировать старую схему.

        Эволюция pre-namespace таблиц: добавление колонок ``namespace``/``meta``
        (существующие строки получают default-namespace), замена UNIQUE-констрейнта
        ``(node_key, chunk_order)`` на индекс ``(namespace, node_key, chunk_order)``.
        Повторный запуск безопасен.
        """
        self._check_dim()
        ns_default = sql.Literal(self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {tbl} (
                        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        node_key    text NOT NULL,
                        source_path text NOT NULL,
                        title       text NOT NULL DEFAULT '',
                        heading     text NOT NULL DEFAULT '',
                        chunk_order int  NOT NULL DEFAULT 0,
                        text        text NOT NULL,
                        embedding   vector({dim}) NOT NULL,
                        seen_run    text NOT NULL DEFAULT '',
                        namespace   text NOT NULL DEFAULT {ns},
                        meta        jsonb NOT NULL DEFAULT '{{}}'::jsonb
                    )
                    """
                ).format(tbl=self._tbl, dim=sql.Literal(self.dim), ns=ns_default)
            )
            # Эволюция существующей (pre-namespace) таблицы: backfill через DEFAULT.
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS namespace text NOT NULL "
                    "DEFAULT {ns}"
                ).format(tbl=self._tbl, ns=ns_default)
            )
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL "
                    "DEFAULT '{{}}'::jsonb"
                ).format(tbl=self._tbl)
            )
            cur.execute(
                sql.SQL("ALTER TABLE {tbl} DROP CONSTRAINT IF EXISTS {old_unique}").format(
                    tbl=self._tbl,
                    old_unique=sql.Identifier(f"{self.table}_node_key_chunk_order_key"),
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE UNIQUE INDEX IF NOT EXISTS {ix} ON {tbl} "
                    "(namespace, node_key, chunk_order)"
                ).format(ix=sql.Identifier(f"{self.table}_ns_node_order_key"), tbl=self._tbl)
            )
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {ix} ON {tbl} (namespace)").format(
                    ix=sql.Identifier(f"{self.table}_ns"), tbl=self._tbl
                )
            )
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {ix} ON {tbl} USING gin (meta)").format(
                    ix=sql.Identifier(f"{self.table}_meta_gin"), tbl=self._tbl
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {ix} ON {tbl} "
                    "USING hnsw (embedding vector_cosine_ops)"
                ).format(ix=sql.Identifier(f"{self.table}_emb_hnsw"), tbl=self._tbl)
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {ix} ON {tbl} USING gin (" + _FTS_DOC + ")"
                ).format(ix=sql.Identifier(f"{self.table}_fts"), tbl=self._tbl)
            )
        self._ensure_trgm()

    def _ensure_trgm(self) -> None:
        """Поставить pg_trgm и trigram-индекс для identifier-поиска (ADR-016 §15).

        Без прав на CREATE EXTENSION индекс не создаётся — lexical_search
        продолжает работать через последовательный ILIKE (медленнее, корректно).
        """
        try:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                    cur.execute(
                        sql.SQL(
                            "CREATE INDEX IF NOT EXISTS {ix} ON {tbl} USING gin (text gin_trgm_ops)"
                        ).format(ix=sql.Identifier(f"{self.table}_text_trgm"), tbl=self._tbl)
                    )
        except psycopg.errors.InsufficientPrivilege:
            pass

    def _check_dim(self) -> None:
        """Если таблица уже есть с другой размерностью — понятная ошибка (нужен --reset)."""
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
                f"{self.dim}. Запустите `cb ingest --reset` или поменяйте CB_EMBEDDING_DIM."
            )

    def reset(self) -> None:
        """Полностью удалить таблицу чанков."""
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {tbl}").format(tbl=self._tbl))

    # --- запись ---

    def delete_for_node(self, node_key: str, namespace: str = "") -> int:
        """Удалить все чанки узла node_key в пределах одного namespace; вернуть их число."""
        ns = resolve_namespace(namespace, self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {tbl} WHERE node_key = %s AND namespace = %s").format(
                    tbl=self._tbl
                ),
                (node_key, ns),
            )
            return cur.rowcount

    def delete_by_meta(self, meta_filter: dict[str, Any], namespace: str = "") -> int:
        """Удалить чанки по containment-фильтру meta (например, по observation_id).

        Используется deletion semantics наблюдений (ADR-016 §8): чанки, порождённые
        text-assertion удаляемого наблюдения, находятся по ``meta.observation_id``.
        """
        if not meta_filter:
            raise ValueError("Пустой meta_filter удалил бы все чанки namespace")
        ns = resolve_namespace(namespace, self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {tbl} WHERE namespace = %s AND meta @> %s::jsonb").format(
                    tbl=self._tbl
                ),
                (ns, json.dumps(meta_filter, ensure_ascii=False)),
            )
            return cur.rowcount

    def count_for_node(self, node_key: str, namespace: str = "") -> int:
        """Число чанков узла node_key в пределах одного namespace."""
        ns = resolve_namespace(namespace, self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT count(*) FROM {tbl} WHERE node_key = %s AND namespace = %s").format(
                    tbl=self._tbl
                ),
                (node_key, ns),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def texts_for_node(self, node_key: str, namespace: str = "") -> list[str]:
        """Тексты чанков узла по порядку chunk_order — для восстановления полного текста."""
        ns = resolve_namespace(namespace, self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT text FROM {tbl} WHERE node_key = %s AND namespace = %s "
                    "ORDER BY chunk_order"
                ).format(tbl=self._tbl),
                (node_key, ns),
            )
            return [row[0] for row in cur.fetchall()]

    def add_chunks(
        self, chunks: list[Chunk], embeddings: list[list[float]], seen_run: str = ""
    ) -> int:
        """Вставить или обновить чанки с эмбеддингами; вернуть число записанных строк.

        Namespace берётся из чанка (пустой -> default стора); конфликт разрешается
        по ``(namespace, node_key, chunk_order)``.
        """
        if not chunks:
            return 0
        if len(chunks) != len(embeddings):
            raise ValueError("Число чанков и эмбеддингов не совпадает")
        rows = [
            (
                c.node_key,
                c.source_path,
                c.title,
                c.heading,
                c.order,
                c.text,
                emb,
                seen_run,
                resolve_namespace(c.namespace, self.default_namespace),
                Jsonb(c.meta or {}),
            )
            for c, emb in zip(chunks, embeddings, strict=True)
        ]
        with self.conn.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    "INSERT INTO {tbl} "
                    "(node_key, source_path, title, heading, chunk_order, text, embedding, "
                    "seen_run, namespace, meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (namespace, node_key, chunk_order) DO UPDATE SET "
                    "source_path=EXCLUDED.source_path, title=EXCLUDED.title, "
                    "heading=EXCLUDED.heading, text=EXCLUDED.text, embedding=EXCLUDED.embedding, "
                    "seen_run=EXCLUDED.seen_run, meta=EXCLUDED.meta"
                ).format(tbl=self._tbl),
                rows,
            )
        return len(rows)

    def prune_chunks(self, seen_run: str, namespace: str = "") -> int:
        """Удалить чанки namespace, не виденные в текущем прогоне ingest.

        Скоупится одним namespace — ре-ingest vault никогда не подметает чанки других
        namespaces (тенантские данные). Чанки с пустым ``seen_run`` — write-back агентов
        (retain мимо ingest): переживают ре-ingest и не подметаются.
        """
        ns = resolve_namespace(namespace, self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "DELETE FROM {tbl} WHERE seen_run <> %s AND seen_run <> '' AND namespace = %s"
                ).format(tbl=self._tbl),
                (seen_run, ns),
            )
            return cur.rowcount

    # --- поиск ---

    def search(
        self,
        query_embedding: list[float],
        query_text: str,
        k: int = 8,
        pool: int = 20,
        *,
        namespaces: Sequence[str] = (),
        meta_filter: dict[str, Any] | None = None,
        scopes: Sequence[str] = (),
        allowed_scopes: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        """Гибрид: вектор top-N + русский FTS top-N, слияние по reciprocal rank fusion.

        ``namespaces`` — области видимости (пусто -> default стора); ``meta_filter`` —
        jsonb-containment по колонке meta (например, ``{"collection": "licenses"}``);
        ``scopes`` — канонические "type:id" (ADR-016 §5): чанк проходит фильтр, если
        пересечение scopes непусто ЛИБО у чанка scopes нет вовсе (общая память KB).
        """
        self._check_dim()  # понятная ошибка вместо сырой 'different vector dimensions'
        nss = resolve_namespaces(namespaces, self.default_namespace)
        # Селективный фильтр поверх HNSW — post-filter: расширяем пул кандидатов.
        pool = max(pool, k * max(2, len(nss)))
        vec_rows = self._vector_search(
            query_embedding, pool, nss, meta_filter, scopes, allowed_scopes
        )
        fts_rows = (
            self._fts_search(query_text, pool, nss, meta_filter, scopes, allowed_scopes)
            if query_text.strip()
            else []
        )
        return self._rrf_merge(vec_rows, fts_rows, k)

    def lexical_search(
        self,
        query_text: str,
        identifiers: Sequence[str] = (),
        *,
        k: int = 8,
        namespaces: Sequence[str] = (),
        meta_filter: dict[str, Any] | None = None,
        scopes: Sequence[str] = (),
        allowed_scopes: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        """Lexical-канал без эмбеддингов: точные идентификаторы (ILIKE) + русский FTS.

        Identifier-совпадение (UUID, SHA, код ошибки, имя файла) ранжируется выше
        стемминговых совпадений — вектор и FTS такие токены теряют (ADR-016 §15).
        """
        nss = resolve_namespaces(namespaces, self.default_namespace)
        clause, extra = self._filters_sql(meta_filter, scopes, allowed_scopes)
        found: dict[int, SearchHit] = {}
        if identifiers:
            like_clause = " OR ".join(["text ILIKE %s"] * len(identifiers))
            like_params = [f"%{token}%" for token in identifiers]
            with self.conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "SELECT id, node_key, source_path, title, heading, text, "
                        "1.0 AS score, namespace, meta "
                        "FROM {tbl} WHERE (" + like_clause + ") AND " + clause + " LIMIT %s"
                    ).format(tbl=self._tbl),
                    (*like_params, nss, *extra, int(k)),
                )
                for hit in self._hit_rows(cur.fetchall()):
                    found[hit.chunk_id] = hit
        if query_text.strip():
            for hit in self._fts_search(
                query_text, int(k), nss, meta_filter, scopes, allowed_scopes
            ):
                found.setdefault(hit.chunk_id, hit)
        ordered = sorted(found.values(), key=lambda h: h.score, reverse=True)
        return ordered[: int(k)]

    def similarities(
        self, query_embedding: list[float], chunk_ids: Sequence[int]
    ) -> dict[int, float]:
        """Косинусная близость запроса к чанкам ``chunk_ids`` (0..1): id -> близость.

        Оценка ``search`` — ранг RRF, а не близость; смысловому якорю типизированного
        обхода нужна сопоставимая с индексом сущностей величина (MEM-ADR-020).
        """
        ids = sorted({int(i) for i in chunk_ids})
        if not ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT id, 1 - (embedding <=> %s::vector) FROM {tbl} WHERE id = ANY(%s)"
                ).format(tbl=self._tbl),
                (query_embedding, ids),
            )
            return {int(r[0]): max(0.0, min(1.0, float(r[1]))) for r in cur.fetchall()}

    @staticmethod
    def _filters_sql(
        meta_filter: dict[str, Any] | None,
        scopes: Sequence[str] = (),
        allowed_scopes: Sequence[str] | None = None,
    ) -> tuple[str, list[Any]]:
        """WHERE-предикаты изоляции: namespace всегда, meta/scopes/видимость — опционально."""
        clause = "namespace = ANY(%s)"
        params: list[Any] = []
        if meta_filter:
            clause += " AND meta @> %s::jsonb"
            params.append(json.dumps(meta_filter, ensure_ascii=False))
        if scopes:
            # Пересечение scopes непусто ИЛИ чанк без scopes (общая память KB).
            clause += " AND (NOT meta ? 'scopes' OR meta->'scopes' ?| %s)"
            params.append(list(scopes))
        if allowed_scopes is not None:
            # Видимость по principal (MEM-ADR-019): чанк с scope воркспейса/principal
            # виден только при пересечении с разрешёнными.
            clause += " AND " + chunk_visibility_sql()
            params.append(list(allowed_scopes))
        return clause, params

    def _hit_rows(self, rows: list[tuple]) -> list[SearchHit]:
        return [
            SearchHit(
                chunk_id=r[0],
                node_key=r[1],
                source_path=r[2],
                title=r[3],
                heading=r[4],
                text=r[5],
                score=r[6],
                namespace=r[7],
                meta=r[8] if isinstance(r[8], dict) else {},
            )
            for r in rows
        ]

    def _vector_search(
        self,
        query_embedding: list[float],
        limit: int,
        namespaces: list[str],
        meta_filter: dict[str, Any] | None,
        scopes: Sequence[str] = (),
        allowed_scopes: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        clause, extra = self._filters_sql(meta_filter, scopes, allowed_scopes)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT id, node_key, source_path, title, heading, text, "
                    "1 - (embedding <=> %s::vector) AS score, namespace, meta "
                    "FROM {tbl} WHERE " + clause + " "
                    "ORDER BY embedding <=> %s::vector LIMIT %s"
                ).format(tbl=self._tbl),
                (query_embedding, namespaces, *extra, query_embedding, limit),
            )
            return self._hit_rows(cur.fetchall())

    def _fts_search(
        self,
        query_text: str,
        limit: int,
        namespaces: list[str],
        meta_filter: dict[str, Any] | None,
        scopes: Sequence[str] = (),
        allowed_scopes: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        clause, extra = self._filters_sql(meta_filter, scopes, allowed_scopes)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT id, node_key, source_path, title, heading, text, "
                    "ts_rank(" + _FTS_DOC + ", q) AS score, namespace, meta "
                    "FROM {tbl}, " + _FTS_QUERY + " q "
                    "WHERE " + _FTS_DOC + " @@ q AND " + clause + " "
                    "ORDER BY score DESC LIMIT %s"
                ).format(tbl=self._tbl),
                (query_text, namespaces, *extra, limit),
            )
            return self._hit_rows(cur.fetchall())

    @staticmethod
    def _rrf_merge(
        vec: list[SearchHit], fts: list[SearchHit], k: int, rrf_k: int = 60
    ) -> list[SearchHit]:
        scores: dict[int, float] = {}
        hits: dict[int, SearchHit] = {}
        for ranked in (vec, fts):
            for rank, hit in enumerate(ranked):
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (rrf_k + rank + 1)
                hits.setdefault(hit.chunk_id, hit)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out: list[SearchHit] = []
        for chunk_id, score in ordered[:k]:
            hit = hits[chunk_id]
            out.append(
                SearchHit(
                    chunk_id=hit.chunk_id,
                    node_key=hit.node_key,
                    source_path=hit.source_path,
                    title=hit.title,
                    heading=hit.heading,
                    text=hit.text,
                    score=round(score, 6),
                    namespace=hit.namespace,
                    meta=hit.meta,
                )
            )
        return out

    # --- статистика ---

    def count_chunks(self, namespaces: Sequence[str] = ()) -> int:
        """Вернуть число чанков (опционально в пределах списка namespaces)."""
        with self.conn.cursor() as cur:
            if namespaces:
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {tbl} WHERE namespace = ANY(%s)").format(
                        tbl=self._tbl
                    ),
                    (list(namespaces),),
                )
            else:
                cur.execute(sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=self._tbl))
            return int(cur.fetchone()[0])

    def table_exists(self) -> bool:
        """Проверить, существует ли таблица чанков."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{self.table}",))
            return cur.fetchone()[0] is not None
