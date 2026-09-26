"""Ingest наблюдений: приём, идемпотентность, детерминированная проекция (ADR-016).

Конвейер (§29 ТЗ):

```
retain_observation
      │ вставка raw observation (идемпотентно по source identity)
      ├── structured assertions ──► детерминированная проекция (без LLM):
      │       entity  → узел графа (origin='agent')
      │       fact    → temporal-ребро FactStore (evidence='asserted')
      │       text    → узел + чанк векторного индекса (цитируемый источник)
      └── unstructured content ──► FTS/trigram observations (сразу же находим);
                                   LLM-extraction — только явно (consolidation)
```

ACK не ждёт LLM. Ошибка проекции не теряет raw observation: статус
``partially_processed``/``failed`` + повторная обработка (redrive) идемпотентна.
"""

from __future__ import annotations

from typing import Any

import psycopg

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.models import Chunk
from platform_memory.core.namespaces import resolve_namespace
from platform_memory.core.observations import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PROCESSED,
    Observation,
    entity_ref_key,
    entity_ref_type,
)
from platform_memory.graph.facts import FactStore
from platform_memory.graph.store import GraphStore
from platform_memory.domain.registry import attach_kind_guard
from platform_memory.index import VectorIndex, build_embedder
from platform_memory.index.embeddings import Embedder
from platform_memory.observations.store import ObservationRecord, ObservationStore

ASSERT_KINDS = frozenset({"entity", "fact", "text"})

# Локальные алиасы (каноника — core.observations, там же ими пользуется compiler).
_entity_key = entity_ref_key
_entity_type = entity_ref_type


class AssertionProjector:
    """Детерминированная проекция assertions одного наблюдения в граф и индекс."""

    def __init__(
        self,
        graph: GraphStore,
        facts: FactStore,
        index: VectorIndex,
        embedder: Embedder | None,
        settings: Settings,
    ):
        """Собрать проектор поверх открытых сторов (соединение — вызывающего)."""
        self.graph = graph
        self.facts = facts
        self.index = index
        self.embedder = embedder
        self.settings = settings

    def project(self, record: ObservationRecord) -> list[str]:
        """Спроецировать assertions наблюдения; вернуть список ошибок (пусто = ок).

        Каждый assertion обрабатывается независимо: ошибка одного не блокирует
        остальные (частичная обработка фиксируется статусом наблюдения).
        Повторный вызов идемпотентен: upsert узлов, reinforce фактов, upsert
        чанков по стабильным ключам.
        """
        errors: list[str] = []
        source_path = str(record.provenance.get("uri") or "") or (
            f"memory:observation/{record.observation_id}"
        )
        for i, assertion in enumerate(record.assertions):
            try:
                kind = str(assertion.get("assert", "")).strip()
                if kind not in ASSERT_KINDS:
                    raise ValueError(f"Неизвестный вид assertion {kind!r}")
                if kind == "entity":
                    self._project_entity(assertion, record, source_path)
                elif kind == "fact":
                    self._project_fact(assertion, record, source_path)
                else:
                    self._project_text(assertion, record, source_path, i)
            except Exception as exc:  # ошибка одного assertion не теряет остальные
                errors.append(f"assertion[{i}]: {exc}")
        return errors

    def _project_entity(self, assertion: dict, record: ObservationRecord, source_path: str):
        entity = assertion.get("entity") or {}
        key = _entity_key(entity)
        self.facts.ensure_entity(
            key,
            entity_type=_entity_type(entity),
            title=str(entity.get("title", "")) or key,
            properties=dict(entity.get("properties") or {}),
            namespace=record.namespace,
            source_path=source_path,
            scopes=record.scopes,
        )

    def _project_fact(self, assertion: dict, record: ObservationRecord, source_path: str):
        fact = assertion.get("fact") or {}
        subject = _entity_key(fact.get("subject"))
        obj = _entity_key(fact.get("object"))
        predicate = str(fact.get("predicate", "")).strip()
        if not predicate:
            raise ValueError("fact без predicate")
        # Концы факта создаются placeholder'ами — проекция не зависит от порядка.
        for ref, key in ((fact.get("subject"), subject), (fact.get("object"), obj)):
            if self.graph.get_node(key, namespaces=[record.namespace]) is None:
                self.facts.ensure_entity(
                    key,
                    entity_type=_entity_type(ref),
                    namespace=record.namespace,
                    source_path=source_path,
                    scopes=record.scopes,
                )
        self.facts.assert_fact(
            subject,
            predicate,
            obj,
            namespace=record.namespace,
            observed_at=record.occurred_at,
            valid_from=str(fact.get("valid_from", "") or ""),
            valid_to=fact.get("valid_to"),
            evidence="asserted",
            confidence=float(fact.get("confidence", 1.0)),
            observation_id=record.observation_id,
            supersedes=str(fact.get("supersedes", "") or ""),
            scopes=record.scopes,
            source_path=source_path,
        )

    def _project_text(self, assertion: dict, record: ObservationRecord, source_path: str, i: int):
        text = assertion.get("text") or {}
        content = str(text.get("content", "")).strip()
        if not content:
            raise ValueError("text без content")
        node_key = str(text.get("key", "") or f"{record.observation_id}:text:{i}")
        title = str(text.get("title", "")) or (record.kind or "observation")
        node_type = str(text.get("type", "") or "note")
        self.facts.ensure_entity(
            node_key,
            entity_type=node_type,
            title=title,
            properties={"content": content, "observation_id": record.observation_id},
            namespace=record.namespace,
            source_path=source_path,
            scopes=record.scopes,  # Контентный узел текста — не сущность домена: вид не проверяется.
            check_kind=False,
        )
        if self.embedder is None:
            raise ValueError("эмбеддер недоступен — чанк не проиндексирован")
        meta: dict[str, Any] = {"observation_id": record.observation_id}
        if record.scopes:
            meta["scopes"] = list(record.scopes)
        chunk = Chunk(
            node_key=node_key,
            text=content,
            source_path=source_path,
            title=title,
            namespace=record.namespace,
            meta=meta,
        )
        self.index.add_chunks([chunk], [self.embedder.embed_one(chunk.embedding_input())])


