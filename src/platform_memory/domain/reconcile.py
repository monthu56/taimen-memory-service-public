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

Сущность, которую держат несколько источников, — сведение их версий (MEM-ADR-022,
``domain/merge.py``): узел графа сверка пишет сведением всех открытых версий, а не
снимком одного источника; атрибут одного источника не пропадает от снимка другого,
конфликт атрибута — ``sourcePriority`` вида, затем «последний писавший» по ``since``
версии (с какого ``observedAt`` источник держит значение; в хэш не входит).

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

Идемпотентно по последнему принятому снимку пары ``(namespace, source, scope)``: его
повтор ничего не меняет и возвращает сохранённые счётчики с ``duplicate: true``. Более
ранний ``snapshotId`` (A → B → A: коннектор делает id хэшем содержимого) применяется
как новый — следующим номером применения (``apply_no``, амендмент MEM-ADR-020
2026-09-30). Снимок старше последнего принятого для ``(source, scope)`` отвергается
(``StaleSnapshotError``).
Сверки одного namespace сериализуются advisory-локом: разрешение отложенных связей
пересекает источники.

Ответ сверки несёт, кроме счётчиков, естественные ключи узлов, которые она тронула
(``changes``: ``opened`` — новые, ``changed`` — изменившиеся, ``closed`` — пропавшие
из снимка; ``{kind, key}``, не больше ``limit`` в каждом списке, ``truncated`` —
какой-то список обрезан). Повтор снимка ничего не меняет — списки пустые.

Предпросмотр и применение по состоянию (амендмент 2026-09-28): ``dry_run`` строит тот
же план в откатываемой транзакции. ``stateToken`` — отпечаток открытых версий пары
``(source, scope)``, хранится в ``<snapshots_table>_state``; ``expected_state``, не
совпавший с ним, — ``SnapshotStateMismatch``. ``conflicts`` — сущности снимка, открытые
в namespace другим ``(source, scope)``.

Журнал версий (``<snapshots_table>_items``) — реляционный: по нему восстанавливается
состояние сущности на ``as_of`` и id снимков, из которых собран контекст.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.kinds import AttributesError, KindCatalog, split_pack_ref
from platform_memory.core.namespaces import resolve_namespace
from platform_memory.core.scopes import (
    MAX_ITEM_SCOPES,
    observation_visibility_sql,
    resolve_scopes,
)
from platform_memory.domain.merge import (
    SINCE_KEY,
    SourceRank,
    merge_by_entity,
    rank_by_catalog,
    stamp_since,
)
from platform_memory.domain.registry import attach_kind_guard
from platform_memory.domain.searchable import entity_index, reconcile_lock_key, sync_entities
from platform_memory.graph.facts import FactStore, normalize_ts
from platform_memory.graph.store import GraphStore
from platform_memory.index import build_embedder
from platform_memory.index.embeddings import Embedder
from platform_memory.observations.store import SchemaMigrationBusy

MAX_SOURCE_LEN = 200
MAX_SCOPE_LEN = 200
MAX_SNAPSHOT_ID_LEN = 200
# Служебные ключи props узла, которые пишет сверка (атрибуты их не перекрывают).
RESERVED_PROPS = frozenset({"aliases", "snapshot", "scopes", "provenance"})
_SEP = "\x1f"
# Префикс stateToken: версия схемы отпечатка (смена схемы — новый префикс).
STATE_TOKEN_PREFIX = "st1-"


class SnapshotError(ValueError):
    """Некорректный снимок (400)."""


class StaleSnapshotError(ValueError):
    """Снимок старше последнего принятого для (source, scope) (409)."""


class SnapshotStateMismatch(ValueError):
    """``expectedState`` не совпал с текущим состоянием пары (source, scope) (409
    ``snapshot_stale``, амендмент MEM-ADR-020 2026-09-28)."""

    def __init__(self, expected_state: str, state_token: str):
        super().__init__(
            "Состояние источника изменилось с построения плана: постройте план заново "
            "по новому stateToken"
        )
        self.expected_state = expected_state
        self.state_token = state_token


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


def fact_id_for_version(
    ns: str, source: str, scope: str, item_key: str, snapshot_id: str, apply_no: int = 1
) -> str:
    """fact_id ребра сверки: источник + связь + снимок, открывший версию.

    ``apply_no`` — номер применения этого ``snapshotId`` в паре: повторное применение
    (A → B → A) не должно совпасть с id ребра, закрытого снимком B. Первое применение
    id не солит — id прежних рёбер не меняются.
    """
    parts = [ns, source, scope, item_key, snapshot_id]
    if apply_no > 1:
        parts.append(str(apply_no))
    raw = _SEP.join(parts)
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
    tenant, name, version = split_pack_ref(ref)
    enabled = [p for p in catalog.packs if p.name == name and bool(p.owner) == tenant]
    if not enabled:
        raise SnapshotError(
            f"Пакет снимка {ref!r} не включён в namespace — включите его "
            "(PUT /api/memory/namespaces/{ns}/kinds); действуют: "
            + (", ".join(p.ref for p in catalog.packs) or "—")
        )
    if version and enabled[0].version != version:
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
        # ``required`` — у сведения источников после записи журнала (_check_required).
        kind = catalog.check_entity(kind_in, natural_key=key, attributes=attrs, partial=True)
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
    opened_apply: int = 1  # номер применения снимка ``opened_in`` в паре


