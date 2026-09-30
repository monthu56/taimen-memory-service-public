"""GraphStore — идемпотентный upsert и обход графа Apache AGE.

Свободные свойства frontmatter хранятся вложенной картой под ключом ``props`` (AGE не
принимает параметр-карту в ``SET n = $m``, но прекрасно принимает map-значение в
``SET n.props = $m``). Служебные поля (type/title/provenance) — отдельными скалярами.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import psycopg
from psycopg import sql as psql

from platform_memory.core import db as dbmod
from platform_memory.core.db import agtype
from platform_memory.core.models import Edge, Node, Provenance
from platform_memory.core.namespaces import resolve_namespace, resolve_namespaces
from platform_memory.core.ontology import sanitize_label
from platform_memory.core.scopes import (
    ForeignObjectError,
    check_write_scopes,
    is_visible,
    merge_scopes,
    node_scopes,
    trace_node_scopes,
)


def _jsonable(value: Any) -> Any:
    """Привести значение к JSON/agtype-совместимому виду (даты -> ISO-строки и т.п.)."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _gid_nodes(rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    """Строки (graphid, свойства) -> свойства узла с ``_gid``."""
    out: list[dict[str, Any]] = []
    for gid, props in rows:
        node = dbmod.parse_agtype(props)
        if isinstance(node, dict):
            node["_gid"] = int(str(dbmod.parse_agtype(gid)))
            out.append(node)
    return out


def _edge_valid(rel: dict[str, Any], as_of: str) -> bool:
    """Ребро проходимо на ``as_of``: без ``evidence_lost``, интервал содержит момент.

    Пусто — действующее сейчас (``valid_to`` не задан); ребро без ``valid_from``
    (структурные связи vault) валидно с начала времён.
    """
    if rel.get("evidence_lost") is True:
        return False
    valid_to = rel.get("valid_to")
    if not as_of:
        return valid_to is None
    valid_from = rel.get("valid_from")
    if valid_from not in (None, "") and not str(valid_from) <= as_of:
        return False
    return valid_to is None or str(valid_to) > as_of


# Типы служебного аудит-контура: их нельзя удалять механизмом delete_node,
# иначе самим механизмом удаления можно стереть след удаления.
PROTECTED_TYPES = frozenset({"audit_event", "pc_trace"})


def assert_deletable(node_type: str) -> None:
    """Проверить, что тип узла разрешён к удалению; аудит-контур защищён (ValueError)."""
    if node_type in PROTECTED_TYPES:
        raise ValueError(f"Узлы типа '{node_type}' защищены от удаления (аудит-контур)")


# Метки, у которых индексы сверки (``GraphStore.ensure_label_index``) уже есть и
# закоммичены: (БД, граф, метка, ребро?). На процесс; индексы не удаляются, кроме как
# вместе с графом (``forget_label_indexes``).
_READY_LABEL_INDEXES: set[tuple[Any, ...]] = set()

# Предел длины идентификатора PostgreSQL — 63 байта; самый длинный суффикс — 13.
_INDEX_LABEL_MAX = 48


def _db_key(conn: Any) -> tuple[Any, ...]:
    info = getattr(conn, "info", None)
    return (getattr(info, "host", ""), getattr(info, "port", ""), getattr(info, "dbname", ""))


def _label_index_names(label: str, *, edge: bool) -> list[str]:
    """Имена индексов метки: GIN по свойствам, у узлов — hash по ``natural_key``, у
    рёбер — B-tree по ``start_id`` и ``end_id``.

    Метка до 48 символов — как есть (имена совпадают с уже созданными на действующих
    графах). Длиннее — префикс 38 символов и 10 hex sha1 полного имени: две метки с
    общим префиксом получают разные индексы, а длина основы (49) не совпадает ни с
    одной короткой меткой.
    """
    base = label
    if len(label) > _INDEX_LABEL_MAX:
        base = f"{label[:38]}_{hashlib.sha1(label.encode()).hexdigest()[:10]}"
    names = [f"{base}_pm_props_gin"]
    names += [f"{base}_pm_start", f"{base}_pm_end"] if edge else [f"{base}_pm_nk_hash"]
    return names


def forget_label_indexes(conn: Any, graph: str) -> None:
    """Сбросить кэш индексов меток графа (граф удалён)."""
    db = _db_key(conn)
    for key in [k for k in _READY_LABEL_INDEXES if k[0] == db and k[1] == graph]:
        _READY_LABEL_INDEXES.discard(key)


class GraphStore:
    """Обёртка над графом AGE: схема, upsert узлов/рёбер, обход, статистика."""

    def __init__(self, conn: psycopg.Connection, graph_name: str, default_namespace: str = ""):
        """Сохранить соединение, санитизировать имя графа, зафиксировать default-namespace."""
        self.conn = conn
        # graph_name подставляется в cypher() f-строкой -> санитизируем (whitelist [A-Za-z0-9_]).
        self.graph = sanitize_label(graph_name)
        self.default_namespace = resolve_namespace(default_namespace)
        # Проверка вида сущности на путях записи агента (MEM-ADR-020): callable
        # (namespace, kind, natural_key, attributes) -> каноническое имя вида или
        # UnknownKindError. None — прежнее поведение (любой вид). Подключает
        # ``domain.registry.attach_kind_guard``.
        self.kind_guard: Callable[[str, str, str, dict[str, Any] | None], str] | None = None

    def _ns(self, namespace: str) -> str:
        """Разрешить namespace записи: пустой -> default стора."""
        return resolve_namespace(namespace, self.default_namespace)

    def _nss(self, namespaces: Sequence[str]) -> list[str]:
        """Разрешить список namespaces чтения: пустой -> [default стора]."""
        return resolve_namespaces(namespaces, self.default_namespace)

    # --- схема ---

    def ensure_schema(self) -> None:
        """Создать граф, если его ещё нет, и backfill'ить namespace на pre-namespace данных.

        Backfill идемпотентен: узлы без ``namespace`` получают default-namespace
        (существующие данные Nexus/Company Brain), повторный запуск ничего не меняет.
        """
        dbmod.ensure_graph(self.conn, self.graph)
        p = agtype({"ns": self.default_namespace})
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
                f"WHERE n.namespace IS NULL SET n.namespace = $ns $$, %s) AS (v agtype)",
                (p,),
            )

    def reset(self) -> None:
        """Удалить граф и создать его заново (полная очистка)."""
        dbmod.drop_graph(self.conn, self.graph)
        forget_label_indexes(self.conn, self.graph)
        dbmod.ensure_graph(self.conn, self.graph)

    # --- upsert ---

    def _serialize_upsert(self, key: str) -> None:
        """Взять advisory-лок на ключ сущности до конца текущей транзакции.

        AGE падает с ``Entity failed to be updated`` при конкурентном MERGE одной
        и той же сущности (например, два параллельных audit-события одного trace_id
        апсертят общий якорь pc_trace). Лок сериализует такие апсерты.
        Вызывать только внутри явной транзакции (``with conn.transaction()``).
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    # Остаточные гонки AGE, безопасные для повтора: конкурентное автосоздание
    # label-таблицы при первом MERGE нового типа (DuplicateTable/UniqueViolation)
    # и конкурентный апдейт сущности (InternalError 'Entity failed to be updated').
    _RETRYABLE = (
        psycopg.errors.DuplicateTable,
        psycopg.errors.UniqueViolation,
        psycopg.errors.InternalError_,
    )

    def _locked_upsert(
        self,
        lock_key: str,
        sql: str,
        params: dict[str, Any],
        prepare: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """Выполнить upsert-запрос под advisory-локом, с повтором retryable-гонок AGE.

        ``prepare`` вызывается под тем же локом до запроса и может поправить ``params``
        по текущему состоянию узла (или отказать исключением) без гонки чтения и записи.
        """
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                with self.conn.transaction():
                    self._serialize_upsert(lock_key)
                    if prepare is not None:
                        prepare(params)
                    with self.conn.cursor() as cur:
                        cur.execute(sql, (agtype(params),))
                        return cur.fetchone()
            except self._RETRYABLE:
                if attempt == attempts:
                    raise
        return None  # недостижимо; для mypy

    def upsert_node(
        self,
        node: Node,
        *,
        guard_scopes: bool = False,
        allowed_scopes: Sequence[str] | None = None,
    ) -> None:
        """Идемпотентно создать/обновить узел по (namespace, natural_key).

        ``guard_scopes`` — запись через API (MEM-ADR-019): существующий узел с тем же
        ключом вне видимости ``allowed_scopes`` (None — без ограничения) не
        перезаписывается — :class:`ForeignObjectError`; ``props.scopes`` видимого
        узла сливаются по :func:`merge_scopes`, а не заменяются. Без флага —
        прежняя замена ``props`` целиком (ингест vault, служебные узлы).
        """
        label = sanitize_label(node.type)
        prov = node.provenance
        ns = self._ns(node.namespace)
        params = {
            "nk": node.natural_key,
            "ns": ns,
            "type": node.type or "entity",
            "title": node.title or node.natural_key,
            "sp": prov.source_path if prov else None,
            "sid": prov.source_id if prov else None,
            "conf": prov.confidence if prov else None,
            "ls": prov.last_seen if prov else None,
            "origin": node.origin or "vault",
            "props": _jsonable(dict(node.properties)),
        }
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MERGE (n:{label} {{natural_key: $nk, namespace: $ns}}) "
            f"SET n.type=$type, n.title=$title, n.source_path=$sp, n.source_id=$sid, "
            f"n.confidence=$conf, n.last_seen=$ls, n.origin=$origin, n.props=$props "
            f"RETURN id(n) $$, %s) AS (id agtype)"
        )
        prepare = None
        if guard_scopes:

            def prepare(p: dict[str, Any]) -> None:
                p["props"] = self._guarded_props(node.natural_key, ns, p["props"], allowed_scopes)

        self._locked_upsert(f"{self.graph}:n:{label}:{ns}:{node.natural_key}", sql, params, prepare)

    def _guarded_props(
        self,
        natural_key: str,
        ns: str,
        props: dict[str, Any],
        allowed_scopes: Sequence[str] | None,
    ) -> dict[str, Any]:
        """``props`` записи со слитыми scopes; отказ, если узел с ключом чужой или
        запись несёт scope видимости не из ``allowed_scopes`` (:func:`check_write_scopes`).

        Учитываются все узлы ключа в namespace, какого бы вида они ни были: чтение и
        удаление по ключу вид не учитывают, а запись другим видом создаёт второй узел
        с тем же ключом — он наследует scopes прежних, а не становится общим.
        """
        existing = self._nodes_with_key(natural_key, ns)
        if any(not is_visible(node_scopes(n), allowed_scopes) for n in existing):
            raise ForeignObjectError(natural_key)
        incoming = node_scopes({"props": props})
        check_write_scopes(incoming, allowed_scopes)
        before = [s for n in existing for s in node_scopes(n)] if existing else None
        merged = merge_scopes(before, incoming)
        out = {k: v for k, v in props.items() if k != "scopes"}
        if merged:
            out["scopes"] = merged
        return out

    def key_visible(
        self, natural_key: str, namespace: str = "", allowed_scopes: Sequence[str] | None = None
    ) -> bool:
        """Видны ли ``allowed_scopes`` все узлы ключа в namespace (нет узлов — True)."""
        if allowed_scopes is None:
            return True
        existing = self._nodes_with_key(natural_key, self._ns(namespace))
        return all(is_visible(node_scopes(n), allowed_scopes) for n in existing)

    def _nodes_with_key(self, natural_key: str, ns: str) -> list[dict[str, Any]]:
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (n {{natural_key: $nk}}) WHERE n.namespace = $ns RETURN properties(n) "
            f"$$, %s) AS (props agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype({"nk": natural_key, "ns": ns}),))
            rows = cur.fetchall()
        return [dbmod.parse_agtype(r[0]) for r in rows]

    # --- батчевые операции сверки снимков (MEM-ADR-020) ---

    # Выражение, в которое AGE транслирует ``n.natural_key`` в WHERE; индекс по нему
    # подхватывается только при текстуальном совпадении.
    _NATURAL_KEY_EXPR = (
        "ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties, "
        "'\"natural_key\"'::ag_catalog.agtype])"
    )

    def ensure_label_index(self, label: str, *, edge: bool = False, create: bool = True) -> bool:
        """Создать метку (если нет) и индексы её таблицы; вернуть, есть ли метка.

        GIN по ``properties``: шаблон ``(n:label {natural_key: …, namespace: …})`` и
        ``[r:REL {fact_id: …}]`` AGE транслирует в ``properties @> {...}`` — поиск
        узла в MERGE и ребра по fact_id идёт index scan'ом, в том числе внутри UNWIND.

        У меток узлов ещё hash-индекс по ``natural_key``: для выборок по списку ключей
        (``existing_keys``/``close_entities``, ``UNWIND $keys … WHERE n.natural_key = k``).
        GIN здесь не помогает — вхождение ``namespace`` общее у всех узлов, и каждый
        поиск читает его posting list целиком; ``IN $keys`` индекс не использует вовсе.
        Hash, а не B-tree: у B-tree предел размера записи (~2.7 КБ), длина ключа не
        ограничена. У меток рёбер — B-tree по ``start_id`` и ``end_id``: шаг typed-обхода
        выбирает рёбра фронта по концам (``typed_neighbors``) без просмотра всей таблицы
        связи; на действующих графах индексы появляются при следующей записи фактов этой
        связи. ``create=False`` — метку не создавать (чтение): False, если её нет.
        Вызывать внутри транзакции (advisory-лок на метку).

        «Индексы метки есть» кэшируется на процесс: повторный вызов не ходит в БД и не
        выполняет ``CREATE INDEX IF NOT EXISTS`` (тот берёт ShareLock таблицы метки до
        конца транзакции сверки и сериализует параллельные сверки по одной метке). В
        кэш попадают только закоммиченные индексы, см. ``_label_indexes_committed``.
        """
        label = sanitize_label(label)
        names = _label_index_names(label, edge=edge)
        key = (_db_key(self.conn), self.graph, label, edge)
        if key in _READY_LABEL_INDEXES:
            return True
        tbl = psql.Identifier(self.graph, label)
        with self.conn.cursor() as cur:
            if self._label_indexes_committed(cur, names):
                _READY_LABEL_INDEXES.add(key)
                return True
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{self.graph}:label:{label}",),
            )
            cur.execute(
                "SELECT 1 FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g "
                "ON g.graphid = l.graph WHERE g.name = %s AND l.name = %s",
                (self.graph, label),
            )
            if cur.fetchone() is None:
                if not create:
                    return False
                fn = "create_elabel" if edge else "create_vlabel"
                cur.execute(f"SELECT ag_catalog.{fn}(%s, %s)", (self.graph, label))
            cur.execute(
                psql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {tbl} USING gin (properties)").format(
                    idx=psql.Identifier(names[0]), tbl=tbl
                )
            )
            if not edge:
                cur.execute(
                    psql.SQL(
                        "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} USING hash ("
                        + self._NATURAL_KEY_EXPR
                        + ")"
                    ).format(idx=psql.Identifier(names[1]), tbl=tbl)
                )
            else:
                for idx, col in zip(names[1:], ("start_id", "end_id"), strict=True):
                    cur.execute(
                        psql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {tbl} ({col})").format(
                            idx=psql.Identifier(idx), tbl=tbl, col=psql.Identifier(col)
                        )
                    )
        return True

    def _label_indexes_committed(self, cur: Any, names: Sequence[str]) -> bool:
        """Все индексы метки есть в каталоге и созданы не текущей транзакцией.

        Индекс, созданный своей незакоммиченной транзакцией (или её savepoint), виден
        в каталоге, но может откатиться — кэшировать его нельзя. Такой индекс
        отличает блокировка: ``CREATE INDEX`` держит AccessExclusiveLock на новом
        индексе до конца транзакции. Индекс без блокировки нашего backend'а —
        закоммичен (или прочитан нами в этой же транзакции: тогда кэш заполнится
        следующим вызовом).
        """
        cur.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = ANY(%s) AND NOT EXISTS ("
            "SELECT 1 FROM pg_locks l WHERE l.relation = c.oid AND l.pid = pg_backend_pid())",
            (self.graph, list(names)),
        )
        row = cur.fetchone()
        return bool(row) and int(row[0]) == len(names)

    def upsert_entities(
        self,
        kind: str,
        rows: Sequence[dict[str, Any]],
        namespace: str,
        *,
        valid_from: str,
        last_seen: str,
    ) -> int:
        """Батч-upsert узлов одного вида (UNWIND): ``rows`` — ``{k, t, sp, props}``.

        Узел открывается (``valid_to`` снимается), ``valid_from`` ставится при первом
        появлении. ``origin='agent'`` — запись агента, как у FactStore.ensure_entity.
        """
        if not rows:
            return 0
        label = sanitize_label(kind)
        self.ensure_label_index(label)
        params = {
            "rows": [_jsonable(dict(r)) for r in rows],
            "ns": self._ns(namespace),
            "type": kind or "entity",
            "vf": valid_from,
            "ls": last_seen,
        }
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"UNWIND $rows AS row "
            f"MERGE (n:{label} {{natural_key: row.k, namespace: $ns}}) "
            f"SET n.type=$type, n.title=row.t, n.source_path=row.sp, n.confidence=1.0, "
            f"n.last_seen=$ls, n.origin='agent', n.props=row.props, "
            f"n.valid_from=coalesce(n.valid_from, $vf), n.valid_to=null "
            f"RETURN count(n) $$, %s) AS (c agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            row = cur.fetchone()
        return int(dbmod.parse_agtype(row[0]) or 0) if row else 0

    def close_entities(
        self, kind: str, natural_keys: Sequence[str], namespace: str, *, valid_to: str
    ) -> int:
        """Закрыть интервал валидности узлов одного вида (узлы не удаляются).

        Выборка — ``UNWIND`` + равенство по ключу (индекс, см. ``ensure_label_index``).
        """
        keys = [k for k in dict.fromkeys(natural_keys) if k]
        if not keys:
            return 0
        label = sanitize_label(kind)
        if not self.ensure_label_index(label, create=False):
            return 0
        params = {"keys": keys, "ns": self._ns(namespace), "vt": valid_to}
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"UNWIND $keys AS k MATCH (n:{label}) WHERE n.natural_key = k AND n.namespace = $ns "
            f"SET n.valid_to = $vt RETURN count(n) $$, %s) AS (c agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            row = cur.fetchone()
        return int(dbmod.parse_agtype(row[0]) or 0) if row else 0

    def existing_keys(self, kind: str, natural_keys: Sequence[str], namespace: str) -> set[str]:
        """Какие из ключей есть узлами вида ``kind`` в namespace (любой интервал).

        ``UNWIND $keys`` + ``n.natural_key = k``, а не ``n.natural_key IN $keys``: IN
        AGE исполняет перебором списка на каждой строке метки (снимок ~1000 ключей на
        ~30k узлов — секунды), равенство даёт hash join или index scan по hash-индексу
        ключа (``ensure_label_index``) — миллисекунды.
        """
        keys = [k for k in dict.fromkeys(natural_keys) if k]
        if not keys:
            return set()
        label = sanitize_label(kind)
        if not self.ensure_label_index(label, create=False):
            return set()  # метки нет — узлов этого вида нет
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"UNWIND $keys AS k MATCH (n:{label}) WHERE n.natural_key = k AND n.namespace = $ns "
            f"RETURN DISTINCT n.natural_key $$, %s) AS (k agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype({"keys": keys, "ns": self._ns(namespace)}),))
            rows = cur.fetchall()
        return {str(dbmod.parse_agtype(r[0])) for r in rows}

    def upsert_nodes(self, nodes: Iterable[Node]) -> int:
        """Upsert набора узлов; вернуть количество обработанных."""
        return sum(1 for node in nodes if (self.upsert_node(node) or True))

    def upsert_edge(self, edge: Edge, last_seen: str | None = None) -> bool:
        """Создать/обновить ребро между существующими узлами. False — если узлы не найдены.

        Оба конца ребра ищутся в ОДНОМ namespace (v1: рёбра не пересекают namespaces).
        """
        etype = sanitize_label(edge.type).upper()
        ns = self._ns(edge.namespace)
        params = {
            "src": edge.src,
            "dst": edge.dst,
            "ns": ns,
            "sp": edge.source_path,
            "ls": last_seen,
            "origin": edge.origin or "vault",
            "props": _jsonable(dict(edge.properties)),
        }
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (a {{natural_key: $src, namespace: $ns}}), "
            f"(b {{natural_key: $dst, namespace: $ns}}) "
            f"MERGE (a)-[r:{etype}]->(b) "
            f"SET r.source_path=$sp, r.last_seen=$ls, r.origin=$origin, r.data=$props "
            f"RETURN id(r) $$, %s) AS (id agtype)"
        )
        row = self._locked_upsert(
            f"{self.graph}:e:{etype}:{ns}:{edge.src}->{edge.dst}", sql, params
        )
        return row is not None

    def upsert_edges(self, edges: Iterable[Edge], last_seen: str | None = None) -> tuple[int, int]:
        """Вернуть (создано/обновлено, пропущено из-за отсутствующих узлов)."""
        ok = skipped = 0
        for edge in edges:
            if self.upsert_edge(edge, last_seen):
                ok += 1
            else:
                skipped += 1
        return ok, skipped

    def prune(self, run_token: str, namespace: str = "") -> tuple[int, int]:
        """Mark-and-sweep: удалить ТОЛЬКО vault-узлы/рёбра namespace, не виденные в прогоне.

        Делает полный ingest самоочищающимся (удалённые файлы, исчезнувшие связи,
        «осиротевшие» при смене типа узлы). Скоупится одним namespace — ре-ingest vault
        никогда не подметает данные других namespaces (тенантские данные).
        Узлы/рёбра origin='agent' (write-back агентов) НЕ подметаются. Возвращает
        (узлов, рёбер).
        """
        ns = self._ns(namespace)
        p = agtype({"run": run_token, "ns": ns})
        # vault-происхождение: origin отсутствует (старые) или = 'vault'.
        # Namespace рёбер выражается через исходный узел (рёбра не пересекают namespaces).
        stale_edge = (
            "a.namespace = $ns "
            "AND (r.last_seen IS NULL OR r.last_seen <> $run) "
            "AND (r.origin IS NULL OR r.origin = 'vault')"
        )
        stale_node = (
            "n.namespace = $ns "
            "AND (n.last_seen IS NULL OR n.last_seen <> $run) "
            "AND (n.origin IS NULL OR n.origin = 'vault')"
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (a)-[r]->() "
                f"WHERE {stale_edge} RETURN count(*) $$, %s) AS (c agtype)",
                (p,),
            )
            edges = int(dbmod.parse_agtype(cur.fetchone()[0]))
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (a)-[r]->() "
                f"WHERE {stale_edge} DELETE r $$, %s) AS (v agtype)",
                (p,),
            )
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
                f"WHERE {stale_node} RETURN count(*) $$, %s) AS (c agtype)",
                (p,),
            )
            nodes = int(dbmod.parse_agtype(cur.fetchone()[0]))
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
                f"WHERE {stale_node} DETACH DELETE n $$, %s) AS (v agtype)",
                (p,),
            )
        return nodes, edges

    # --- write-back (факты авторства агента) ---

    def write_fact(
        self,
        natural_key: str,
        node_type: str,
        title: str,
        *,
        properties: dict[str, Any] | None = None,
        links: list[str] | None = None,
        run_id: str | None = None,
        confidence: float = 0.8,
        source: str | None = None,
        trace_id: str | None = None,
        namespace: str = "",
        allowed_scopes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Записать факт авторства агента (origin='agent'). Переживает ре-ingest (не подметается).

        Перезапись существующего узла проверяет видимость ``allowed_scopes`` и сливает
        ``props.scopes`` (``upsert_node(guard_scopes=True)``, MEM-ADR-019); ``links`` к
        невидимым узлам не создаются, как к отсутствующим.

        Замыкает цикл «mini-app читают И пишут в Brain». Рёбра LINKS_TO создаются к уже
        существующим узлам-целям. Если задан ``trace_id`` — факт привязывается ребром
        ``IN_TRACE`` к якорю трейса задачи (recall→действия→retain в одном подграфе).
        Запись в vault при этом НЕ производится.
        """
        now = dt.datetime.now().isoformat()
        src = source or f"agent:run/{run_id or '-'}"
        ns = self._ns(namespace)
        if self.kind_guard is not None:
            node_type = self.kind_guard(ns, node_type, natural_key, properties)
        node = Node(
            type=node_type,
            natural_key=natural_key,
            title=title,
            properties=properties or {},
            origin="agent",
            namespace=ns,
            provenance=Provenance(
                source_path=src, source_id=run_id, confidence=confidence, last_seen=now
            ),
        )
        self.upsert_node(node, guard_scopes=True, allowed_scopes=allowed_scopes)
        edges_made = 0
        for dst in links or []:
            if not self.key_visible(dst, ns, allowed_scopes):
                continue  # невидимая цель — как отсутствующая: счётчик её не выдаёт
            edge = Edge(
                type="LINKS_TO",
                src=natural_key,
                dst=dst,
                origin="agent",
                source_path=src,
                namespace=ns,
            )
            if self.upsert_edge(edge, now):
                edges_made += 1
        linked_trace = None
        if trace_id:
            anchor = self.ensure_trace_anchor(trace_id, namespace=ns)
            self.upsert_edge(
                Edge(
                    type="IN_TRACE",
                    src=natural_key,
                    dst=anchor,
                    origin="agent",
                    source_path=src,
                    namespace=ns,
                ),
                now,
            )
            linked_trace = anchor
        return {
            "natural_key": natural_key,
            "namespace": ns,
            "origin": "agent",
            "edges": edges_made,
            "trace": linked_trace,
        }

    # --- аудит и трейсы (подграф трейса задачи) ---

    @staticmethod
    def trace_anchor_key(trace_id: str) -> str:
        """natural_key узла-якоря трейса задачи (trace_id = issueId/runId)."""
        return f"pc-trace:{trace_id}"

    def ensure_trace_anchor(
        self, trace_id: str, *, title: str | None = None, namespace: str = ""
    ) -> str:
        """Идемпотентно создать узел-якорь трейса задачи (origin='agent'). Вернуть natural_key.

        К этому якорю привязываются события аудита (``IN_TRACE``) и сохранённые факты —
        так формируется подграф трейса конкретной задачи Paperclip.
        """
        nk = self.trace_anchor_key(trace_id)
        now = dt.datetime.now().isoformat()
        self.upsert_node(
            Node(
                type="pc_trace",
                natural_key=nk,
                title=title or f"Трейс {trace_id}",
                properties={"trace_id": trace_id},
                origin="agent",
                namespace=self._ns(namespace),
                provenance=Provenance(
                    source_path=f"paperclip:trace/{trace_id}",
                    source_id=trace_id,
                    confidence=1.0,
                    last_seen=now,
                ),
            )
        )
        return nk

    def record_audit(
        self,
        trace_id: str,
        actor: str,
        action: str,
        *,
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
        run_id: str | None = None,
        actor_id: str | None = None,
        actor_kind: str = "agent",
        namespace: str = "",
    ) -> dict[str, Any]:
        """Записать событие аудита как узел ``audit_event``, привязанный к якорю трейса.

        Идемпотентно по содержимому (natural_key = ``audit:{trace}:{hash}``) — повторная
        доставка одного события не плодит дубликаты. Если задан ``actor_id`` — событие
        связывается ребром ``PERFORMED_BY`` с узлом-актором (``pc_agent``). origin='agent'.
        Весь подграф трейса живёт в одном namespace вызывающего.
        """
        ts = ts or dt.datetime.now().isoformat()
        ns = self._ns(namespace)
        anchor = self.ensure_trace_anchor(trace_id, namespace=ns)
        digest_src = json.dumps(
            {
                "a": action,
                "t": ts,
                "actor": actor,
                "actor_id": actor_id,
                "kind": actor_kind,
                "run": run_id,
                "p": _jsonable(payload or {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:16]
        nk = f"audit:{trace_id}:{digest}"
        props: dict[str, Any] = {
            "action": action,
            "actor": actor,
            "actor_kind": actor_kind,
            "ts": ts,
            "trace_id": trace_id,
        }
        if run_id:
            props["run_id"] = run_id
        if payload is not None:
            props["payload"] = _jsonable(payload)
        self.upsert_node(
            Node(
                type="audit_event",
                natural_key=nk,
                title=action,
                properties=props,
                origin="agent",
                namespace=ns,
                provenance=Provenance(
                    source_path=f"paperclip:trace/{trace_id}",
                    source_id=run_id or trace_id,
                    confidence=1.0,
                    last_seen=ts,
                ),
            )
        )
        self.upsert_edge(
            Edge(
                type="IN_TRACE",
                src=nk,
                dst=anchor,
                origin="agent",
                source_path=f"paperclip:trace/{trace_id}",
                namespace=ns,
            ),
            ts,
        )
        if actor_id:
            agent_nk = f"pc-agent:{actor_id}"
            self.upsert_node(
                Node(
                    type="pc_agent",
                    natural_key=agent_nk,
                    title=actor or actor_id,
                    properties={"actor_id": actor_id, "kind": actor_kind},
                    origin="agent",
                    namespace=ns,
                    provenance=Provenance(
                        source_path=f"paperclip:agent/{actor_id}",
                        source_id=actor_id,
                        confidence=1.0,
                        last_seen=ts,
                    ),
                )
            )
            self.upsert_edge(
                Edge(
                    type="PERFORMED_BY",
                    src=nk,
                    dst=agent_nk,
                    origin="agent",
                    source_path=f"paperclip:trace/{trace_id}",
                    namespace=ns,
                ),
                ts,
            )
        return {"natural_key": nk, "namespace": ns, "trace": anchor, "action": action, "ts": ts}

    def trace_subgraph(
        self,
        trace_id: str,
        *,
        limit: int = 200,
        namespace: str = "",
        allowed_scopes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Вернуть подграф трейса задачи: события аудита (по времени) и привязанные факты.

        Источник для проверки сквозного сценария и для UI «Память и трейс по задаче».
        ``allowed_scopes`` (не None) — видимость вызывающего (MEM-ADR-019): узлы вне
        неё отбрасываются до ``limit``. Scope факта — его ``props.scopes``, события
        аудита — ещё и ``props.payload.scopes`` (scopes удалённого объекта).
        Фильтр — в Python (``trace_node_scopes`` + ``is_visible``), а не в Cypher:
        payload события приходит от клиента, и предикат по произвольному agtype
        (объект, строка вместо списка) падал бы на весь трейс.
        """
        anchor = self.trace_anchor_key(trace_id)
        ns = self._ns(namespace)
        params: dict[str, Any] = {"nk": anchor, "ns": ns}
        # С фильтром видимости лимит считается после него — в цикле ниже.
        sql_limit = f" LIMIT {int(limit)}" if allowed_scopes is None else ""
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (n)-[:IN_TRACE]->(a {{natural_key: $nk, namespace: $ns}}) "
            f"RETURN properties(n) $$, %s) AS (props agtype){sql_limit}"
        )
        events: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            for (props,) in cur:
                parsed = dbmod.parse_agtype(props)
                if not isinstance(parsed, dict):
                    continue
                if not is_visible(trace_node_scopes(parsed), allowed_scopes):
                    continue
                if parsed.get("type") == "audit_event":
                    events.append(parsed)
                else:
                    facts.append(parsed)
                if len(events) + len(facts) >= limit:
                    break

        def _ts(node: dict[str, Any]) -> str:
            inner = node.get("props")
            return str(inner.get("ts", "")) if isinstance(inner, dict) else ""

        events.sort(key=_ts)
        return {
            "trace_id": trace_id,
            "anchor": anchor,
            "events": events,
            "facts": facts,
            "event_count": len(events),
            "fact_count": len(facts),
        }

    # --- чтение / обход ---

    def get_node(self, natural_key: str, namespaces: Sequence[str] = ()) -> dict[str, Any] | None:
        """Вернуть свойства узла по natural_key в области видимости или None, если не найден."""
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (n {{natural_key: $nk}}) WHERE n.namespace IN $nss RETURN properties(n) "
            f"$$, %s) AS (props agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype({"nk": natural_key, "nss": self._nss(namespaces)}),))
            row = cur.fetchone()
        return dbmod.parse_agtype(row[0]) if row else None

    def delete_node(
        self,
        natural_key: str,
        namespace: str = "",
        *,
        allowed_scopes: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Удалить узел вместе с рёбрами (DETACH DELETE); вернуть снапшот свойств.

        None — если узла нет или он вне видимости ``allowed_scopes`` (MEM-ADR-019;
        None — без ограничения): невидимый узел не удаляется и неотличим от
        отсутствующего. Защищённые типы (PROTECTED_TYPES) не удаляет — ValueError.
        Чанки индекса не трогает (слой VectorIndex); аудит пишет вызывающий — снапшот
        возвращается именно для payload аудит-события.
        """
        ns = self._ns(namespace)
        snapshot = self.get_node(natural_key, namespaces=[ns])
        if snapshot is None or not is_visible(node_scopes(snapshot), allowed_scopes):
            return None
        assert_deletable(str(snapshot.get("type") or ""))
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH (n {{natural_key: $nk, namespace: $ns}}) DETACH DELETE n "
                f"$$, %s) AS (v agtype)",
                (agtype({"nk": natural_key, "ns": ns}),),
            )
        return snapshot

    def expand_neighbors(
        self,
        natural_keys: list[str],
        hops: int = 1,
        limit: int = 40,
        namespaces: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Соседи узлов в пределах ``hops`` (1–2) внутри области видимости namespaces.

        Предикат namespace стоит на ОБОИХ концах пути — обход не выходит за пределы
        разрешённых namespaces.
        """
        if not natural_keys:
            return []
        hops = max(1, min(hops, 2))
        nss = self._nss(namespaces)
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (a)-[*1..{hops}]-(b) "
            f"WHERE a.natural_key IN $keys AND a.namespace IN $nss AND b.namespace IN $nss "
            f"AND a.natural_key <> b.natural_key "
            f"RETURN DISTINCT properties(b) "
            f"$$, %s) AS (props agtype) LIMIT {int(limit)}"
        )
        out: list[dict[str, Any]] = []
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype({"keys": natural_keys, "nss": nss}),))
            for (props,) in cur.fetchall():
                parsed = dbmod.parse_agtype(props)
                if isinstance(parsed, dict):
                    out.append(parsed)
        return out

    # --- типизированный обход (MEM-ADR-020) ---

    def nodes_by_keys(
        self, natural_keys: Sequence[str], namespaces: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Узлы с точным natural_key из списка в области видимости namespaces.

        Без метки — просмотр всех вершин графа; на горячих путях — ``lookup_nodes``.
        У узла — ``_gid`` (graphid вершины).
        """
        keys = [k for k in dict.fromkeys(natural_keys) if k]
        if not keys:
            return []
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (n) WHERE n.natural_key IN $keys AND n.namespace IN $nss "
            f"RETURN id(n), properties(n) $$, %s) AS (id agtype, props agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype({"keys": keys, "nss": self._nss(namespaces)}),))
            return _gid_nodes(cur.fetchall())

    def graph_labels(self) -> dict[int, tuple[str, str]]:
        """Метки графа: id метки -> (имя, ``v``|``e``); служебные родительские — без них.

        Старшие 16 бит graphid вершины/ребра — id его метки.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT l.id, l.name, l.kind FROM ag_catalog.ag_label l "
                "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
                "WHERE g.name = %s AND l.name NOT IN ('_ag_label_vertex', '_ag_label_edge')",
                (self.graph,),
            )
            return {int(r[0]): (str(r[1]), str(r[2])) for r in cur.fetchall()}

    def lookup_nodes(
        self,
        keys_by_label: dict[str, Iterable[str]],
        aliases_by_label: dict[str, Iterable[str]] | None = None,
        namespaces: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Узлы по точному ключу и по псевдониму — одним запросом, только в заданных метках.

        ``keys_by_label`` — метка -> ключи (``UNWIND`` + равенство: hash-индекс ключа
        метки, см. ``ensure_label_index``), ``aliases_by_label`` — метка -> строки,
        которые должны быть среди ``props.aliases`` (вхождение ``properties @> …`` —
        GIN-индекс свойств). Ветви по меткам — ``UNION ALL`` одного cypher-запроса:
        просматриваются только таблицы названных меток, а не все вершины графа
        (неразмеченный ``MATCH (n)`` читает весь граф всех namespaces). Метка, которой
        нет в графе, ничего не находит. У узла — ``_gid``; узел, найденный несколькими
        ветвями, — один раз.
        """
        branches: list[str] = []
        params: dict[str, Any] = {"nss": self._nss(namespaces)}
        for kind, group in (("k", keys_by_label), ("a", aliases_by_label or {})):
            for label in sorted(group):
                values = [v for v in dict.fromkeys(group[label]) if v]
                if not values:
                    continue
                name = f"{kind}{len(branches)}"
                params[name] = values
                lab = sanitize_label(label)
                match = (
                    f"MATCH (n:{lab}) WHERE n.natural_key = v AND n.namespace IN $nss"
                    if kind == "k"
                    else f"MATCH (n:{lab} {{props: {{aliases: [v]}}}}) WHERE n.namespace IN $nss"
                )
                branches.append(f"UNWIND ${name} AS v {match} RETURN id(n), properties(n)")
        if not branches:
            return []
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            + " UNION ALL ".join(branches)
            + " $$, %s) AS (id agtype, props agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            nodes = _gid_nodes(cur.fetchall())
        return list({n["_gid"]: n for n in nodes}.values())

    def typed_neighbors(
        self,
        start: dict[int, str],
        relation: str,
        directions: Sequence[str],
        *,
        labels: dict[int, tuple[str, str]],
        as_of: str = "",
        max_edges: int = 20000,
    ) -> tuple[list[tuple[int, str, dict[str, Any], dict[str, Any], int]], bool]:
        """Рёбра типа ``relation`` у стартовых вершин и узлы на других концах (MEM-ADR-020).

        ``start`` — graphid вершины -> её namespace; ``directions`` — из ``out``/``in``
        (``out`` — вершина начало ребра). Два запроса на все стартовые вершины: рёбра —
        из таблицы метки связи по ``start_id``/``end_id``, концы — по graphid только из
        таблиц их меток (id метки — старшие биты graphid, ``labels`` —
        ``graph_labels()``); вершины графа целиком не просматриваются. Проходим только
        по рёбрам, валидным на ``as_of`` (``valid_from <= t < valid_to``; пусто —
        действующие сейчас, ``valid_to IS NULL``), без ``evidence_lost`` и с концом в
        том же namespace.

        Валидность, ``evidence_lost`` и namespace ребра (у фактов — свойство
        ``namespace``) проверяются в SQL до лимита: закрытые версии рёбер хаба не
        вытесняют действующие. Рёбра без свойства ``namespace`` (структурные связи
        vault) соединяют узлы одного namespace по построению (``upsert_edge``); конец
        сверяется с namespace старта ещё раз по загруженному узлу. Действующих рёбер
        читается не больше ``max_edges`` (по возрастанию id). Вернуть
        ``([(graphid_откуда, направление, свойства_узла_куда, свойства_ребра,
        id_ребра)], усечено)``; у узла — ``_gid``; ``усечено`` — рёбер больше
        ``max_edges``, часть не прочитана.
        """
        if not start:
            return [], False
        dirs = [d for d in ("out", "in") if d in directions]
        if len(dirs) != len(set(directions)):
            raise ValueError(f"direction должен быть out|in: {list(directions)!r}")
        edge_label = sanitize_label(relation).upper()
        if (edge_label, "e") not in labels.values():
            return [], False
        ids = [str(g) for g in start]
        ends = [
            f"r.{'start_id' if d == 'out' else 'end_id'} = ANY(%s::text[]::graphid[])" for d in dirs
        ]
        # Свойства ребра — операторами agtype (``->>`` — текст, null/нет ключа — NULL);
        # сравнение моментов — побайтово (COLLATE "C"), как строки в Python.
        prop = "(r.properties ->> '{}'::text)"
        cond = [
            "(" + " OR ".join(ends) + ")",
            f"coalesce({prop.format('evidence_lost')}, '') <> 'true'",
            f"({prop.format('namespace')} IS NULL OR {prop.format('namespace')} IN "
            "(%s::jsonb ->> r.start_id::text, %s::jsonb ->> r.end_id::text))",
        ]
        args: list[Any] = [
            *([ids] * len(dirs)),
            *([json.dumps({g: ns for g, ns in zip(ids, start.values(), strict=True)})] * 2),
        ]
        if as_of:
            cond.append(
                f"(coalesce({prop.format('valid_from')}, '') = '' "
                f'OR {prop.format("valid_from")} COLLATE "C" <= %s)'
            )
            cond.append(
                f'({prop.format("valid_to")} IS NULL OR {prop.format("valid_to")} COLLATE "C" > %s)'
            )
            args += [as_of, as_of]
        else:
            cond.append(f"{prop.format('valid_to')} IS NULL")
        query = psql.SQL(
            "SELECT r.id::text, r.start_id::text, r.end_id::text, r.properties::text FROM {t} r "
            "WHERE " + " AND ".join(cond) + " ORDER BY r.id LIMIT %s"
        ).format(t=psql.Identifier(self.graph, edge_label))
        with self.conn.cursor() as cur:
            # На строку больше лимита: так видно, что рёбер больше, чем прочитано.
            cur.execute(query, (*args, int(max_edges) + 1))
            raw = cur.fetchall()
        truncated = len(raw) > max_edges
        edges: list[tuple[int, str, int, dict[str, Any], int]] = []
        for rid, sid, eid, props in raw[:max_edges]:
            rel = dbmod.parse_agtype(props)
            rel = rel if isinstance(rel, dict) else {}
            if not _edge_valid(rel, as_of):
                continue
            s_id, e_id = int(sid), int(eid)
            if "out" in dirs and s_id in start:
                edges.append((s_id, "out", e_id, rel, int(rid)))
            if "in" in dirs and e_id in start:
                edges.append((e_id, "in", s_id, rel, int(rid)))
        nodes = self._vertices_by_id({dst for _, _, dst, _, _ in edges}, labels)
        out: list[tuple[int, str, dict[str, Any], dict[str, Any], int]] = []
        for src, direction, dst, rel, rid in edges:
            node = nodes.get(dst)
            if node is not None and node.get("namespace") == start[src]:
                out.append((src, direction, node, rel, rid))
        return out, truncated

    def _vertices_by_id(
        self, ids: Iterable[int], labels: dict[int, tuple[str, str]]
    ) -> dict[int, dict[str, Any]]:
        """Вершины по graphid — только из таблиц их меток, одним запросом (``UNION ALL``)."""
        by_label: dict[str, list[str]] = {}
        for gid in ids:
            name, kind = labels.get(gid >> 48, ("", ""))
            if kind == "v":
                by_label.setdefault(name, []).append(str(gid))
        if not by_label:
            return {}
        parts: list[psql.Composable] = []
        params: list[Any] = []
        for name in sorted(by_label):
            parts.append(
                psql.SQL(
                    "SELECT id::text, properties::text FROM {t} "
                    "WHERE id = ANY(%s::text[]::graphid[])"
                ).format(t=psql.Identifier(self.graph, name))
            )
            params.append(by_label[name])
        with self.conn.cursor() as cur:
            cur.execute(psql.SQL(" UNION ALL ").join(parts), params)
            return {n["_gid"]: n for n in _gid_nodes(cur.fetchall())}

    def concept_subgraph(
        self,
        seed_keys: list[str],
        namespaces: Sequence[str] = (),
        max_related: int = 8,
    ) -> dict[str, list[dict[str, Any]]]:
        """Подграф отбора для визуализации: seed-статьи → их концепты → связанные статьи.

        Возвращает ``{"nodes": [{natural_key,type,title,role}], "edges": [{src,dst}]}``,
        где role ∈ {seed, concept, related}. Показывает, что статьи связаны через общие
        концепты графа (COVERS), а не выбраны только вектором. Пусто, если рёбер нет.
        """
        empty: dict[str, list[dict[str, Any]]] = {"nodes": [], "edges": []}
        if not seed_keys:
            return empty
        nss = self._nss(namespaces)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []

        def _add(props: Any, role: str) -> None:
            if not isinstance(props, dict):
                return
            nk = props.get("natural_key")
            if not nk:
                return
            if nk not in nodes:
                nodes[nk] = {
                    "natural_key": nk,
                    "type": props.get("type", ""),
                    "title": props.get("title", ""),
                    "role": role,
                }
            elif role == "seed":
                nodes[nk]["role"] = "seed"  # seed важнее related при пересечении

        # 1) seed-статьи → их концепты
        sql1 = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (a)-[:COVERS]->(c) "
            f"WHERE a.natural_key IN $keys AND a.namespace IN $nss AND c.namespace IN $nss "
            f"RETURN properties(a), properties(c) $$, %s) AS (pa agtype, pc agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql1, (agtype({"keys": seed_keys, "nss": nss}),))
            for pa, pc in cur.fetchall():
                a, c = dbmod.parse_agtype(pa), dbmod.parse_agtype(pc)
                _add(a, "seed")
                _add(c, "concept")
                if isinstance(a, dict) and isinstance(c, dict):
                    edges.append({"src": a["natural_key"], "dst": c["natural_key"]})

        concept_keys = [k for k, v in nodes.items() if v["type"] == "concept"]
        if not concept_keys:
            return {"nodes": list(nodes.values()), "edges": edges}

        # 2) те же концепты → связанные статьи (не seed), ограничение по числу
        sql2 = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (b)-[:COVERS]->(c) "
            f"WHERE c.natural_key IN $ckeys AND b.namespace IN $nss AND b.type = 'article' "
            f"RETURN properties(b), properties(c) $$, %s) AS (pb agtype, pc agtype) LIMIT 200"
        )
        seeds = {k for k, v in nodes.items() if v["role"] == "seed"}
        related_count = 0
        per_concept: dict[str, int] = {}  # не даём одному концепту залить панель
        with self.conn.cursor() as cur:
            cur.execute(sql2, (agtype({"ckeys": concept_keys, "nss": nss}),))
            for pb, pc in cur.fetchall():
                b, c = dbmod.parse_agtype(pb), dbmod.parse_agtype(pc)
                if not (isinstance(b, dict) and isinstance(c, dict)):
                    continue
                bk, ck = b.get("natural_key"), c.get("natural_key")
                if not bk or not ck or bk in seeds:
                    continue
                if bk not in nodes:
                    # новый связанный узел: лимит на концепт (≤2) и общий (max_related)
                    if related_count >= max_related or per_concept.get(ck, 0) >= 2:
                        continue
                    related_count += 1
                    per_concept[ck] = per_concept.get(ck, 0) + 1
                    _add(b, "related")
                edges.append({"src": bk, "dst": ck})
        return {"nodes": list(nodes.values()), "edges": edges}

    def neighborhood_subgraph(
        self,
        seed_keys: list[str],
        namespaces: Sequence[str] = (),
        hops: int = 1,
        limit: int = 55,
        per_seed: int = 10,  # оставлен для совместимости вызова; набор режется общим limit
    ) -> dict[str, list[dict[str, Any]]]:
        """Общая окрестность seed-узлов для визуализации графа компании (любые типы).

        Возвращает ``{"nodes": [{natural_key,type,title,role,hop}], "edges":
        [{src,dst,type}]}``. role: seed | neighbor; hop: 0 для seed, 1/2 для окрестности.
        Показывает типизированный подграф (люди, решения, проекты, концепты…), а не только
        концепты — retrieval вытаскивает связанную память по рёбрам. Изоляция по namespace.
        """
        empty: dict[str, list[dict[str, Any]]] = {"nodes": [], "edges": []}
        if not seed_keys:
            return empty
        nss = self._nss(namespaces)
        seeds = list(dict.fromkeys(seed_keys))
        seed_set = set(seeds)

        # 1) Набор узлов окрестности — ПОСТАДИЙНЫМ обходом hop-за-hop (каждый уровень — дешёвый
        #    hops=1 expand_neighbors), а НЕ одним variable-path [*1..hops]. На графе компании с
        #    хаб-узлами (концепты связаны с десятками статей/людей) обход `[*1..2]` комбинаторно
        #    взрывается: AGE обходит ВСЕ пути до применения LIMIT — секунды (у нас ~15с на hops=2).
        #    Постадийно тот же 2-hop набор строится за ~1с: два линейных hops=1 обхода от фронтира.
        node_keys = list(seeds)
        seen = set(seeds)
        frontier = list(seeds)
        # Фронтир hop2 ограничиваем: расширяться от десятков хабов не нужно, набор всё равно
        # режется общим limit, а меньший фронтир — меньше работы обходу.
        _FRONTIER_CAP = 16
        for _ in range(max(1, min(hops, 2))):
            if not frontier or len(node_keys) >= limit:
                break
            nbrs = self.expand_neighbors(
                frontier[:_FRONTIER_CAP], hops=1, limit=limit, namespaces=namespaces
            )
            frontier = []
            for n in nbrs:
                nk = str(n.get("natural_key", ""))
                if nk and nk not in seen:
                    seen.add(nk)
                    node_keys.append(nk)
                    frontier.append(nk)
                    if len(node_keys) >= limit:
                        break
        if not node_keys:
            return empty

        # 2) Свойства узлов (type/title) — один node-only запрос по всему набору ключей.
        meta: dict[str, dict[str, str]] = {}
        sqln = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (n) WHERE n.natural_key IN $keys AND n.namespace IN $nss "
            f"RETURN n.natural_key, n.type, n.title "
            f"$$, %s) AS (k agtype, t agtype, ti agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sqln, (agtype({"keys": node_keys, "nss": nss}),))
            for k_a, t_a, ti_a in cur.fetchall():
                k = str(dbmod.parse_agtype(k_a) or "")
                if k:
                    meta[k] = {
                        "type": str(dbmod.parse_agtype(t_a) or "entity"),
                        "title": str(dbmod.parse_agtype(ti_a) or ""),
                    }

        # 3) Рёбра среди известного набора (оба конца в node_keys). ВАЖНО: паттерн
        #    НАПРАВЛЕННЫЙ `(a)-[r]->(b)`. Ненаправленный `-[r]-` заставляет AGE обходить обе
        #    стороны каждого ребра и на выросшем графе тормозит в СОТНИ раз (~9.8с против ~70мс).
        #    Рёбра хранятся направленными — направленный обход находит каждое ровно один раз.
        edges: list[dict[str, str]] = []
        seen_edge: set[frozenset] = set()
        sqle = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (a)-[r]->(b) "
            f"WHERE a.natural_key IN $keys AND b.natural_key IN $keys AND a.namespace IN $nss "
            f"AND a.natural_key <> b.natural_key "
            f"RETURN a.natural_key, label(r), b.natural_key "
            f"$$, %s) AS (ak agtype, lbl agtype, bk agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sqle, (agtype({"keys": node_keys, "nss": nss}),))
            for ak_a, lbl, bk_a in cur.fetchall():
                ak = str(dbmod.parse_agtype(ak_a) or "")
                bk = str(dbmod.parse_agtype(bk_a) or "")
                if not ak or not bk:
                    continue
                rel = dbmod.parse_agtype(lbl)
                etype = str(rel).upper() if rel else "REL"
                sig = frozenset((ak, bk, etype))  # схлопнуть неориентированный дубль
                if sig in seen_edge:
                    continue
                seen_edge.add(sig)
                edges.append({"src": ak, "dst": bk, "type": etype})

        # 4) hop (0 для seed) — BFS от seed по неориентированным рёбрам.
        adj: dict[str, set[str]] = {}
        for e in edges:
            adj.setdefault(e["src"], set()).add(e["dst"])
            adj.setdefault(e["dst"], set()).add(e["src"])
        hop_of: dict[str, int] = dict.fromkeys(seeds, 0)
        frontier, dist = list(seeds), 0
        while frontier:
            dist += 1
            nxt: list[str] = []
            for u in frontier:
                for v in adj.get(u, ()):
                    if v not in hop_of:
                        hop_of[v] = dist
                        nxt.append(v)
            frontier = nxt

        nodes = [
            {
                "natural_key": k,
                "type": meta.get(k, {}).get("type", "entity"),
                "title": meta.get(k, {}).get("title", ""),
                "role": "seed" if k in seed_set else "neighbor",
                "hop": hop_of.get(k, 1),
            }
            for k in node_keys
        ]
        return {"nodes": nodes, "edges": edges}

    def list_nodes(
        self,
        *,
        type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        namespaces: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Список узлов области видимости с фильтром по типу и (опционально) статусу."""
        nss = self._nss(namespaces)
        if type:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
                f"WHERE n.type = $type AND n.namespace IN $nss "
                f"RETURN properties(n) $$, %s) AS (props agtype) LIMIT 500"
            )
            args: tuple = (agtype({"type": type, "nss": nss}),)
        else:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
                f"WHERE n.namespace IN $nss "
                f"RETURN properties(n) $$, %s) AS (props agtype) LIMIT 500"
            )
            args = (agtype({"nss": nss}),)
        out: list[dict[str, Any]] = []
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            for (props,) in cur.fetchall():
                parsed = dbmod.parse_agtype(props)
                if not isinstance(parsed, dict):
                    continue
                if status is not None:
                    inner = parsed.get("props") or {}
                    node_status = inner.get("status") if isinstance(inner, dict) else None
                    if node_status != status:
                        continue
                out.append(parsed)
                if len(out) >= limit:
                    break
        return out

    # --- экспорт для проекции в NetworkX ---

    def all_nodes(
        self, *, limit: int = 100_000, namespaces: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Узлы графа (свойства) — для проекции в NetworkX (кластеризация/анализ).

        ``namespaces=None`` — весь граф (offline-анализ); список — только эти namespaces.
        """
        if namespaces is None:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
                f"RETURN properties(n) $$) AS (props agtype) LIMIT {int(limit)}"
            )
            args: tuple = ()
        else:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
                f"WHERE n.namespace IN $nss "
                f"RETURN properties(n) $$, %s) AS (props agtype) LIMIT {int(limit)}"
            )
            args = (agtype({"nss": self._nss(namespaces)}),)
        out: list[dict[str, Any]] = []
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            for (props,) in cur.fetchall():
                parsed = dbmod.parse_agtype(props)
                if isinstance(parsed, dict):
                    out.append(parsed)
        return out

    def all_edges(
        self, *, limit: int = 500_000, namespaces: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Рёбра графа как {src, dst, type, source_path} — для проекции в NetworkX.

        ``namespaces=None`` — весь граф; список — рёбра, оба конца которых в области.
        """
        if namespaces is None:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (a)-[r]->(b) "
                f"RETURN a.natural_key, b.natural_key, type(r), r.source_path "
                f"$$) AS (src agtype, dst agtype, rtype agtype, sp agtype) LIMIT {int(limit)}"
            )
            args: tuple = ()
        else:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ MATCH (a)-[r]->(b) "
                f"WHERE a.namespace IN $nss AND b.namespace IN $nss "
                f"RETURN a.natural_key, b.natural_key, type(r), r.source_path "
                f"$$, %s) AS (src agtype, dst agtype, rtype agtype, sp agtype) "
                f"LIMIT {int(limit)}"
            )
            args = (agtype({"nss": self._nss(namespaces)}),)
        out: list[dict[str, Any]] = []
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            for src, dst, rtype, sp in cur.fetchall():
                out.append(
                    {
                        "src": dbmod.parse_agtype(src),
                        "dst": dbmod.parse_agtype(dst),
                        "type": dbmod.parse_agtype(rtype),
                        "source_path": dbmod.parse_agtype(sp),
                    }
                )
        return out

    # --- разметка сообществ (Phase 3) ---

    def set_node_communities(
        self,
        assignments: dict[str, int],
        cohesions: dict[int, float] | None = None,
        names: dict[int, str] | None = None,
        namespace: str = "",
    ) -> int:
        """Проставить узлам ``community``/``cohesion``/``community_name`` одним UNWIND.

        ``assignments`` — natural_key → community_id. ``cohesions`` (community_id → скор)
        и ``names`` (community_id → метка) опциональны. Разметка скоуплена одним
        namespace (пусто -> default стора) — офлайн-анализ сообществ не трогает чужие
        базы знаний даже при коллизии natural_key (ADR-017). Возвращает число
        обновлённых узлов. Узлы, отсутствующие в графе, молча пропускаются.
        """
        if not assignments:
            return 0
        ns = self._ns(namespace)
        cohesions = cohesions or {}
        names = names or {}
        rows = [
            {
                "nk": nk,
                "cid": cid,
                "coh": cohesions.get(cid),
                "name": names.get(cid),
            }
            for nk, cid in assignments.items()
        ]
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"UNWIND $rows AS row "
            f"MATCH (n {{natural_key: row.nk, namespace: $ns}}) "
            f"SET n.community = row.cid, n.cohesion = row.coh, n.community_name = row.name "
            f"RETURN count(n) $$, %s) AS (c agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype({"rows": rows, "ns": ns}),))
            row = cur.fetchone()
        return int(dbmod.parse_agtype(row[0])) if row else 0

    def community_members(
        self, community_id: int, *, limit: int = 500, namespaces: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Узлы заданного сообщества в области видимости (пусто -> default namespace)."""
        nss = self._nss(namespaces)
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ MATCH (n) "
            f"WHERE n.community = $cid AND n.namespace IN $nss "
            f"RETURN properties(n) $$, %s) AS (props agtype) LIMIT {int(limit)}"
        )
        out: list[dict[str, Any]] = []
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype({"cid": community_id, "nss": nss}),))
            for (props,) in cur.fetchall():
                parsed = dbmod.parse_agtype(props)
                if isinstance(parsed, dict):
                    out.append(parsed)
        return out

    # --- слияние узлов (применение одобренного арбитром merge) ---

    def merge_nodes(
        self, survivor_key: str, duplicate_key: str, namespace: str = ""
    ) -> dict[str, Any]:
        """Слить ``duplicate_key`` в ``survivor_key``: рёбра перевешиваются, дубликат удаляется.

        Применяется при одобрении кандидата на слияние арбитром, в пределах одного
        namespace. Рёбра дубликата пересоздаются на выжившем (через MERGE — дубли и петли
        отсекаются), затем дубликат удаляется ``DETACH DELETE``. Идемпотентно: повторный
        вызов на уже удалённом дубликате просто ничего не находит.
        """
        ns = self._ns(namespace)
        if survivor_key == duplicate_key:
            return {"ok": False, "reason": "same_node", "rewired": 0}
        if self.get_node(duplicate_key, namespaces=[ns]) is None:
            return {"ok": False, "reason": "duplicate_missing", "rewired": 0}
        if self.get_node(survivor_key, namespaces=[ns]) is None:
            return {"ok": False, "reason": "survivor_missing", "rewired": 0}

        rewired = 0
        with self.conn.cursor() as cur:
            # Исходящие рёбра дубликата.
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH (d {{natural_key: $dup, namespace: $ns}})-[r]->(b) "
                f"RETURN type(r), b.natural_key, r.source_path, r.origin "
                f"$$, %s) AS (t agtype, nk agtype, sp agtype, og agtype)",
                (agtype({"dup": duplicate_key, "ns": ns}),),
            )
            out_edges = [
                (
                    str(dbmod.parse_agtype(t)),
                    dbmod.parse_agtype(nk),
                    dbmod.parse_agtype(sp),
                    dbmod.parse_agtype(og),
                )
                for t, nk, sp, og in cur.fetchall()
            ]
            # Входящие рёбра дубликата.
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH (a)-[r]->(d {{natural_key: $dup, namespace: $ns}}) "
                f"RETURN type(r), a.natural_key, r.source_path, r.origin "
                f"$$, %s) AS (t agtype, nk agtype, sp agtype, og agtype)",
                (agtype({"dup": duplicate_key, "ns": ns}),),
            )
            in_edges = [
                (
                    str(dbmod.parse_agtype(t)),
                    dbmod.parse_agtype(nk),
                    dbmod.parse_agtype(sp),
                    dbmod.parse_agtype(og),
                )
                for t, nk, sp, og in cur.fetchall()
            ]

        for etype, other, sp, og in out_edges:
            if other and other != survivor_key:
                if self.upsert_edge(
                    Edge(
                        type=etype,
                        src=survivor_key,
                        dst=str(other),
                        source_path=sp,
                        origin=og or "vault",
                        namespace=ns,
                    )
                ):
                    rewired += 1
        for etype, other, sp, og in in_edges:
            if other and other != survivor_key:
                if self.upsert_edge(
                    Edge(
                        type=etype,
                        src=str(other),
                        dst=survivor_key,
                        source_path=sp,
                        origin=og or "vault",
                        namespace=ns,
                    )
                ):
                    rewired += 1

        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH (d {{natural_key: $dup, namespace: $ns}}) DETACH DELETE d "
                f"$$, %s) AS (v agtype)",
                (agtype({"dup": duplicate_key, "ns": ns}),),
            )
        return {
            "ok": True,
            "survivor": survivor_key,
            "duplicate": duplicate_key,
            "rewired": rewired,
        }

    # --- статистика ---

    def count_nodes_by_type(self, namespaces: Sequence[str] | None = None) -> dict[str, int]:
        """Вернуть число узлов в разрезе их типа (None — весь граф)."""
        if namespaces is None:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH (n) RETURN n.type, count(*) $$) AS (type agtype, c agtype)"
            )
            args: tuple = ()
        else:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH (n) WHERE n.namespace IN $nss "
                f"RETURN n.type, count(*) $$, %s) AS (type agtype, c agtype)"
            )
            args = (agtype({"nss": self._nss(namespaces)}),)
        out: dict[str, int] = {}
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            for type_val, c_val in cur.fetchall():
                t = dbmod.parse_agtype(type_val) or "entity"
                out[str(t)] = int(dbmod.parse_agtype(c_val))
        return out

    def count_edges_by_type(self, namespaces: Sequence[str] | None = None) -> dict[str, int]:
        """Вернуть число рёбер в разрезе их типа (None — весь граф)."""
        if namespaces is None:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH ()-[r]->() RETURN type(r), count(*) $$) AS (type agtype, c agtype)"
            )
            args: tuple = ()
        else:
            sql = (
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH (a)-[r]->() WHERE a.namespace IN $nss "
                f"RETURN type(r), count(*) $$, %s) AS (type agtype, c agtype)"
            )
            args = (agtype({"nss": self._nss(namespaces)}),)
        out: dict[str, int] = {}
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            for type_val, c_val in cur.fetchall():
                out[str(dbmod.parse_agtype(type_val))] = int(dbmod.parse_agtype(c_val))
        return out

    def total_nodes(self) -> int:
        """Вернуть суммарное число узлов в графе."""
        return sum(self.count_nodes_by_type().values())

    def total_edges(self) -> int:
        """Вернуть суммарное число рёбер в графе."""
        return sum(self.count_edges_by_type().values())
