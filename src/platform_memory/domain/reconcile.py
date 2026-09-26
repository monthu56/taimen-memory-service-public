"""reconcile(source, scope, snapshot) — сверка полного снимка источника с памятью (MEM-ADR-020).

Формат снимка — общий для любого доменного пакета (контракт производителя —
SNAPSHOT.md пакета, TAI-ADR-0042 п.2)::

    {"pack": "software-delivery@1", "source": "git:control-plane", "scope": "control-plane",
     "snapshotId": "control-plane@f602…", "observedAt": "2026-09-23T08:45:00+00:00",
     "entities": [{"kind": "endpoint", "key": "GET /api/v1/runs/{}", "title": "…",
                   "attributes": {…}, "provenance": {"repo": "…", "sha": "…", "path": "…",
                   "line": 199}}],
     "relations": [{"relation": "calls", "from": {"kind": "ui_call", "key": "…"},
                    "to": {"kind": "endpoint", "key": "…"}, "attributes": {…}}]}

Снимок — ПОЛНОЕ состояние одного ``(source, scope)`` на момент ``observedAt``. Сверка
с предыдущим состоянием того же ``(source, scope)`` в namespace:

* новое — открывается (узел/ребро с ``valid_from = observedAt``);
* изменившееся (``title``, ``attributes``, ``provenance`` — всё содержимое входит в
  хэш) — старая версия закрывается (``valid_to``) со ссылкой ``superseded_by`` на
  новую, новая открывается с ``supersedes``;
* пропавшее — закрывается ``valid_to = observedAt``; ничего не удаляется. Закрывается
  только то, что пришло от того же ``(source, scope)``: снимок одного источника не
  трогает сущности и связи другого.

Идентичность: сущность — ``(kind, key)``, связь — ``(relation, from, to)``. Связь —
набор: у субъекта может быть много объектов одной связи, пропавшие пары закрываются
поштучно. Замена 1:1 (``supersedes`` при смене объекта) — только у связи пакета с
``cardinality: one``.

Связь может вести на сущность из снимка другого источника (ключи глобальны в
namespace). Если конца ещё нет в графе, связь хранится в журнале **отложенной**
(``graph_ref = ''``) и материализуется ребром, как только конец появится — при
сверке любого снимка, который открыл или подтвердил сущность-конец (в том числе
узел, появившийся в графе мимо сверки), или при следующей сверке своего источника.

fact_id ребра сверки привязан к источнику и версии: ``(namespace, source, scope,
связь, снимок открытия)``. Поэтому закрытие фактом одного источника не трогает факт
другого, а повтор с тем же ``valid_from`` не переоткрывает закрытое ребро — ребро
создаётся заново (CREATE), а не сливается.

Идемпотентно по ``(namespace, source, scope, snapshotId)``: повтор снимка ничего не
меняет и возвращает сохранённые счётчики с ``duplicate: true``. Снимок старше
последнего принятого для ``(source, scope)`` отвергается (``StaleSnapshotError``).
Сверки одного namespace сериализуются advisory-локом: разрешение отложенных связей
пересекает источники.

Журнал версий (``<snapshots_table>_items``) — реляционный: по нему восстанавливается
состояние сущности на ``as_of`` и id снимков, из которых собран контекст.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.kinds import KindCatalog
from platform_memory.core.namespaces import resolve_namespace
from platform_memory.core.scopes import (
    MAX_ITEM_SCOPES,
    observation_visibility_sql,
    resolve_scopes,
)
from platform_memory.domain.registry import attach_kind_guard
from platform_memory.graph.facts import FactStore, normalize_ts
from platform_memory.graph.store import GraphStore

MAX_SOURCE_LEN = 200
MAX_SCOPE_LEN = 200
MAX_SNAPSHOT_ID_LEN = 200
# Служебные ключи props узла, которые пишет сверка (атрибуты их не перекрывают).
RESERVED_PROPS = frozenset({"aliases", "snapshot", "scopes", "provenance"})
_SEP = "\x1f"


class SnapshotError(ValueError):
    """Некорректный снимок (400)."""


class StaleSnapshotError(ValueError):
    """Снимок старше последнего принятого для (source, scope) (409)."""


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and not isinstance(value, dict | list):
            return str(value).strip()
    return ""


def provenance_path(prov: dict[str, Any] | None) -> str:
    """Цитата по provenance: ``repo@sha:path:line`` (что есть из частей)."""
    if not prov:
        return ""
    repo, sha = str(prov.get("repo") or ""), str(prov.get("sha") or "")
    path, line = str(prov.get("path") or ""), prov.get("line")
    head = f"{repo}@{sha}" if repo and sha else repo or sha
    tail = f"{path}:{line}" if path and line not in (None, "") else path
    return ":".join(p for p in (head, tail) if p)


def fact_id_for_version(ns: str, source: str, scope: str, item_key: str, snapshot_id: str) -> str:
    """fact_id ребра сверки: источник + связь + снимок, открывший версию."""
    raw = _SEP.join((ns, source, scope, item_key, snapshot_id))
    return f"fact-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True, slots=True)
class Ref:
    """Конец связи: ``(kind, key)`` — идентичность сущности в namespace."""

    kind: str
    key: str

    @property
    def ident(self) -> str:
        return f"{self.kind}{_SEP}{self.key}"

    @classmethod
    def from_ident(cls, ident: str) -> Ref:
        kind, _, key = ident.partition(_SEP)
        return cls(kind, key)

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "key": self.key}


@dataclass(slots=True)
class SnapshotEntity:
    kind: str
    key: str
    title: str
    attributes: dict[str, Any]
    provenance: dict[str, Any] | None
    aliases: list[str]
    source_path: str

    @property
    def ref(self) -> Ref:
        return Ref(self.kind, self.key)

    def content(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "title": self.title,
            "attributes": self.attributes,
            "provenance": self.provenance,
            "aliases": self.aliases,
            "source_path": self.source_path,
        }


@dataclass(slots=True)
class SnapshotRelation:
    relation: str
    src: Ref
    dst: Ref
    attributes: dict[str, Any]
    provenance: dict[str, Any] | None = None

    @property
    def item_key(self) -> str:
        return _SEP.join((self.relation, self.src.ident, self.dst.ident))

    def content(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "from": self.src.to_payload(),
            "to": self.dst.to_payload(),
            "attributes": self.attributes,
            "provenance": self.provenance,
        }


@dataclass(slots=True)
class Snapshot:
    source: str
    scope: str
    snapshot_id: str
    observed_at: str
    pack: str = ""
    entities: list[SnapshotEntity] = field(default_factory=list)
    relations: list[SnapshotRelation] = field(default_factory=list)


def _check_pack(ref: str, catalog: KindCatalog) -> None:
    """Пакет снимка должен быть включён в namespace (пакеты действуют только там)."""
    name, _, version = ref.partition("@")
    enabled = [p for p in catalog.packs if p.name == name.strip()]
    if not enabled:
        raise SnapshotError(
            f"Пакет снимка {ref!r} не включён в namespace — включите его "
            "(PUT /api/memory/namespaces/{ns}/kinds); действуют: "
            + (", ".join(p.ref for p in catalog.packs) or "—")
        )
    if version.strip() and enabled[0].version != version.strip():
        raise SnapshotError(f"Пакет снимка {ref!r}: в namespace включена версия {enabled[0].ref}")


def _ref(raw: Any, catalog: KindCatalog, where: str) -> Ref:
    if not isinstance(raw, dict):
        raise SnapshotError(f"{where}: ожидается объект {{kind, key}}")
    kind, key = _text(raw, "kind"), _text(raw, "key")
    if not kind or not key:
        raise SnapshotError(f"{where}: нужны kind и key")
    return Ref(catalog.canonical(kind), key)


def _optional_dict(raw: dict[str, Any], name: str, where: str) -> dict[str, Any] | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SnapshotError(f"{where}.{name}: ожидается объект")
    return value


def parse_snapshot(
    payload: dict[str, Any], catalog: KindCatalog, *, max_items: int = 20000
) -> Snapshot:
    """Разобрать документ снимка как есть и проверить виды и связи по каталогу namespace.

    Всё, что может отвергнуть снимок, проверяется ДО записи: сверка либо проходит
    целиком, либо не меняет ничего.
    """
    if not isinstance(payload, dict):
        raise SnapshotError("Снимок должен быть объектом")
    source = _text(payload, "source")
    if not source or len(source) > MAX_SOURCE_LEN:
        raise SnapshotError(f"source обязателен (до {MAX_SOURCE_LEN} символов)")
    scope = payload.get("scope", "")
    if scope is None:
        scope = ""
    if not isinstance(scope, str) or len(scope) > MAX_SCOPE_LEN:
        raise SnapshotError(f"scope снимка — строка до {MAX_SCOPE_LEN} символов")
    sid = _text(payload, "snapshotId", "snapshot_id")
    if not sid or len(sid) > MAX_SNAPSHOT_ID_LEN:
        raise SnapshotError(f"snapshotId обязателен (до {MAX_SNAPSHOT_ID_LEN} символов)")
    observed_raw = _text(payload, "observedAt", "observed_at")
    if not observed_raw:
        raise SnapshotError("observedAt обязателен: это время снимка")
    try:
        observed_at = normalize_ts(observed_raw) or ""
    except ValueError as exc:
        raise SnapshotError(f"observedAt: невалидная метка времени: {exc}") from exc
    pack = _text(payload, "pack")
    if pack:
        _check_pack(pack, catalog)

    raw_entities = payload.get("entities") or []
    raw_relations = payload.get("relations") or []
    if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
        raise SnapshotError("entities и relations должны быть списками")
    if len(raw_entities) + len(raw_relations) > max_items:
        raise SnapshotError(f"Снимок больше лимита {max_items} элементов")

    snap = Snapshot(
        source=source, scope=scope.strip(), snapshot_id=sid, observed_at=observed_at, pack=pack
    )
    seen: set[Ref] = set()
    for i, raw in enumerate(raw_entities):
        where = f"entities[{i}]"
        if not isinstance(raw, dict):
            raise SnapshotError(f"{where}: ожидается объект")
        attrs = _optional_dict(raw, "attributes", where) or {}
        clash = RESERVED_PROPS.intersection(attrs)
        if clash:
            raise SnapshotError(f"{where}.attributes: зарезервированные ключи {sorted(clash)}")
        provenance = _optional_dict(raw, "provenance", where)
        kind_in = _text(raw, "kind")
        if not kind_in:
            raise SnapshotError(f"{where}.kind обязателен")
        key = _text(raw, "key")
        if not key:
            spec = catalog.spec(kind_in)
            built = spec.build_key(attrs) if spec is not None else None
            if not built:
                raise SnapshotError(f"{where}: нужен key (или шаблон naturalKey вида)")
            key = built
        # Строгий режим: неизвестный вид / ключ / атрибуты вне схемы -> отказ всего снимка.
        kind = catalog.check_entity(kind_in, natural_key=key, attributes=attrs)
        ref = Ref(kind, key)
        if ref in seen:
            raise SnapshotError(f"{where}: сущность {kind}:{key!r} встречается в снимке дважды")
        seen.add(ref)
        explicit = raw.get("aliases") or []
        if not isinstance(explicit, list):
            raise SnapshotError(f"{where}.aliases: ожидается список строк")
        aliases = {str(a).strip() for a in explicit if str(a).strip()}
        aliases.update(catalog.key_aliases(kind, key, attrs))
        aliases.discard(key)
        snap.entities.append(
            SnapshotEntity(
                kind=kind,
                key=key,
                title=_text(raw, "title") or key,
                attributes=attrs,
                provenance=provenance,
                aliases=sorted(aliases),
                source_path=_text(raw, "source_path", "url") or provenance_path(provenance),
            )
        )

    rel_keys: set[str] = set()
    for i, raw in enumerate(raw_relations):
        where = f"relations[{i}]"
        if not isinstance(raw, dict):
            raise SnapshotError(f"{where}: ожидается объект")
        relation = _text(raw, "relation")
        if not relation:
            raise SnapshotError(f"{where}.relation обязателен")
        src = _ref(raw.get("from"), catalog, f"{where}.from")
        dst = _ref(raw.get("to"), catalog, f"{where}.to")
        # Строгий режим: связь объявлена, виды концов — из fromKinds/toKinds.
        relation = catalog.check_relation(relation, src.kind, dst.kind)
        rel = SnapshotRelation(
            relation=relation,
            src=src,
            dst=dst,
            attributes=_optional_dict(raw, "attributes", where) or {},
            provenance=_optional_dict(raw, "provenance", where),
        )
        if rel.item_key in rel_keys:
            raise SnapshotError(f"{where}: связь встречается в снимке дважды")
        rel_keys.add(rel.item_key)
        snap.relations.append(rel)
    return snap


@dataclass(slots=True)
class LedgerItem:
    id: int
    source: str
    scope: str
    item_type: str
    kind: str
    item_key: str
    content_hash: str
    graph_ref: str
    valid_from: str
    opened_in: str
    ref_from: str
    ref_to: str
    payload: dict[str, Any]


_ITEM_COLUMNS = (
    "id, source, scope, item_type, kind, item_key, content_hash, graph_ref, valid_from, "
    "opened_in, ref_from, ref_to, payload"
)


def _item(row: Sequence[Any]) -> LedgerItem:
    return LedgerItem(*row[:12], payload=dict(row[12] or {}))


class SnapshotLedger:
    """Журнал снимков и версий элементов источников (две таблицы Postgres)."""

    def __init__(self, conn: psycopg.Connection, table: str):
        self.conn = conn
        self.table = table
        self.items_table = f"{table}_items"

    def _t(self, name: str) -> sql.Identifier:
        return sql.Identifier("public", name)

    def _idx(self, suffix: str) -> sql.Identifier:
        return sql.Identifier(f"{self.items_table}_{suffix}")

    def ensure_schema(self) -> None:
        items = self._t(self.items_table)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {snaps} (
                        namespace text NOT NULL,
                        source text NOT NULL,
                        scope text NOT NULL DEFAULT '',
                        snapshot_id text NOT NULL,
                        observed_at text NOT NULL,
                        pack text NOT NULL DEFAULT '',
                        result jsonb NOT NULL,
                        actor text NOT NULL DEFAULT '',
                        received_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, source, scope, snapshot_id)
                    )
                    """
                ).format(snaps=self._t(self.table))
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {items} (
                        id bigserial PRIMARY KEY,
                        namespace text NOT NULL,
                        source text NOT NULL,
                        scope text NOT NULL DEFAULT '',
                        item_type text NOT NULL,
                        kind text NOT NULL,
                        item_key text NOT NULL,
                        version_id text NOT NULL,
                        content_hash text NOT NULL,
                        graph_ref text NOT NULL,
                        payload jsonb NOT NULL,
                        valid_from text NOT NULL,
                        valid_to text,
                        opened_in text NOT NULL,
                        closed_in text,
                        superseded_by text,
                        ref_from text NOT NULL DEFAULT '',
                        ref_to text NOT NULL DEFAULT ''
                    )
                    """
                ).format(items=items)
            )
            for suffix, cols, where in (
                ("open_idx", "(namespace, source, scope)", "WHERE valid_to IS NULL"),
                ("key_idx", "(namespace, item_type, item_key)", ""),
                (
                    "pending_to_idx",
                    "(namespace, ref_to)",
                    "WHERE graph_ref = '' AND valid_to IS NULL",
                ),
                (
                    "pending_from_idx",
                    "(namespace, ref_from)",
                    "WHERE graph_ref = '' AND valid_to IS NULL",
                ),
                # Канал resolve компилятора (MEM-ADR-020, амендмент «канал resolve»):
                # псевдонимы ключа и суффикс ключа по границе разделителя.
                (
                    "aliases_idx",
                    "USING gin ((payload->'aliases'))",
                    "WHERE item_type = 'entity'",
                ),
                (
                    "rkey_idx",
                    '(namespace, (reverse(item_key) COLLATE "C"))',
                    "WHERE item_type = 'entity'",
                ),
            ):
                cur.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {idx} ON {items} " + cols + " " + where
                    ).format(idx=self._idx(suffix), items=items)
                )

    def table_exists(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{self.items_table}",))
            row = cur.fetchone()
        return bool(row and row[0])

    # --- снимки ---

    def snapshot_result(
        self, ns: str, source: str, scope: str, snapshot_id: str
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT result FROM {t} WHERE namespace=%s AND source=%s AND scope=%s "
                    "AND snapshot_id=%s"
                ).format(t=self._t(self.table)),
                (ns, source, scope, snapshot_id),
            )
            row = cur.fetchone()
        return dict(row[0]) if row else None

    def last_observed_at(self, ns: str, source: str, scope: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT max(observed_at) FROM {t} WHERE namespace=%s AND source=%s AND scope=%s"
                ).format(t=self._t(self.table)),
                (ns, source, scope),
            )
            row = cur.fetchone()
        return row[0] if row and row[0] else None

    def record_snapshot(self, ns: str, snap: Snapshot, result: dict[str, Any], actor: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {t} (namespace, source, scope, snapshot_id, observed_at, pack, "
                    "result, actor) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(t=self._t(self.table)),
                (
                    ns,
                    snap.source,
                    snap.scope,
                    snap.snapshot_id,
                    snap.observed_at,
                    snap.pack,
                    Jsonb(result),
                    actor,
                ),
            )

    # --- версии элементов ---

    def open_items(
        self, ns: str, source: str, scope: str
    ) -> dict[tuple[str, str, str], LedgerItem]:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    f"SELECT {_ITEM_COLUMNS} FROM {{t}} WHERE namespace=%s AND source=%s "
                    "AND scope=%s AND valid_to IS NULL"
                ).format(t=self._t(self.items_table)),
                (ns, source, scope),
            )
            rows = cur.fetchall()
        items = [_item(r) for r in rows]
        return {(i.item_type, i.kind, i.item_key): i for i in items}

    def insert_items(self, rows: Sequence[tuple]) -> None:
        """Вставить версии: ``(namespace, source, scope, item_type, kind, item_key,
        version_id, content_hash, graph_ref, payload, valid_from, opened_in, ref_from,
        ref_to)``."""
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    "INSERT INTO {t} (namespace, source, scope, item_type, kind, item_key, "
                    "version_id, content_hash, graph_ref, payload, valid_from, opened_in, "
                    "ref_from, ref_to) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(t=self._t(self.items_table)),
                rows,
            )

    def close_items(self, rows: Sequence[tuple[str, str, str | None, int]]) -> None:
        """Закрыть версии: ``(valid_to, closed_in, superseded_by, id)``."""
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    "UPDATE {t} SET valid_to=%s, closed_in=%s, superseded_by=%s WHERE id=%s"
                ).format(t=self._t(self.items_table)),
                rows,
            )

    def set_graph_refs(self, rows: Sequence[tuple[str, int]]) -> None:
        """Материализованные отложенные связи: ``(graph_ref, id)``."""
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                sql.SQL("UPDATE {t} SET graph_ref=%s WHERE id=%s").format(
                    t=self._t(self.items_table)
                ),
                rows,
            )

    def open_entities(self, ns: str, refs: Iterable[Ref]) -> set[Ref]:
        """Какие сущности держит открытыми хоть один источник namespace."""
        refs = list(refs)
        if not refs:
            return set()
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT DISTINCT kind, item_key FROM {t} WHERE namespace=%s "
                    "AND item_type='entity' AND item_key = ANY(%s) AND valid_to IS NULL"
                ).format(t=self._t(self.items_table)),
                (ns, sorted({r.key for r in refs})),
            )
            rows = cur.fetchall()
        wanted = set(refs)
        return {Ref(k, key) for k, key in rows} & wanted

    def pending_for(self, ns: str, refs: Iterable[Ref]) -> list[LedgerItem]:
        """Отложенные связи любых источников namespace с концом среди ``refs``."""
        idents = sorted({r.ident for r in refs})
        if not idents:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    f"SELECT {_ITEM_COLUMNS} FROM {{t}} WHERE namespace=%s "
                    "AND item_type='relation' AND graph_ref='' AND valid_to IS NULL "
                    "AND (ref_to = ANY(%s) OR ref_from = ANY(%s)) ORDER BY id"
                ).format(t=self._t(self.items_table)),
                (ns, idents, idents),
            )
            rows = cur.fetchall()
        return [_item(r) for r in rows]

    def entity_versions(
        self, namespaces: Sequence[str], keys: Sequence[str]
    ) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        """Все версии сущностей (по источникам) — для состояния на ``as_of``.

        Ключ — ``(namespace, kind, key)``.
        """
        if not keys:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT namespace, kind, item_key, source, scope, version_id, payload, "
                    "valid_from, valid_to, opened_in FROM {t} WHERE item_type='entity' "
                    "AND namespace = ANY(%s) AND item_key = ANY(%s) ORDER BY id"
                ).format(t=self._t(self.items_table)),
                (list(namespaces), list(keys)),
            )
            rows = cur.fetchall()
        out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for ns, kind, key, source, scope, vid, payload, vf, vt, opened_in in rows:
            out.setdefault((ns, kind, key), []).append(
                {
                    "source": source,
                    "scope": scope,
                    "version_id": vid,
                    "payload": dict(payload or {}),
                    "valid_from": vf,
                    "valid_to": vt,
                    "snapshot_id": opened_in,
                }
            )
        return out

    # --- разрешение идентификаторов (канал resolve компилятора) ---

    _ENTITY_COLS = (
        "id, namespace, kind, item_key, source, scope, opened_in, payload, valid_from, valid_to"
    )

    def _entity_rows(
        self,
        where: str,
        params: Sequence[Any],
        namespaces: Sequence[str],
        as_of: str,
        allowed_scopes: Sequence[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Версии сущностей namespaces, валидные на ``as_of`` и видимые вызывающему."""
        cond = ["item_type = 'entity'", "namespace = ANY(%s)", where]
        args: list[Any] = [list(namespaces), *params]
        if as_of:
            cond.append("valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)")
            args += [as_of, as_of]
        else:
            cond.append("valid_to IS NULL")
        if allowed_scopes is not None:
            cond.append(observation_visibility_sql("coalesce(payload->'scopes', '[]'::jsonb)"))
            args.append(list(allowed_scopes))
        query = (
            f"SELECT {self._ENTITY_COLS} FROM {{t}} WHERE "
            + " AND ".join(cond)
            + " ORDER BY item_key, id LIMIT %s"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL(query).format(t=self._t(self.items_table)), (*args, int(limit)))
            return [_entity_row(r) for r in cur.fetchall()]

    def entities_by_keys(
        self,
        namespaces: Sequence[str],
        keys: Sequence[str],
        *,
        as_of: str = "",
        allowed_scopes: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Сущности с точным ключом из списка (индекс ``key_idx``)."""
        if not keys or not namespaces:
            return []
        return self._entity_rows(
            "item_key = ANY(%s)", [list(keys)], namespaces, as_of, allowed_scopes, limit
        )

    def entities_by_aliases(
        self,
        namespaces: Sequence[str],
        aliases: Sequence[str],
        *,
        as_of: str = "",
        allowed_scopes: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Сущности, у которых среди ``aliases`` версии есть любая из строк (GIN ``aliases_idx``)."""
        if not aliases or not namespaces:
            return []
        return self._entity_rows(
            "(payload->'aliases') ?| %s::text[]",
            [list(aliases)],
            namespaces,
            as_of,
            allowed_scopes,
            limit,
        )

    def entities_by_suffixes(
        self,
        namespaces: Sequence[str],
        suffixes: Sequence[str],
        *,
        as_of: str = "",
        allowed_scopes: Sequence[str] | None = None,
        per_suffix: int = 10,
        per_namespace: bool = False,
        kinds: Sequence[Sequence[str] | None] | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        """Сущности, чей ключ оканчивается строкой из ``suffixes``: пары (номер суффикса, строка).

        Суффикс ключа — префикс перевёрнутого ключа: диапазон по индексу ``rkey_idx``
        (``reverse(item_key) COLLATE "C"``) на каждый суффикс, не больше ``per_suffix``
        (``per_namespace`` — не больше ``per_suffix`` в каждом namespace). ``kinds`` —
        допустимые виды на каждый суффикс (по порядку; None — любой): фильтр до лимита,
        сущности чужих видов не занимают места нужных.
        """
        kinds = list(kinds) if kinds is not None else [None] * len(suffixes)
        if len(kinds) != len(suffixes):
            raise ValueError("kinds должен соответствовать suffixes")
        bounds = [
            (r, _prefix_upper(r), None if k is None else json.dumps(sorted(k)))
            for r, k in ((s[::-1], k) for s, k in zip(suffixes, kinds, strict=True) if s)
        ]
        if not bounds or not namespaces:
            return []
        cond = [
            "t.item_type = 'entity'",
            "t.namespace = n.ns" if per_namespace else "t.namespace = ANY(%s)",
            'reverse(t.item_key) COLLATE "C" >= v.lo',
            'reverse(t.item_key) COLLATE "C" < v.hi',
            "(v.kinds IS NULL OR t.kind IN (SELECT jsonb_array_elements_text(v.kinds::jsonb)))",
        ]
        args: list[Any] = [] if per_namespace else [list(namespaces)]
        if as_of:
            cond.append("t.valid_from <= %s AND (t.valid_to IS NULL OR t.valid_to > %s)")
            args += [as_of, as_of]
        else:
            cond.append("t.valid_to IS NULL")
        if allowed_scopes is not None:
            cond.append(observation_visibility_sql("coalesce(t.payload->'scopes', '[]'::jsonb)"))
            args.append(list(allowed_scopes))
        cols = ", ".join(f"t.{c.strip()}" for c in self._ENTITY_COLS.split(","))
        # Пространства — отдельным unnest: лимит на суффикс в каждом namespace.
        spaces = " CROSS JOIN unnest(%s::text[]) AS n(ns)" if per_namespace else ""
        query = (
            f"SELECT v.i, {cols} FROM unnest(%s::text[], %s::text[], %s::text[]) "
            f"WITH ORDINALITY AS v(lo, hi, kinds, i){spaces} CROSS JOIN LATERAL (SELECT * FROM {{t}} t WHERE "
            + " AND ".join(cond)
            + " ORDER BY t.item_key, t.id LIMIT %s) t ORDER BY v.i, t.namespace, t.item_key, t.id"
        )
        params = [[b[0] for b in bounds], [b[1] for b in bounds], [b[2] for b in bounds]]
        if per_namespace:
            params.append(list(namespaces))
        params += [*args, int(per_suffix)]
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL(query).format(t=self._t(self.items_table)), params)
            rows = cur.fetchall()
        return [(int(r[0]) - 1, _entity_row(r[1:])) for r in rows]


def _prefix_upper(prefix: str) -> str:
    """Верхняя граница диапазона строк с префиксом ``prefix`` (порядок "C" = кодовые точки)."""
    last = ord(prefix[-1])
    return prefix[:-1] + chr(last + 1) if last < 0x10FFFF else prefix + "\U0010ffff"


def _entity_row(row: Sequence[Any]) -> dict[str, Any]:
    rid, ns, kind, key, source, scope, opened_in, payload, vf, vt = row
    return {
        "id": int(rid),
        "namespace": ns,
        "kind": kind,
        "key": key,
        "source": source,
        "scope": scope,
        "snapshot_id": opened_in,
        "payload": dict(payload or {}),
        "valid_from": vf,
        "valid_to": vt,
    }


def valid_at(valid_from: str | None, valid_to: str | None, as_of: str) -> bool:
    """Интервал [valid_from, valid_to) содержит момент; пустой as_of — «сейчас» (открыт)."""
    if not as_of:
        return not valid_to
    if valid_from and valid_from > as_of:
        return False
    return not valid_to or valid_to > as_of


def _counters(*extra: str) -> dict[str, int]:
    return {k: 0 for k in ("opened", "closed", "unchanged", "superseded", *extra)}


@dataclass(slots=True)
class _EdgePlan:
    """Ребро к созданию: версия связи из журнала (новая или отложенная)."""

    relation: str
    src: Ref
    dst: Ref
    fact_id: str
    valid_from: str
    supersedes: str | None
    source_path: str
    attributes: dict[str, Any]
    source: str
    scope: str
    snapshot_id: str
    scopes: list[str]
    ledger_id: int | None = None  # отложенная версия, уже лежащая в журнале
    ledger_row: tuple | None = None  # новая версия, ещё не записанная в журнал


class _Reconciler:
    """Одна сверка снимка: план изменений, запись в граф и журнал, счётчики."""

    def __init__(
        self,
        ns: str,
        snap: Snapshot,
        scopes: list[str],
        catalog: KindCatalog,
        graph: GraphStore,
        facts: FactStore,
        ledger: SnapshotLedger,
    ):
        self.ns = ns
        self.snap = snap
        self.scopes = scopes
        self.catalog = catalog
        self.graph = graph
        self.facts = facts
        self.ledger = ledger
        self.ent = _counters()
        self.rel = _counters("pending", "resolved", "retried")
        self.default_sp = f"snapshot:{snap.source}/{snap.snapshot_id}"

    def _version_id(self, item_type: str, kind: str, item_key: str) -> str:
        s = self.snap
        return (
            "ver-"
            + _digest([self.ns, s.source, s.scope, item_type, kind, item_key, s.snapshot_id])[:24]
        )

    def _row(
        self,
        item_type: str,
        kind: str,
        item_key: str,
        content: dict,
        h: str,
        graph_ref: str,
        ref_from: str = "",
        ref_to: str = "",
    ) -> tuple:
        s = self.snap
        return (
            self.ns,
            s.source,
            s.scope,
            item_type,
            kind,
            item_key,
            self._version_id(item_type, kind, item_key),
            h,
            graph_ref,
            Jsonb(content),
            s.observed_at,
            s.snapshot_id,
            ref_from,
            ref_to,
        )

    # --- сущности ---

    def entities(self, open_items: dict[tuple[str, str, str], LedgerItem]) -> list[Ref]:
        """Сверить сущности; вернуть все сущности снимка (открытые и подтверждённые).

        По ним перепроверяются отложенные связи других источников: конец мог
        вернуться узлом мимо сверки (запись агента) или новой версией этого же
        источника — а не только появиться этим снимком впервые.
        """
        s = self.snap
        upserts: dict[str, list[dict[str, Any]]] = {}
        new_rows: list[tuple] = []
        closes: list[tuple[str, str, str | None, int]] = []
        for e in s.entities:
            content = {**e.content(), "scopes": self.scopes}
            h = _digest(content)
            row = open_items.pop(("entity", e.kind, e.key), None)
            if row is not None and row.content_hash == h:
                self.ent["unchanged"] += 1
                continue
            props: dict[str, Any] = dict(e.attributes)
            if e.aliases:
                props["aliases"] = e.aliases
            if e.provenance:
                props["provenance"] = e.provenance
            if self.scopes:
                props["scopes"] = self.scopes
            props["snapshot"] = {
                "source": s.source,
                "scope": s.scope,
                "snapshot_id": s.snapshot_id,
            }
            upserts.setdefault(e.kind, []).append(
                {"k": e.key, "t": e.title, "sp": e.source_path or self.default_sp, "props": props}
            )
            new_row = self._row("entity", e.kind, e.key, content, h, e.key)
            new_rows.append(new_row)
            self.ent["opened"] += 1
            if row is not None:
                closes.append((s.observed_at, s.snapshot_id, new_row[6], row.id))
                self.ent["closed"] += 1
                self.ent["superseded"] += 1

        vanished = [row for (itype, _, _), row in list(open_items.items()) if itype == "entity"]
        for row in vanished:
            open_items.pop(("entity", row.kind, row.item_key), None)
            closes.append((s.observed_at, s.snapshot_id, None, row.id))
            self.ent["closed"] += 1

        for kind in sorted(upserts):
            self.graph.upsert_entities(
                kind, upserts[kind], self.ns, valid_from=s.observed_at, last_seen=_now_iso()
            )
        self.ledger.close_items(closes)
        self.ledger.insert_items(new_rows)
        # Узел закрывается, только если его не держит открытым другой источник.
        gone = [Ref(r.kind, r.item_key) for r in vanished]
        still_open = self.ledger.open_entities(self.ns, gone)
        by_kind: dict[str, list[str]] = {}
        for ref in gone:
            if ref not in still_open:
                by_kind.setdefault(ref.kind, []).append(ref.key)
        for kind in sorted(by_kind):
            self.graph.close_entities(kind, by_kind[kind], self.ns, valid_to=s.observed_at)
        return [e.ref for e in s.entities]

    # --- связи ---

    def relations(
        self, open_items: dict[tuple[str, str, str], LedgerItem], seen: list[Ref]
    ) -> None:
        s = self.snap
        changed: list[tuple[SnapshotRelation, LedgerItem | None, str, dict[str, Any]]] = []
        retry: list[LedgerItem] = []
        for r in s.relations:
            content = {**r.content(), "scopes": self.scopes}
            h = _digest(content)
            row = open_items.pop(("relation", r.relation, r.item_key), None)
            if row is not None and row.content_hash == h:
                self.rel["unchanged"] += 1
                if not row.graph_ref:
                    retry.append(row)  # отложенная: вдруг конец уже появился
                continue
            changed.append((r, row, h, content))
        vanished = [row for (itype, _, _), row in open_items.items() if itype == "relation"]

        # cardinality: one — смена объекта у (subject, relation) 1:1 — замена, а не
        # «закрыт + открыт». Связь-набор (many, по умолчанию) закрывает пары поштучно.
        gone_by_subject: dict[tuple[str, str], list[LedgerItem]] = {}
        for row in vanished:
            gone_by_subject.setdefault((row.kind, row.ref_from), []).append(row)
        new_by_subject: dict[tuple[str, str], list[int]] = {}
        for i, (r, row, _, _) in enumerate(changed):
            if row is None:
                new_by_subject.setdefault((r.relation, r.src.ident), []).append(i)
        paired: set[int] = set()
        for subject, idxs in new_by_subject.items():
            spec = self.catalog.relation(subject[0])
            gone = gone_by_subject.get(subject, [])
            if spec is not None and spec.cardinality == "one" and len(idxs) == 1 and len(gone) == 1:
                r, _, h, content = changed[idxs[0]]
                changed[idxs[0]] = (r, gone[0], h, content)
                paired.add(gone[0].id)

        closes: list[tuple[str, str, str | None, int]] = []
        edge_closes: dict[str, list[dict[str, Any]]] = {}
        for row in vanished:
            if row.id in paired:
                continue
            closes.append((s.observed_at, s.snapshot_id, None, row.id))
            if row.graph_ref:
                edge_closes.setdefault(row.kind, []).append(
                    {"fid": row.graph_ref, "vt": s.observed_at, "nxt": None}
                )
            self.rel["closed"] += 1

        plans: list[_EdgePlan] = []
        for r, row, h, content in changed:
            fid = fact_id_for_version(self.ns, s.source, s.scope, r.item_key, s.snapshot_id)
            new_row = self._row(
                "relation", r.relation, r.item_key, content, h, "", r.src.ident, r.dst.ident
            )
            plans.append(
                _EdgePlan(
                    relation=r.relation,
                    src=r.src,
                    dst=r.dst,
                    fact_id=fid,
                    valid_from=s.observed_at,
                    supersedes=(row.graph_ref or None) if row is not None else None,
                    source_path=provenance_path(r.provenance) or self.default_sp,
                    attributes=r.attributes,
                    source=s.source,
                    scope=s.scope,
                    snapshot_id=s.snapshot_id,
                    scopes=self.scopes,
                    ledger_row=new_row,
                )
            )
            self.rel["opened"] += 1
            if row is not None:
                closes.append((s.observed_at, s.snapshot_id, new_row[6], row.id))
                if row.graph_ref:
                    edge_closes.setdefault(row.kind, []).append(
                        {"fid": row.graph_ref, "vt": s.observed_at, "nxt": fid}
                    )
                self.rel["closed"] += 1
                self.rel["superseded"] += 1

        # Отложенные связи: свои (повтор без изменений) и чужие, чей конец есть в этом
        # снимке — открыт им или подтверждён (узел мог появиться мимо сверки).
        own_pending = {row.id for row in retry}
        others = [
            row
            for row in self.ledger.pending_for(self.ns, seen)
            if row.id not in own_pending and (row.source, row.scope) != (s.source, s.scope)
        ]
        # Перепланированные отложенные связи — видимость цены повтора (MEM-ADR-020,
        # амендмент п.3): сколько из них стало рёбрами — ``resolved``.
        self.rel["retried"] = len(retry) + len(others)
        for row in [*retry, *others]:
            payload = row.payload
            plans.append(
                _EdgePlan(
                    relation=row.kind,
                    src=Ref.from_ident(row.ref_from),
                    dst=Ref.from_ident(row.ref_to),
                    fact_id=fact_id_for_version(
                        self.ns, row.source, row.scope, row.item_key, row.opened_in
                    ),
                    valid_from=row.valid_from,
                    supersedes=None,
                    source_path=provenance_path(payload.get("provenance"))
                    or f"snapshot:{row.source}/{row.opened_in}",
                    attributes=dict(payload.get("attributes") or {}),
                    source=row.source,
                    scope=row.scope,
                    snapshot_id=row.opened_in,
                    scopes=[str(x) for x in payload.get("scopes") or []],
                    ledger_id=row.id,
                )
            )

        for relation in sorted(edge_closes):
            self.facts.close_facts(relation, edge_closes[relation], namespace=self.ns)
        created = self._materialize(plans)

        new_rows: list[tuple] = []
        resolved: list[tuple[str, int]] = []
        for plan in plans:
            done = plan.fact_id in created
            if plan.ledger_row is not None:
                row = list(plan.ledger_row)
                row[8] = plan.fact_id if done else ""
                new_rows.append(tuple(row))
                if not done:
                    self.rel["pending"] += 1
            elif done and plan.ledger_id is not None:
                resolved.append((plan.fact_id, plan.ledger_id))
                self.rel["resolved"] += 1
            elif plan.ledger_id in own_pending:
                self.rel["pending"] += 1
        self.ledger.close_items(closes)
        self.ledger.insert_items(new_rows)
        self.ledger.set_graph_refs(resolved)

    def _materialize(self, plans: list[_EdgePlan]) -> set[str]:
        """Создать рёбра для планов, у которых оба конца есть в графе; вернуть fact_id."""
        wanted: dict[str, set[str]] = {}
        for p in plans:
            wanted.setdefault(p.src.kind, set()).add(p.src.key)
            wanted.setdefault(p.dst.kind, set()).add(p.dst.key)
        present: set[Ref] = set()
        for kind in sorted(wanted):
            for key in self.graph.existing_keys(kind, sorted(wanted[kind]), self.ns):
                present.add(Ref(kind, key))
        batches: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for p in plans:
            if p.src not in present or p.dst not in present:
                continue
            batches.setdefault((p.relation, p.src.kind, p.dst.kind), []).append(
                {
                    "fid": p.fact_id,
                    "s": p.src.key,
                    "d": p.dst.key,
                    "vf": p.valid_from,
                    "sup": p.supersedes,
                    "sp": p.source_path,
                    "a": p.attributes,
                    "src": p.source,
                    "scope": p.scope,
                    "sid": p.snapshot_id,
                    "scopes": p.scopes,
                }
            )
        created: set[str] = set()
        for (relation, from_kind, to_kind), rows in sorted(batches.items()):
            created |= self.facts.create_facts(
                relation,
                from_kind,
                to_kind,
                rows,
                namespace=self.ns,
                observed_at=self.snap.observed_at,
            )
        return created

    def result(self) -> dict[str, Any]:
        s = self.snap
        return {
            "source": s.source,
            "scope": s.scope,
            "namespace": self.ns,
            "snapshot_id": s.snapshot_id,
            "observed_at": s.observed_at,
            "pack": s.pack,
            **{
                k: self.ent[k] + self.rel[k]
                for k in ("opened", "closed", "unchanged", "superseded")
            },
            "entities": self.ent,
            "relations": self.rel,
        }


def reconcile(
    settings: Settings,
    *,
    snapshot: dict[str, Any],
    namespace: str = "",
    scopes: Sequence[str] = (),
    actor: str = "",
    conn: psycopg.Connection | None = None,
) -> dict[str, Any]:
    """Сверить документ снимка (формат SNAPSHOT.md) с памятью namespace; вернуть счётчики.

    ``scopes`` — scopes видимости (MEM-ADR-019), которые ядро выводит из задачи:
    пишутся в узлы и рёбра этого снимка (``props.scopes`` / ``r.scopes``).

    Ответ: ``{opened, closed, unchanged, superseded, entities{…}, relations{…,
    pending, resolved}, source, scope, snapshot_id, observed_at, namespace,
    duplicate}``. Изменившийся элемент считается одновременно закрытым (старая
    версия) и открытым (новая) и попадает в ``superseded``; ``relations.pending`` —
    связи снимка, чей конец ещё не появился, ``relations.resolved`` — отложенные
    связи (свои и чужие), материализованные этой сверкой.
    """
    ns = resolve_namespace(namespace, settings.default_namespace)
    try:
        vis_scopes = resolve_scopes(scopes, limit=MAX_ITEM_SCOPES)
    except ValueError as exc:
        raise SnapshotError(f"scopes: {exc}") from exc

    own_conn = conn is None
    conn = conn or dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        dbmod.ensure_graph(conn, graph.graph)
        registry = attach_kind_guard(graph, settings)
        facts = FactStore(graph)
        ledger = SnapshotLedger(conn, settings.snapshots_table)
        ledger.ensure_schema()
        catalog = registry.catalog_for(ns)
        snap = parse_snapshot(snapshot, catalog, max_items=settings.reconcile_max_items)

        with conn.transaction():
            with conn.cursor() as cur:
                # Весь namespace: отложенные связи разрешаются через границу источников.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{graph.graph}:reconcile:{ns}",),
                )
            stored = ledger.snapshot_result(ns, snap.source, snap.scope, snap.snapshot_id)
            if stored is not None:
                return {**stored, "duplicate": True}
            last = ledger.last_observed_at(ns, snap.source, snap.scope)
            if last and snap.observed_at < last:
                raise StaleSnapshotError(
                    f"Снимок {snap.snapshot_id!r} ({snap.observed_at}) старше последнего "
                    f"принятого для {snap.source!r}/{snap.scope!r} ({last})"
                )
            run = _Reconciler(ns, snap, vis_scopes, catalog, graph, facts, ledger)
            open_items = ledger.open_items(ns, snap.source, snap.scope)
            seen = run.entities(open_items)
            run.relations(open_items, seen)
            result = run.result()
            ledger.record_snapshot(ns, snap, result, actor)
        return {**result, "duplicate": False}
    finally:
        if own_conn:
            conn.close()


def ledger_for(settings: Settings, conn: psycopg.Connection) -> SnapshotLedger:
    return SnapshotLedger(conn, settings.snapshots_table)


__all__ = [
    "SnapshotError",
    "StaleSnapshotError",
    "SnapshotLedger",
    "parse_snapshot",
    "provenance_path",
    "reconcile",
    "valid_at",
]