_ITEM_COLUMNS = (
    "id, source, scope, item_type, kind, item_key, content_hash, graph_ref, valid_from, "
    "opened_in, ref_from, ref_to, payload, opened_apply"
)


def _item(row: Sequence[Any]) -> LedgerItem:
    return LedgerItem(*row[:12], payload=dict(row[12] or {}), opened_apply=int(row[13]))


class SnapshotLedger:
    """Журнал снимков и версий элементов источников (две таблицы Postgres)."""

    # Ожидание лока миграции журнала (``_migrate_apply_no``) — как у ObservationStore.
    MIGRATION_LOCK_TIMEOUT_MS = 500
    MIGRATION_ATTEMPTS = 10
    MIGRATION_RETRY_PAUSE = 1.0  # секунд

    def __init__(self, conn: psycopg.Connection, table: str):
        self.conn = conn
        self.table = table
        self.items_table = f"{table}_items"
        self.state_table = f"{table}_state"

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
                        apply_no integer NOT NULL DEFAULT 1,
                        PRIMARY KEY (namespace, source, scope, snapshot_id, apply_no)
                    )
                    """
                ).format(snaps=self._t(self.table))
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {state} (
                        namespace text NOT NULL,
                        source text NOT NULL,
                        scope text NOT NULL DEFAULT '',
                        state_token text NOT NULL,
                        snapshot_id text NOT NULL,
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, source, scope)
                    )
                    """
                ).format(state=self._t(self.state_table))
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
                        ref_to text NOT NULL DEFAULT '',
                        opened_apply integer NOT NULL DEFAULT 1
                    )
                    """
                ).format(items=items)
            )
        # Таблицы до амендмента 2026-09-30. ALTER берёт ACCESS EXCLUSIVE даже с
        # IF NOT EXISTS, а ensure_schema зовётся на каждую сверку — поэтому сначала
        # дешёвая проверка каталога.
        if not self._apply_no_migrated():
            self._migrate_apply_no()
        with self.conn.cursor() as cur:
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

    def _snaps_pk_columns(self, cur: psycopg.Cursor) -> list[str]:
        cur.execute(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN LATERAL unnest(c.conkey) AS k(attnum) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.conrelid = to_regclass(%s) AND c.contype = 'p'",
            (f"public.{self.table}",),
        )
        return [str(r[0]) for r in cur.fetchall()]

    def _apply_no_migrated(self) -> bool:
        """``apply_no`` в PK журнала снимков и ``opened_apply`` в журнале версий — по
        каталогу, без локов таблиц."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                "WHERE c.conrelid = to_regclass(%s) AND c.contype = 'p' "
                "AND a.attname = 'apply_no') "
                "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) "
                "AND attname = 'opened_apply' AND NOT attisdropped)",
                (f"public.{self.table}", f"public.{self.items_table}"),
            )
            row = cur.fetchone()
        return bool(row and row[0])

    @property
    def migration_lock_name(self) -> str:
        """Имя advisory-лока миграции журнала (ключ — ``hashtext`` от него)."""
        return f"platform_memory:migrate:{self.table}"

    def _migrate_apply_no(self) -> None:
        """Журнал снимков до амендмента 2026-09-30: PK по ``snapshot_id`` не давал
        применить прежний ``snapshotId`` второй раз. Колонка ``apply_no`` (прежние строки —
        первое применение) и PK с ней, у журнала версий — ``opened_apply``; строки не
        трогаются.

        Как ``ObservationStore._add_writer_visibility``: пока ALTER ждёт лок, за ним в
        очереди стоят все читатели журнала (typed-контекст, /entities, канал resolve),
        поэтому попытка ждёт не дольше ``MIGRATION_LOCK_TIMEOUT_MS``, попыток
        ``MIGRATION_ATTEMPTS`` с паузой ``MIGRATION_RETRY_PAUSE``, затем —
        :class:`SchemaMigrationBusy`. ALTER пробует только сеанс, взявший advisory-лок
        миграции; обе таблицы — в одной транзакции (неудача не оставляет половину).
        """
        for attempt in range(1, self.MIGRATION_ATTEMPTS + 1):
            if attempt > 1:
                time.sleep(self.MIGRATION_RETRY_PAUSE)
                if self._apply_no_migrated():
                    return
            try:
                with self.conn.transaction(), self.conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                        (self.migration_lock_name,),
                    )
                    if not cur.fetchone()[0]:
                        continue  # миграцию уже делает другой сеанс
                    # set_config(…, true) — только до конца транзакции (SET LOCAL).
                    cur.execute(
                        "SELECT set_config('lock_timeout', %s, true)",
                        (f"{self.MIGRATION_LOCK_TIMEOUT_MS}ms",),
                    )
                    self._apply_no_ddl(cur)
                return
            except psycopg.errors.LockNotAvailable:
                continue
        raise SchemaMigrationBusy(
            f"Миграция журнала снимков {self.table} (apply_no) не получила лок таблиц за "
            f"{self.MIGRATION_ATTEMPTS} попыток по {self.MIGRATION_LOCK_TIMEOUT_MS} мс: "
            "журнал держит долгая транзакция (pg_stat_activity). Повторите позже."
        )

    def _apply_no_ddl(self, cur: psycopg.Cursor) -> None:
        """DDL миграции под advisory-локом; каталог перепроверяется — предыдущий
        держатель лока мог уже всё сделать."""
        snaps = self._t(self.table)
        if "apply_no" not in self._snaps_pk_columns(cur):
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {t} ADD COLUMN IF NOT EXISTS apply_no integer NOT NULL DEFAULT 1"
                ).format(t=snaps)
            )
            cur.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(%s) "
                "AND contype = 'p'",
                (f"public.{self.table}",),
            )
            for (name,) in cur.fetchall():
                cur.execute(
                    sql.SQL("ALTER TABLE {t} DROP CONSTRAINT {c}").format(
                        t=snaps, c=sql.Identifier(name)
                    )
                )
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {t} ADD PRIMARY KEY "
                    "(namespace, source, scope, snapshot_id, apply_no)"
                ).format(t=snaps)
            )
        cur.execute(
            "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) "
            "AND attname = 'opened_apply' AND NOT attisdropped",
            (f"public.{self.items_table}",),
        )
        if cur.fetchone() is None:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {t} ADD COLUMN opened_apply integer NOT NULL DEFAULT 1"
                ).format(t=self._t(self.items_table))
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
        """Счётчики последнего применения ``snapshot_id`` в паре (``None`` — не было)."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT result FROM {t} WHERE namespace=%s AND source=%s AND scope=%s "
                    "AND snapshot_id=%s ORDER BY apply_no DESC LIMIT 1"
                ).format(t=self._t(self.table)),
                (ns, source, scope, snapshot_id),
            )
            row = cur.fetchone()
        return dict(row[0]) if row else None

    def last_accepted(self, ns: str, source: str, scope: str) -> tuple[str, dict[str, Any]] | None:
        """Последний принятый снимок пары: ``(snapshot_id, счётчики)``.

        Последний — тот, что записан в состояние пары (``_state``: пишется каждой
        принятой сверкой под advisory-локом, порядок строгий). У пары без состояния
        (снимки до K004) — самый поздний по ``received_at``.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT snapshot_id FROM {st} WHERE namespace=%s AND source=%s AND scope=%s"
                ).format(st=self._t(self.state_table)),
                (ns, source, scope),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    sql.SQL(
                        "SELECT snapshot_id FROM {t} WHERE namespace=%s AND source=%s "
                        "AND scope=%s ORDER BY received_at DESC, apply_no DESC LIMIT 1"
                    ).format(t=self._t(self.table)),
                    (ns, source, scope),
                )
                row = cur.fetchone()
        if row is None:
            return None
        sid = str(row[0])
        result = self.snapshot_result(ns, source, scope, sid)
        return (sid, result) if result is not None else None

    def next_apply_no(self, ns: str, source: str, scope: str, snapshot_id: str) -> int:
        """Номер следующего применения ``snapshot_id`` в паре (первое — 1)."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT coalesce(max(apply_no), 0) + 1 FROM {t} WHERE namespace=%s "
                    "AND source=%s AND scope=%s AND snapshot_id=%s"
                ).format(t=self._t(self.table)),
                (ns, source, scope, snapshot_id),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 1

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

    def record_snapshot(
        self, ns: str, snap: Snapshot, result: dict[str, Any], actor: str, apply_no: int = 1
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {t} (namespace, source, scope, snapshot_id, observed_at, pack, "
                    "result, actor, apply_no) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
                    apply_no,
                ),
            )

    # --- состояние пары (source, scope): stateToken ---

    def compute_state_token(self, ns: str, source: str, scope: str) -> str:
        """Отпечаток открытых версий пары: id версий меняются тогда и только тогда, когда
        сверка открыла, закрыла или заменила элемент (разрешение отложенной связи id
        версии не трогает). У пары без версий — токен пустого состояния."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT coalesce(string_agg(version_id, ',' ORDER BY version_id), '') "
                    "FROM {t} WHERE namespace=%s AND source=%s AND scope=%s "
                    "AND valid_to IS NULL"
                ).format(t=self._t(self.items_table)),
                (ns, source, scope),
            )
            row = cur.fetchone()
        raw = row[0] if row else ""
        return STATE_TOKEN_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

    def state_token(self, ns: str, source: str, scope: str) -> str:
        """Текущий stateToken пары: сохранённый последней принятой сверкой; у пары без
        сохранённого (нет снимков или снимки до миграции K004) — вычисленный."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT state_token FROM {t} WHERE namespace=%s AND source=%s AND scope=%s"
                ).format(t=self._t(self.state_table)),
                (ns, source, scope),
            )
            row = cur.fetchone()
        return row[0] if row else self.compute_state_token(ns, source, scope)

    def save_state(self, ns: str, snap: Snapshot, token: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {t} (namespace, source, scope, state_token, snapshot_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (namespace, source, scope) "
                    "DO UPDATE SET state_token = EXCLUDED.state_token, "
                    "snapshot_id = EXCLUDED.snapshot_id, updated_at = now()"
                ).format(t=self._t(self.state_table)),
                (ns, snap.source, snap.scope, token, snap.snapshot_id),
            )

    def key_conflicts(
        self, ns: str, source: str, scope: str, refs: Sequence[Ref], limit: int
    ) -> tuple[list[dict[str, str]], bool]:
        """Сущности ``refs``, открытые в namespace версией другого ``(source, scope)``:
        до ``limit`` штук (по виду, ключу, источнику) и признак обрезки."""
        limit = max(0, int(limit))
        if not refs:
            return [], False
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT kind, item_key, source, scope FROM {t} WHERE namespace=%s "
                    "AND item_type='entity' AND valid_to IS NULL AND item_key = ANY(%s) "
                    "AND (kind, item_key) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
                    "AND NOT (source=%s AND scope=%s) "
                    "ORDER BY kind, item_key, source, scope LIMIT %s"
                ).format(t=self._t(self.items_table)),
                (
                    ns,
                    sorted({r.key for r in refs}),
                    [r.kind for r in refs],
                    [r.key for r in refs],
                    source,
                    scope,
                    limit + 1,
                ),
            )
            rows = cur.fetchall()
        items = [{"kind": k, "key": key, "source": src, "scope": sc} for k, key, src, sc in rows]
        return items[:limit], len(items) > limit

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
        ref_to, opened_apply)``."""
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    "INSERT INTO {t} (namespace, source, scope, item_type, kind, item_key, "
                    "version_id, content_hash, graph_ref, payload, valid_from, opened_in, "
                    "ref_from, ref_to, opened_apply) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
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

    def open_entity_versions(
        self,
        ns: str,
        kinds: Sequence[str],
        keys: Sequence[str] | None = None,
        *,
        rank: SourceRank | None = None,
    ) -> list[dict[str, Any]]:
        """Открытое состояние сущностей видов ``kinds`` (опц. только ключей ``keys``).

        По одной строке на ``(kind, key)``: если сущность держат открытой несколько
        источников, их версии сводятся (``domain/merge.py``, MEM-ADR-022; ``rank`` —
        приоритет источников). Для индекса сущностей и узлов графа сверки, поэтому
        сводятся только версии одного класса видимости (``node_versions``): узел и
        строка индекса видимость версий не расширяют.
        """
        if not kinds or (keys is not None and not keys):
            return []
        cond = ["namespace = %s", "item_type = 'entity'", "valid_to IS NULL", "kind = ANY(%s)"]
        args: list[Any] = [ns, sorted(set(kinds))]
        if keys is not None:
            cond.append("item_key = ANY(%s)")
            args.append(sorted(set(keys)))
        query = (
            f"SELECT {self._ENTITY_COLS} FROM {{t}} WHERE "
            + " AND ".join(cond)
            + " ORDER BY kind, item_key, id"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL(query).format(t=self._t(self.items_table)), args)
            rows = [_entity_row(r) for r in cur.fetchall()]
        return list(merge_by_entity(rows, rank, node=True).values())

    def open_entity_state(
        self, ns: str, refs: Sequence[Ref], rank: SourceRank | None = None
    ) -> dict[Ref, dict[str, Any]]:
        """Сведение ВСЕХ открытых версий сущностей ``refs`` (любых классов видимости).

        Состояние сущности целиком — для проверки ``required`` вида (MEM-ADR-022).
        """
        if not refs:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    f"SELECT {self._ENTITY_COLS} FROM {{t}} WHERE namespace = %s "
                    "AND item_type = 'entity' AND valid_to IS NULL AND kind = ANY(%s) "
                    "AND item_key = ANY(%s) ORDER BY kind, item_key, id"
                ).format(t=self._t(self.items_table)),
                (ns, sorted({r.kind for r in refs}), sorted({r.key for r in refs})),
            )
            rows = [_entity_row(r) for r in cur.fetchall()]
        wanted = set(refs)
        return {
            Ref(kind, key): row
            for (_, kind, key), row in merge_by_entity(rows, rank).items()
            if Ref(kind, key) in wanted
        }

    def open_entity_refs(self, ns: str) -> list[Ref]:
        """Все сущности namespace, открытые хоть одним источником (по виду и ключу)."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT DISTINCT kind, item_key FROM {t} WHERE namespace=%s "
                    "AND item_type='entity' AND valid_to IS NULL ORDER BY kind, item_key"
                ).format(t=self._t(self.items_table)),
                (ns,),
            )
            return [Ref(kind, key) for kind, key in cur.fetchall()]

    def namespaces(self) -> list[str]:
        """Namespaces, у которых есть версии в журнале."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT DISTINCT namespace FROM {t} ORDER BY namespace").format(
                    t=self._t(self.items_table)
                )
            )
            return [str(r[0]) for r in cur.fetchall()]

    def entity_page(
        self,
        namespaces: Sequence[str],
        kinds: Sequence[str],
        *,
        as_of: str = "",
        allowed_scopes: Sequence[str] | None = None,
        after: tuple[str, str, str] | None = None,
        limit: int = 200,
        rank: SourceRank | None = None,
    ) -> list[dict[str, Any]]:
        """Страница сущностей видов ``kinds`` на ``as_of`` (пусто — открытые).

        По одной на ``(kind, key, namespace)`` — сведение версий, валидных на момент и
        видимых вызывающему (сущность могут держать несколько источников; MEM-ADR-022,
        ``rank`` — приоритет источников). Порядок — ``(kind, key, namespace)`` побайтно
        (``COLLATE "C"``, не зависит от локали базы); ``after`` — позиция курсора,
        строки строго после неё. Для перечня сущностей (``context/entities.py``).
        """
        if not kinds or not namespaces:
            return []
        cond = ["item_type = 'entity'", "namespace = ANY(%s)", "kind = ANY(%s)"]
        args: list[Any] = [list(namespaces), sorted(set(kinds))]
        if as_of:
            cond.append("valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)")
            args += [as_of, as_of]
        else:
            cond.append("valid_to IS NULL")
        if allowed_scopes is not None:
            cond.append(observation_visibility_sql("coalesce(payload->'scopes', '[]'::jsonb)"))
            args.append(list(allowed_scopes))
        order = 'kind COLLATE "C", item_key COLLATE "C", namespace COLLATE "C"'
        if after is not None:
            cond.append(f"({order}) > (%s, %s, %s)")
            args += list(after)
        # Сначала позиции страницы (по одной на сущность), затем все их версии теми же
        # условиями: сведение видит каждую версию, валидную и видимую вызывающему.
        where = " AND ".join(cond)
        page = (
            f"SELECT DISTINCT ON ({order}) kind, item_key, namespace FROM {{t}} WHERE "
            + where
            + f" ORDER BY {order} LIMIT %s"
        )
        versions = (
            f"SELECT {self._ENTITY_COLS} FROM {{t}} WHERE "
            + where
            + " AND (kind, item_key, namespace) IN "
            "(SELECT * FROM unnest(%s::text[], %s::text[], %s::text[])) ORDER BY id"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL(page).format(t=self._t(self.items_table)), (*args, int(limit)))
            idents = [(str(k), str(key), str(ns)) for k, key, ns in cur.fetchall()]
            if not idents:
                return []
            cur.execute(
                sql.SQL(versions).format(t=self._t(self.items_table)),
                (*args, *(list(col) for col in zip(*idents, strict=True))),
            )
            rows = [_entity_row(r) for r in cur.fetchall()]
        merged = merge_by_entity(rows, rank)
        return [merged[(ns, kind, key)] for kind, key, ns in idents if (ns, kind, key) in merged]

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


