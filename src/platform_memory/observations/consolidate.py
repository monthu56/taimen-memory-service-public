"""Consolidation — on-demand сворачивание наблюдений в полезную память (ADR-016 §7).

Без демона: вызывается CLI (``cb consolidate``), HTTP (``POST /api/memory/
consolidate``) или хостом по расписанию. Что делает:

- redrive необработанных наблюдений (received/partially_processed/failed) —
  идемпотентная повторная проекция assertions;
- reinforcement фактов происходит внутри проекции (повтор того же утверждения
  добавляет observation_id и поднимает confidence, не плодя рёбра);
- закрытие temporal-интервалов — через явный ``supersedes`` в assertions.

LLM-extraction unstructured-контента сознательно ОТДЕЛЬНО и выключен по
умолчанию (§27: core corretness не зависит от LLM): включается флагом
``llm_extract=True`` и требует настроенного LLM-провайдера. Deletion
semantics — в ``delete_observation`` (redact/purge + каскад на derived).
"""

from __future__ import annotations

from typing import Any

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.namespaces import resolve_namespace
from platform_memory.graph.facts import FactStore
from platform_memory.graph.store import GraphStore
from platform_memory.index import VectorIndex
from platform_memory.observations.ingest import process_observations
from platform_memory.observations.store import ObservationStore


def consolidate(
    settings: Settings,
    *,
    namespace: str = "",
    limit: int = 500,
    llm_extract: bool = False,
) -> dict[str, Any]:
    """Прогнать consolidation; вернуть отчёт {processed, statuses, llm_extract}.

    Идемпотентно: повторный вызов на обработанных наблюдениях ничего не меняет.
    """
    ns = resolve_namespace(namespace, settings.default_namespace)
    report = process_observations(settings, namespace=ns, limit=limit)
    report["llm_extract"] = "disabled"
    if llm_extract:
        # Extension point (§27): извлечение entities/relations из unstructured
        # контента LLM-провайдером с evidence='inferred'. Не входит в core:
        # корректность памяти не должна зависеть от генеративной модели.
        report["llm_extract"] = "not_configured" if settings.llm_provider == "echo" else "skipped"
    return report


def delete_observation(
    settings: Settings,
    observation_id: str,
    *,
    namespace: str = "",
    mode: str = "redact",
    actor: str = "api",
    trace_id: str = "",
) -> dict[str, Any] | None:
    """Удалить/зачистить наблюдение и корректно отразить это на derived-памяти.

    ``redact`` — содержимое затирается, запись и id остаются (аудит-след);
    ``purge`` — строка удаляется физически. В обоих случаях: чанки, порождённые
    text-assertions наблюдения, удаляются из индекса; из фактов вычёркивается
    observation_id, факты без оставшихся свидетельств помечаются
    ``evidence_lost`` и выпадают из retrieval (не из графа — история и аудит).
    Событие удаления пишется в аудит-контур. None — наблюдение не найдено.
    """
    if mode not in ("redact", "purge"):
        raise ValueError(f"Неизвестный режим удаления {mode!r}: redact | purge")
    ns = resolve_namespace(namespace, settings.default_namespace)
    conn = dbmod.connect(settings, autocommit=True)
    try:
        obs_store = ObservationStore(conn, settings.observations_table, settings.default_namespace)
        if not obs_store.table_exists():
            return None
        record = obs_store.get(observation_id, namespaces=[ns])
        if record is None:
            return None
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        dbmod.ensure_graph(conn, graph.graph)  # наблюдение могло не породить граф
        facts = FactStore(graph)
        index = VectorIndex(
            conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
        )
        chunks_deleted = (
            index.delete_by_meta({"observation_id": observation_id}, namespace=ns)
            if index.table_exists()
            else 0
        )
        facts_lost = facts.mark_evidence_lost(observation_id, namespace=ns)
        # Содержимое наблюдения не должно пережить redact в производных узлах
        # (text-assertion кладёт content в props узла — зачищаем/удаляем).
        nodes_scrubbed = facts.scrub_derived_nodes(
            observation_id, namespace=ns, purge=(mode == "purge")
        )
        if mode == "redact":
            obs_store.redact(observation_id, namespace=ns)
        else:
            obs_store.purge(observation_id, namespace=ns)
        graph.record_audit(
            trace_id or observation_id,
            actor,
            "observation_delete",
            payload={
                "observation_id": observation_id,
                "mode": mode,
                "chunks_deleted": chunks_deleted,
                "facts_evidence_lost": facts_lost,
                "nodes_scrubbed": nodes_scrubbed,
            },
            namespace=ns,
        )
        return {
            "observation_id": observation_id,
            "mode": mode,
            "chunks_deleted": chunks_deleted,
            "facts_evidence_lost": facts_lost,
            "nodes_scrubbed": nodes_scrubbed,
        }
    finally:
        conn.close()