def _process_record(
    stores: tuple[ObservationStore, AssertionProjector],
    record: ObservationRecord,
) -> str:
    """Обработать одно наблюдение: проекция + статус. Вернуть итоговый статус."""
    obs_store, projector = stores
    if not record.assertions:
        status = STATUS_PROCESSED
        obs_store.set_status(record.observation_id, status, namespace=record.namespace)
        return status
    errors = projector.project(record)
    if not errors:
        status = STATUS_PROCESSED
    elif len(errors) < len(record.assertions):
        status = STATUS_PARTIAL
    else:
        status = STATUS_FAILED
    obs_store.set_status(
        record.observation_id, status, detail="; ".join(errors)[:2000], namespace=record.namespace
    )
    return status


def _open_stores(
    settings: Settings, conn: psycopg.Connection, embedder: Embedder | None
) -> tuple[ObservationStore, AssertionProjector]:
    graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
    attach_kind_guard(graph, settings)  # строгий режим видов namespace (MEM-ADR-020)
    # Граф обязан существовать до проекции assertions: первый вызов API может
    # прийти раньше init-db (дёшево: один SELECT в ag_catalog + create при нужде).
    dbmod.ensure_graph(conn, graph.graph)
    facts = FactStore(graph)
    index = VectorIndex(
        conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
    )
    obs_store = ObservationStore(conn, settings.observations_table, settings.default_namespace)
    return obs_store, AssertionProjector(graph, facts, index, embedder, settings)