def node_row(merged: dict[str, Any]) -> dict[str, Any]:
    """Строка ``GraphStore.upsert_entities`` из сведения версий сущности (MEM-ADR-022).

    ``props`` узла — сведённые атрибуты всех источников и служебные ключи сверки:
    ``aliases`` и ``scopes`` — объединение, ``provenance`` и ``snapshot`` — старшей версии.
    """
    payload = merged["payload"]
    props: dict[str, Any] = dict(payload.get("attributes") or {})
    if payload.get("aliases"):
        props["aliases"] = list(payload["aliases"])
    if payload.get("provenance"):
        props["provenance"] = payload["provenance"]
    if payload.get("scopes"):
        props["scopes"] = list(payload["scopes"])
    props["snapshot"] = {
        "source": merged["source"],
        "scope": merged["scope"],
        "snapshot_id": merged["snapshot_id"],
    }
    return {
        "k": merged["key"],
        "t": str(payload.get("title") or merged["key"]),
        "sp": str(payload.get("source_path") or ""),
        "props": props,
    }


def write_merged_nodes(
    graph: GraphStore,
    ledger: SnapshotLedger,
    ns: str,
    catalog: KindCatalog,
    refs: Sequence[Ref],
    valid_from: str,
    *,
    existing_only: Iterable[Ref] = (),
) -> int:
    """Записать в узлы графа сведение открытых версий сущностей ``refs``; вернуть число узлов.

    Свойства узла переписываются целиком — но сведением всех источников, а не снимком
    одного: атрибут другого источника не теряется. Сущность без открытых версий не
    трогается (её закрывает сверка). Сущности ``existing_only`` пишутся, только если
    узел уже есть: пересведение не воскрешает узел, удалённый через API.
    """
    wanted = set(refs)
    if not wanted:
        return 0
    check: dict[str, list[str]] = {}
    for ref in set(existing_only) & wanted:
        check.setdefault(ref.kind, []).append(ref.key)
    for kind, keys in sorted(check.items()):
        present = graph.existing_keys(kind, sorted(keys), ns)
        wanted -= {Ref(kind, key) for key in keys if key not in present}
    merged = ledger.open_entity_versions(
        ns,
        sorted({r.kind for r in wanted}),
        sorted({r.key for r in wanted}),
        rank=rank_by_catalog(catalog),
    )
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in merged:
        if Ref(row["kind"], row["key"]) in wanted:
            by_kind.setdefault(row["kind"], []).append(node_row(row))
    now = _now_iso()
    for kind in sorted(by_kind):
        graph.upsert_entities(kind, by_kind[kind], ns, valid_from=valid_from, last_seen=now)
    return sum(len(rows) for rows in by_kind.values())


