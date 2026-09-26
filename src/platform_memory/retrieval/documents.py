"""Батчевый ингест документов: узел графа + чанки + эмбеддинги одним вызовом (ADR-017).

Контракт для мигрируемых Баз знаний: хост парсит документ (PDF/DOCX/…) сам и передаёт
готовые текстовые чанки; движок владеет эмбеддингами (единая модель/размерность) и
пишет узел ``document`` + чанки в ОДИН namespace. Идемпотентный re-ingest по
``natural_key`` (``replace=True`` предварительно удаляет старые чанки узла);
продолжение больших документов — повторные вызовы с ``replace=False``.

Удаление документа — hard delete с обязательным аудит-событием (ADR-001), как у
``DELETE /api/brain/nodes/{key}``; узлы аудит-контура защищены (ValueError).
"""

from __future__ import annotations

import uuid
from typing import Any

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.models import Chunk
from platform_memory.core.namespaces import resolve_namespace
from platform_memory.domain.registry import attach_kind_guard
from platform_memory.graph import GraphStore
from platform_memory.index import VectorIndex, build_embedder
from platform_memory.index.embeddings import Embedder

# Максимум чанков в одном вызове (защита от гигантских запросов; продолжение —
# повторными вызовами с replace=False).
MAX_CHUNKS_PER_REQUEST = 500


def retain_document(
    settings: Settings,
    *,
    namespace: str,
    natural_key: str,
    title: str,
    chunks: list[dict[str, Any]],
    node_type: str = "document",
    source_path: str = "",
    properties: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    links: list[str] | None = None,
    replace: bool = True,
    run_id: str | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Записать документ: upsert узла + (replace) чанки с эмбеддингами. Вернуть сводку.

    ``chunks`` — список ``{"text": str, "heading"?: str, "order"?: int}``.
    ``meta`` копируется на каждый чанк (фильтруемые теги, например collection).
    """
    if len(chunks) > MAX_CHUNKS_PER_REQUEST:
        raise ValueError(
            f"Слишком много чанков в одном вызове: {len(chunks)} > {MAX_CHUNKS_PER_REQUEST}"
        )
    ns = resolve_namespace(namespace, settings.default_namespace)
    embedder = embedder or build_embedder(settings)
    src = source_path or f"agent:run/{run_id or '-'}"

    conn = dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        graph.ensure_schema()
        attach_kind_guard(graph, settings)  # строгий режим видов namespace (MEM-ADR-020)
        index = VectorIndex(
            conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
        )
        index.ensure_schema()

        node_props = dict(properties or {})
        if meta:
            node_props.setdefault("meta", meta)
        graph.write_fact(
            natural_key,
            node_type,
            title,
            properties=node_props,
            links=links,
            run_id=run_id,
            confidence=1.0,
            source=src,
            trace_id=None,
            namespace=ns,
        )

        if replace:
            index.delete_for_node(natural_key, ns)

        chunk_objs = [
            Chunk(
                node_key=natural_key,
                text=str(c["text"]),
                source_path=src,
                title=title,
                heading=str(c.get("heading", "")),
                order=int(c.get("order", i)),
                namespace=ns,
                meta=dict(meta or {}),
            )
            for i, c in enumerate(chunks)
        ]
        written = 0
        if chunk_objs:
            embeddings = embedder.embed([c.embedding_input() for c in chunk_objs])
            written = index.add_chunks(chunk_objs, embeddings)
    finally:
        conn.close()

    return {
        "natural_key": natural_key,
        "namespace": ns,
        "type": node_type,
        "chunks": written,
        "replaced": replace,
    }


def delete_document(
    settings: Settings,
    natural_key: str,
    namespace: str,
    *,
    actor: str = "api",
    trace_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Удалить документ: узел с рёбрами (DETACH DELETE) и все его чанки в namespace.

    Каждое фактическое удаление фиксируется аудит-событием ``action="delete"`` со
    снапшотом узла (ADR-001); повторный вызов по отсутствующему ключу — ``deleted: false``
    без события. Защищённые типы (аудит-контур) — ValueError.
    """
    ns = resolve_namespace(namespace, settings.default_namespace)
    trace = trace_id or run_id or uuid.uuid4().hex
    conn = dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        index = VectorIndex(
            conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
        )
        snapshot = graph.delete_node(natural_key, ns)
        chunks_deleted = 0
        if index.table_exists():
            chunks_deleted = index.delete_for_node(natural_key, ns)
        if snapshot is not None:
            graph.record_audit(
                trace,
                actor,
                "delete",
                payload={
                    "natural_key": natural_key,
                    "type": snapshot.get("type"),
                    "title": snapshot.get("title"),
                    "chunks_deleted": chunks_deleted,
                    "namespace": snapshot.get("namespace"),
                },
                run_id=run_id,
                namespace=ns,
            )
    finally:
        conn.close()
    return {
        "natural_key": natural_key,
        "namespace": ns,
        "deleted": snapshot is not None,
        "chunks_deleted": chunks_deleted,
        "trace_id": trace,
    }