def retain_observation(
    settings: Settings,
    observation: Observation | dict[str, Any],
    *,
    namespace: str = "",
    embedder: Embedder | None = None,
    conn: psycopg.Connection | None = None,
) -> dict[str, Any]:
    """Принять одно наблюдение: вставка (идемпотентно) + детерминированная проекция.

    Возвращает ``{observation_id, duplicate, status}``. Дубликат по source
    identity не перезаписывает существующую запись и не проецируется повторно.
    """
    ns = resolve_namespace(namespace, settings.default_namespace)
    if isinstance(observation, dict):
        obs = Observation.from_payload(observation, namespace=ns)
    else:
        obs = observation
        obs.namespace = ns
        obs.canonicalize()

    own_conn = conn is None
    conn = conn or dbmod.connect(settings, autocommit=True)
    try:
        if embedder is None and _needs_embedder(obs):
            embedder = build_embedder(settings)
        obs_store, projector = _open_stores(settings, conn, embedder)
        obs_store.ensure_schema()
        oid, duplicate = obs_store.insert(obs)
        if duplicate:
            existing = obs_store.get(oid, namespaces=[ns])
            return {
                "observation_id": oid,
                "duplicate": True,
                "status": existing.status if existing else "received",
            }
        record = obs_store.get(oid, namespaces=[ns])
        status = _process_record((obs_store, projector), record)
        return {"observation_id": oid, "duplicate": False, "status": status}
    finally:
        if own_conn:
            conn.close()


def _needs_embedder(obs: Observation) -> bool:
    return any(str(a.get("assert")) == "text" for a in obs.assertions)


def retain_observations(
    settings: Settings,
    observations: list[Observation | dict[str, Any]],
    *,
    namespace: str = "",
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Batch-приём наблюдений с по-элементными результатами (partial errors).

    Каждый элемент обрабатывается в собственной операции (батч — НЕ одна
    гигантская транзакция): ошибка валидации одного не откатывает остальные.
    Размер батча ограничен ``CB_OBSERVATIONS_MAX_BATCH``.
    """
    if len(observations) > settings.observations_max_batch:
        raise ValueError(
            f"Батч больше лимита: {len(observations)} > {settings.observations_max_batch}"
        )
    conn = dbmod.connect(settings, autocommit=True)
    results: list[dict[str, Any]] = []
    accepted = duplicates = failed = 0
    try:
        if embedder is None:
            needs = any(
                _needs_embedder(o) for o in observations if isinstance(o, Observation)
            ) or any(
                str(a.get("assert")) == "text"
                for o in observations
                if isinstance(o, dict)
                for a in (o.get("assertions") or [])
            )
            embedder = build_embedder(settings) if needs else None
        for i, payload in enumerate(observations):
            try:
                result = retain_observation(
                    settings, payload, namespace=namespace, embedder=embedder, conn=conn
                )
                results.append({"index": i, **result})
                if result["duplicate"]:
                    duplicates += 1
                else:
                    accepted += 1
            except Exception as exc:  # ошибка элемента не роняет батч
                failed += 1
                results.append({"index": i, "error": str(exc)})
    finally:
        conn.close()
    return {
        "results": results,
        "accepted": accepted,
        "duplicates": duplicates,
        "failed": failed,
    }


def process_observations(
    settings: Settings,
    *,
    namespace: str = "",
    limit: int = 100,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Redrive: повторно обработать наблюдения в статусах received/partial/failed.

    Безопасно и идемпотентно (upsert/reinforce по стабильным ключам) — это
    dead-letter-механизм ADR-016 §30.
    """
    ns = resolve_namespace(namespace, settings.default_namespace)
    conn = dbmod.connect(settings, autocommit=True)
    counts: dict[str, int] = {}
    try:
        if embedder is None:
            embedder = build_embedder(settings)
        obs_store, projector = _open_stores(settings, conn, embedder)
        if not obs_store.table_exists():
            return {"processed": 0, "statuses": {}}
        records = obs_store.list_unprocessed([ns], limit=limit)
        for record in records:
            status = _process_record((obs_store, projector), record)
            counts[status] = counts.get(status, 0) + 1
        return {"processed": len(records), "statuses": counts}
    finally:
        conn.close()