def valid_at(valid_from: str | None, valid_to: str | None, as_of: str) -> bool:
    """Интервал [valid_from, valid_to) содержит момент; пустой as_of — «сейчас» (открыт)."""
    if not as_of:
        return not valid_to
    if valid_from and valid_from > as_of:
        return False
    return not valid_to or valid_to > as_of


CHANGE_LISTS = ("opened", "changed", "closed")


def changes_payload(keys: dict[str, list[Ref]], limit: int) -> dict[str, Any]:
    """Ключи узлов, тронутых сверкой: по ``limit`` в списке (сортировка по виду и ключу).

    ``truncated`` — хоть один список обрезан; полные числа — в счётчиках ``entities``.
    """
    limit = max(0, int(limit))
    out: dict[str, Any] = {}
    truncated = False
    for name in CHANGE_LISTS:
        refs = sorted(keys.get(name, ()), key=lambda r: (r.kind, r.key))
        truncated = truncated or len(refs) > limit
        out[name] = [r.to_payload() for r in refs[:limit]]
    return {**out, "limit": limit, "truncated": truncated}


def conflicts_payload(items: list[dict[str, str]], limit: int, truncated: bool) -> dict[str, Any]:
    """Конфликты естественного ключа с другими ``(source, scope)`` (FR-009): сведения."""
    return {"items": items, "limit": max(0, int(limit)), "truncated": truncated}


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
        apply_no: int = 1,
    ):
        self.ns = ns
        self.snap = snap
        self.apply_no = apply_no
        self.scopes = scopes
        self.catalog = catalog
        self.graph = graph
        self.facts = facts
        self.ledger = ledger
        self.ent = _counters()
        self.rel = _counters("pending", "resolved", "retried")
        self.keys: dict[str, list[Ref]] = {name: [] for name in CHANGE_LISTS}
        self.default_sp = f"snapshot:{snap.source}/{snap.snapshot_id}"

    def _version_id(self, item_type: str, kind: str, item_key: str) -> str:
        s = self.snap
        parts = [self.ns, s.source, s.scope, item_type, kind, item_key, s.snapshot_id]
        if self.apply_no > 1:  # как у fact_id: первое применение — прежние id
            parts.append(str(self.apply_no))
        return "ver-" + _digest(parts)[:24]

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
            self.apply_no,
        )

    # --- сущности ---

    def entities(self, open_items: dict[tuple[str, str, str], LedgerItem]) -> list[Ref]:
        """Сверить сущности; вернуть все сущности снимка (открытые и подтверждённые).

        По ним перепроверяются отложенные связи других источников: конец мог
        вернуться узлом мимо сверки (запись агента) или новой версией этого же
        источника — а не только появиться этим снимком впервые.
        """
        s = self.snap
        touched: list[Ref] = []
        new_rows: list[tuple] = []
        closes: list[tuple[str, str, str | None, int]] = []
        for e in s.entities:
            content = {**e.content(), "scopes": self.scopes}
            h = _digest(content)
            row = open_items.pop(("entity", e.kind, e.key), None)
            if row is not None and row.content_hash == h:
                self.ent["unchanged"] += 1
                continue
            touched.append(e.ref)
            # since — с какого момента источник держит каждое значение (MEM-ADR-022):
            # хранится в версии, но в хэш содержимого не входит.
            stored = {
                **content,
                SINCE_KEY: stamp_since(
                    content,
                    row.payload if row is not None else None,
                    row.valid_from if row is not None else None,
                    s.observed_at,
                ),
            }
            new_row = self._row("entity", e.kind, e.key, stored, h, e.key)
            new_rows.append(new_row)
            self.ent["opened"] += 1
            if row is not None:
                closes.append((s.observed_at, s.snapshot_id, new_row[6], row.id))
                self.ent["closed"] += 1
                self.ent["superseded"] += 1
                self.keys["changed"].append(e.ref)
            else:
                self.keys["opened"].append(e.ref)

        vanished = [row for (itype, _, _), row in list(open_items.items()) if itype == "entity"]
        for row in vanished:
            open_items.pop(("entity", row.kind, row.item_key), None)
            closes.append((s.observed_at, s.snapshot_id, None, row.id))
            self.ent["closed"] += 1
            self.keys["closed"].append(Ref(row.kind, row.item_key))

        self.ledger.close_items(closes)
        self.ledger.insert_items(new_rows)
        self._check_required()
        # Узел закрывается, только если его не держит открытым другой источник; иначе
        # узел пересводится из оставшихся версий (атрибуты ушедшего источника уходят,
        # атрибуты других — остаются).
        gone = [Ref(r.kind, r.item_key) for r in vanished]
        still_open = self.ledger.open_entities(self.ns, gone)
        by_kind: dict[str, list[str]] = {}
        for ref in gone:
            if ref in still_open:
                touched.append(ref)
            else:
                by_kind.setdefault(ref.kind, []).append(ref.key)
        write_merged_nodes(
            self.graph,
            self.ledger,
            self.ns,
            self.catalog,
            touched,
            s.observed_at,
            existing_only=[r for r in gone if r in still_open],
        )
        for kind in sorted(by_kind):
            self.graph.close_entities(kind, by_kind[kind], self.ns, valid_to=s.observed_at)
        return [e.ref for e in s.entities]

    def _check_required(self) -> None:
        """``required`` вида — у сведения всех открытых версий, а не у вклада снимка.

        Вклад источника может быть частичным (CRM даёт ``{inn}``, имя — у учётной
        системы): типы и ``properties`` проверены при разборе, а обязательные атрибуты
        — здесь, после записи журнала в той же транзакции. Нет обязательного атрибута
        ни в одном источнике — ``AttributesError`` (как у атрибутов вне схемы, HTTP
        422), сверка откатывается целиком.
        """
        refs = [e.ref for e in self.snap.entities if self.catalog.required_attributes(e.kind)]
        if not refs:
            return
        state = self.ledger.open_entity_state(self.ns, refs, rank_by_catalog(self.catalog))
        errors: list[str] = []
        for ref in refs:
            row = state.get(ref)
            attrs = (row or {}).get("payload", {}).get("attributes") or {}
            missing = [n for n in self.catalog.required_attributes(ref.kind) if n not in attrs]
            if missing:
                errors.append(f"{ref.kind}:{ref.key!r} — нет {missing}")
        if errors:
            raise AttributesError(
                "Обязательные атрибуты не заданы ни одним источником сущности: "
                + "; ".join(errors[:10])
            )

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
            fid = fact_id_for_version(
                self.ns, s.source, s.scope, r.item_key, s.snapshot_id, self.apply_no
            )
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
                        self.ns,
                        row.source,
                        row.scope,
                        row.item_key,
                        row.opened_in,
                        row.opened_apply,
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
    embedder: Embedder | None = None,
    dry_run: bool = False,
    expected_state: str | None = None,
) -> dict[str, Any]:
    """Сверить документ снимка (формат SNAPSHOT.md) с памятью namespace; вернуть счётчики.

    ``scopes`` — scopes видимости (MEM-ADR-019), которые ядро выводит из задачи:
    пишутся в узлы и рёбра этого снимка (``props.scopes`` / ``r.scopes``).

    Сущности видов с ``searchable`` индексируются для поиска по смыслу в той же
    транзакции (``domain/searchable.py``): открытые и изменённые — с новым вектором,
    закрытые — снимаются. Ошибка эмбеддера откатывает сверку целиком — индекс не
    расходится с журналом. ``embedder`` — для тестов; по умолчанию — из настроек и
    только если есть текст к векторизации. ``dry_run`` индекс не трогает и эмбеддер не
    вызывает: откат транзакции не отменяет внешних вызовов, а план на большой снимок
    стоил бы сотен запросов к эмбеддингам.

    Ответ: ``{opened, closed, unchanged, superseded, entities{…}, relations{…,
    pending, resolved}, changes{opened, changed, closed, limit, truncated}, conflicts{items,
    limit, truncated}, source, scope, snapshot_id, observed_at, namespace, duplicate,
    dryRun, stateToken}``. Изменившийся элемент считается одновременно закрытым (старая
    версия) и открытым (новая) и попадает в ``superseded``; ``relations.pending`` —
    связи снимка, чей конец ещё не появился, ``relations.resolved`` — отложенные
    связи (свои и чужие), материализованные этой сверкой. ``changes`` — ключи
    ``{kind, key}`` узлов этой сверки: ``opened`` — новые для ``(source, scope)``,
    ``changed`` — изменившиеся (их же считает ``entities.superseded``), ``closed`` —
    пропавшие из снимка; не больше ``settings.reconcile_changes_limit`` в каждом списке.
    ``conflicts`` — сущности снимка, открытые в namespace версией другого
    ``(source, scope)`` (сведения, а не отказ). Журнал снимка хранит только счётчики:
    повтор (``duplicate``) возвращает их и пустые списки — эта сверка ничего не изменила.

    Амендмент MEM-ADR-020 2026-09-28: ``dry_run`` — та же сверка в транзакции, которая
    откатывается: ответ — план на текущем состоянии, ничего не записано.
    ``stateToken`` — отпечаток открытых версий пары ``(source, scope)``: после записи —
    новое состояние, у ``dry_run`` — то, на котором построен план. ``expected_state``,
    не совпавший с текущим токеном, — ``SnapshotStateMismatch`` (повтор последнего
    принятого ``snapshotId`` его не проверяет).

    Амендмент MEM-ADR-020 2026-09-30: ``duplicate`` — только повтор последнего принятого
    снимка пары; более ранний ``snapshotId`` применяется как новый (номер применения
    солит id версий и рёбер, чтобы не совпасть с закрытыми).
    """
    ns = resolve_namespace(namespace, settings.default_namespace)
    try:
        vis_scopes = resolve_scopes(scopes, limit=MAX_ITEM_SCOPES)
    except ValueError as exc:
        raise SnapshotError(f"scopes: {exc}") from exc
    limit = settings.reconcile_changes_limit

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
        # Индекс сущностей: есть индексируемые виды — таблица нужна; нет, но таблица
        # есть — сверка снимает из неё закрытое (вид мог быть индексируемым раньше).
        # Предпросмотр индекс не трогает: эмбеддер — внешний вызов, откат его не отменит.
        index = None
        if not dry_run:
            index = entity_index(conn, settings)
            if catalog.searchable_kinds():
                index.ensure_schema()
            elif not index.table_exists():
                index = None

        # Предпросмотр идёт тем же кодом, что и запись, но его транзакция откатывается:
        # граф, журнал, отложенные связи и запись снимка остаются как были.
        with conn.transaction(force_rollback=dry_run):
            with conn.cursor() as cur:
                # Весь namespace: отложенные связи разрешаются через границу источников.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (reconcile_lock_key(graph.graph, ns),),
                )
            state = ledger.state_token(ns, snap.source, snap.scope)
            # Дубликат — только повтор последнего принятого снимка пары: прежний
            # snapshotId после другого (A → B → A) — новое применение.
            last_accepted = ledger.last_accepted(ns, snap.source, snap.scope)
            if last_accepted is not None and last_accepted[0] == snap.snapshot_id:
                return {
                    **last_accepted[1],
                    "changes": changes_payload({}, limit),
                    "conflicts": conflicts_payload([], limit, False),
                    "duplicate": True,
                    "dryRun": dry_run,
                    "stateToken": state,
                }
            if expected_state is not None and expected_state != state:
                raise SnapshotStateMismatch(expected_state, state)
            last = ledger.last_observed_at(ns, snap.source, snap.scope)
            if last and snap.observed_at < last:
                raise StaleSnapshotError(
                    f"Снимок {snap.snapshot_id!r} ({snap.observed_at}) старше последнего "
                    f"принятого для {snap.source!r}/{snap.scope!r} ({last})"
                )
            conflicts, conflicts_cut = ledger.key_conflicts(
                ns, snap.source, snap.scope, [e.ref for e in snap.entities], limit
            )
            apply_no = ledger.next_apply_no(ns, snap.source, snap.scope, snap.snapshot_id)
            run = _Reconciler(ns, snap, vis_scopes, catalog, graph, facts, ledger, apply_no)
            open_items = ledger.open_items(ns, snap.source, snap.scope)
            seen = run.entities(open_items)
            run.relations(open_items, seen)
            result = run.result()
            if index is not None:
                touched = [ref for name in CHANGE_LISTS for ref in run.keys[name]]
                result["search_index"] = sync_entities(
                    index,
                    ledger,
                    ns,
                    catalog,
                    lambda: embedder or build_embedder(settings),
                    idents=[(r.kind, r.key) for r in touched],
                )
            if not dry_run:
                # Узлы сведены по каталогу этой сверки (прочитан до лока): другой
                # sourcePriority — отпечаток пересведения устарел (MEM-ADR-022, п. 5).
                registry.reset_stale_merge(ns, catalog.source_priorities())
                ledger.record_snapshot(ns, snap, result, actor, apply_no)
                state = ledger.compute_state_token(ns, snap.source, snap.scope)
                ledger.save_state(ns, snap, state)
        return {
            **result,
            "changes": changes_payload(run.keys, limit),
            "conflicts": conflicts_payload(conflicts, limit, conflicts_cut),
            "duplicate": False,
            "dryRun": dry_run,
            "stateToken": state,
        }
    finally:
        if own_conn:
            conn.close()


