"""Typed response models for the platform-memory HTTP contract.

Every model allows extra fields (``extra="allow"``) so a newer service that adds
response keys never breaks an older client — forward compatibility by default.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Forward(BaseModel):
    """Base: tolerate unknown fields the service may add later."""

    model_config = ConfigDict(extra="allow")


class Memory(_Forward):
    """A single recalled memory fragment with provenance back to its source file."""

    node_key: str | None = None
    title: str | None = None
    heading: str | None = None
    source_path: str | None = None
    text: str | None = None
    namespace: str | None = None


class RecallResult(_Forward):
    """Response of ``POST /recall`` — relevant context with citations."""

    query: str
    budget: str | None = None
    scope: dict[str, Any] | None = None
    count: int = 0
    memories: list[Memory] = []
    sources: list[dict[str, Any]] = []
    neighbors: Any = None
    context: str | None = None


class SearchResult(_Forward):
    """Response of ``POST /search`` — structural (by type) or semantic top-k."""

    query: str
    mode: str | None = None
    count: int = 0
    results: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []


class RetainResult(_Forward):
    """Response of ``POST /retain`` — idempotent fact/decision write-back."""

    retained: bool = True
    type: str | None = None
    title: str | None = None


class AuditResult(_Forward):
    """Response of ``POST /audit`` — a task-trace audit event."""

    recorded: bool = True


class QueryResult(_Forward):
    """Response of ``POST /api/brain/query`` — hits, neighbors, optional LLM answer."""

    question: str
    synthesized: bool = False
    answer: str | None = None
    hits: list[Any] = []
    neighbors: Any = None
    sources: list[dict[str, Any]] = []
    context: str | None = None


class DocumentChunk(BaseModel):
    """One pre-chunked text fragment for ``retain_document`` (host does the parsing)."""

    text: str
    heading: str = ""
    order: int | None = None


class DocumentIngestResult(_Forward):
    """Response of ``POST /api/brain/documents`` — batch document ingest summary."""

    natural_key: str | None = None
    namespace: str | None = None
    type: str | None = None
    chunks: int = 0
    replaced: bool = True


class DocumentDeleteResult(_Forward):
    """Response of ``DELETE /api/brain/documents/{natural_key}``."""

    natural_key: str | None = None
    namespace: str | None = None
    deleted: bool = False


class NodesResult(_Forward):
    """Response of ``GET /api/brain/nodes`` — structural listing by type / status."""

    namespace: str | None = None
    count: int = 0
    nodes: list[dict[str, Any]] = []


class NodeResult(_Forward):
    """Response of ``GET /api/brain/nodes/{natural_key}`` — a node with its neighbours."""

    node: dict[str, Any] | None = None
    neighbors: Any = None


class SourceResult(_Forward):
    """Response of ``GET /api/brain/sources/{natural_key}`` — the original document."""

    natural_key: str | None = None
    title: str | None = None
    type: str | None = None
    content: str | None = None
    namespace: str | None = None
    provenance: dict[str, Any] | None = None


class NodeDeleteResult(_Forward):
    """Response of ``DELETE /api/brain/nodes/{natural_key}`` (audited)."""

    natural_key: str | None = None
    namespace: str | None = None
    deleted: bool = False
    trace_id: str | None = None


class ObservationResult(_Forward):
    """Response of ``POST /api/memory/observations`` — one retained observation."""

    id: str | None = None
    deduplicated: bool = False


class ObservationBatchResult(_Forward):
    """Response of ``POST /api/memory/observations:batch`` (207, per-item results)."""

    results: list[dict[str, Any]] = []


class ContextPack(_Forward):
    """Response of ``POST /api/memory/context`` — bounded ContextPack (ADR-016 §18–25)."""

    sections: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    trace_id: str | None = None


class PackageResult(_Forward):
    """Response of ``POST /api/memory/packages`` — registered domain pack (MEM-ADR-020)."""

    status: str | None = None  # created | unchanged
    pack: dict[str, Any] = {}


class NamespaceKinds(_Forward):
    """``GET/PUT /api/memory/namespaces/{ns}/kinds`` — kind settings + effective catalog."""

    settings: dict[str, Any] = {}
    catalog: dict[str, Any] = {}


class ReconcileResult(_Forward):
    """Response of ``POST /api/memory/reconcile`` — snapshot reconciliation counters."""

    source: str | None = None
    scope: str | None = None
    namespace: str | None = None
    snapshot_id: str | None = None
    observed_at: str | None = None
    pack: str | None = None
    opened: int = 0
    closed: int = 0
    unchanged: int = 0
    superseded: int = 0
    entities: dict[str, int] = {}
    # Plus ``pending`` (ends not yet present), ``resolved`` (pending ones linked now)
    # and ``retried`` (pending ones re-checked by this reconcile).
    relations: dict[str, int] = {}
    duplicate: bool = False


class TypedContextPack(_Forward):
    """Response of ``POST /api/memory/context/typed`` — typed traversal pack (MEM-ADR-020)."""

    as_of: str | None = None
    anchors: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    used: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    trace_id: str | None = None
