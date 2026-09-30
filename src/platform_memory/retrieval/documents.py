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
from collections.abc import Sequence
from typing import Any

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.models import Chunk
from platform_memory.core.namespaces import resolve_namespace
from platform_memory.core.scopes import (
    audit_scopes,
    check_write_scopes,
    is_visible,
    meta_scopes,
    meta_with_scopes,
    node_scopes,
)
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
    allowed_scopes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Записать документ: upsert узла + (replace) чанки с эмбеддингами. Вернуть сводку.

    ``chunks`` — список ``{"text": str, "heading"?: str, "order"?: int}``.
    ``meta`` копируется на каждый чанк (фильтруемые теги, например collection).

    ``allowed_scopes`` — видимость пишущего (None — без ограничения, MEM-ADR-019):
    узел или любой чанк ключа вне неё — :class:`ForeignObjectError` до любой записи,
    так что ``replace`` удаляет только видимые чанки. Scopes существующего узла
    сливаются, а не стираются; чанки несут в ``meta.scopes`` scopes узла и прежних
    чанков.
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
        check_write_scopes(meta_scopes(meta), allowed_scopes)  # scopes чанков — свои
        chunk_objs = [
            Chunk(
                node_key=natural_key,
                text=str(c["text"]),
                source_path=src,
                title=title,
                heading=str(c.get("heading", "")),
                order=int(c.get("order", i)),
                namespace=ns,
            )
            for i, c in enumerate(chunks)
        ]
        # Эмбеддинги — до лока ключа: провайдер может быть сетевым.
        embeddings = embedder.embed([c.embedding_input() for c in chunk_objs]) if chunk_objs else []
        node_props = dict(properties or {})
        if meta:
            node_props.setdefault("meta", meta)
        written = 0
        # Проверка чанков и узла ключа и запись — под одним локом ключа (MEM-ADR-019).
        with index.key_write_lock(natural_key, ns):
            chunk_scopes = index.guard_chunk_write(natural_key, ns, allowed_scopes)
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
                allowed_scopes=allowed_scopes,
            )
            node = graph.get_node(natural_key, namespaces=[ns]) or {}
            chunk_meta = meta_with_scopes(meta, node_scopes(node), chunk_scopes)
            if replace:
                index.delete_for_node(natural_key, ns, allowed_scopes=allowed_scopes)
            for c in chunk_objs:
                c.meta = dict(chunk_meta)
            if chunk_objs:
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
    allowed_scopes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Удалить документ: узел с рёбрами (DETACH DELETE) и все его чанки в namespace.

    Каждое фактическое удаление фиксируется аудит-событием ``action="delete"`` со
    снапшотом узла (ADR-001); повторный вызов по отсутствующему ключу — ``deleted: false``
    без события. Защищённые типы (аудит-контур) — ValueError. Документ вне видимости
    ``allowed_scopes`` (MEM-ADR-019; None — без ограничения) не трогается вовсе, ни
    узел, ни чанки, и ответ тот же, что для отсутствующего.
    """
    ns = resolve_namespace(namespace, settings.default_namespace)
    trace = trace_id or run_id or uuid.uuid4().hex
    conn = dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        index = VectorIndex(
            conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
        )
        chunks_deleted = 0
        current = graph.get_node(natural_key, namespaces=[ns])
        if current is not None and not is_visible(node_scopes(current), allowed_scopes):
            return _deleted(natural_key, ns, None, chunks_deleted, trace)
        snapshot = graph.delete_node(natural_key, ns, allowed_scopes=allowed_scopes)
        if index.table_exists():
            # Чанки без узла тоже фильтруются: чужой чанк не удаляется и не считается.
            chunks_deleted = index.delete_for_node(natural_key, ns, allowed_scopes=allowed_scopes)
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
                    **audit_scopes(snapshot),
                },
                run_id=run_id,
                namespace=ns,
            )
    finally:
        conn.close()
    return _deleted(natural_key, ns, snapshot, chunks_deleted, trace)


def _deleted(
    natural_key: str, ns: str, snapshot: dict[str, Any] | None, chunks_deleted: int, trace: str
) -> dict[str, Any]:
    return {
        "natural_key": natural_key,
        "namespace": ns,
        "deleted": snapshot is not None,
        "chunks_deleted": chunks_deleted,
        "trace_id": trace,
    }