def ledger_for(settings: Settings, conn: psycopg.Connection) -> SnapshotLedger:
    return SnapshotLedger(conn, settings.snapshots_table)


MERGE_BATCH = 500


def merge_namespace_nodes(
    settings: Settings, conn: psycopg.Connection, ns: str, catalog: KindCatalog
) -> dict[str, int]:
    """Пересвести узлы всех открытых сущностей namespace из журнала (MEM-ADR-022).

    Миграция узлов, записанных до сведения (свойства — снимок последнего писавшего
    источника), и пересчёт после смены ``sourcePriority`` в пакетах namespace
    (``PUT …/kinds``). Идемпотентна; одной транзакцией под advisory-локом сверок
    namespace. Удаляет из узла только атрибуты, которых нет ни в одной открытой
    версии; узлы без журнала (наблюдения, записи API) не трогает.
    """
    ledger = SnapshotLedger(conn, settings.snapshots_table)
    if not ledger.table_exists():
        return {"nodes": 0}
    graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
    written = 0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (reconcile_lock_key(graph.graph, ns),),
            )
        refs = ledger.open_entity_refs(ns)
        for i in range(0, len(refs), MERGE_BATCH):
            batch = refs[i : i + MERGE_BATCH]
            written += write_merged_nodes(
                graph, ledger, ns, catalog, batch, _now_iso(), existing_only=batch
            )
    return {"nodes": written}


__all__ = [
    "SnapshotError",
    "SnapshotStateMismatch",
    "StaleSnapshotError",
    "SnapshotLedger",
    "changes_payload",
    "conflicts_payload",
    "merge_namespace_nodes",
    "parse_snapshot",
    "provenance_path",
    "reconcile",
    "valid_at",
]
