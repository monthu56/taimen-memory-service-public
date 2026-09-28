"""Typed response models for the platform-memory HTTP contract.

Every model allows extra fields (``extra="allow"``) so a newer service that adds
response keys never breaks an older client — forward compatibility by default.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    """``GET/PUT /api/memory/namespaces/{ns}/kinds`` — kind settings + effective catalog.

    ``reindex`` (PUT only) — the entity search index rebuilt for the new catalog (kinds
    with ``searchable``): ``{indexed, updated, removed, unchanged}`` or ``{error}``; an
    error keeps the settings, a repeated PUT finishes the reindex.
    """

    settings: dict[str, Any] = {}
    catalog: dict[str, Any] = {}
    reindex: dict[str, Any] | None = None


class ReconcileChanges(_Forward):
    """Natural keys ``{kind, key}`` of nodes touched by one reconcile (MEM-ADR-020).

    ``opened`` — new for the ``(source, scope)``, ``changed`` — superseded by a new
    version, ``closed`` — gone from the snapshot. Each list holds at most ``limit``
    keys; ``truncated`` says one of them was cut (full numbers are in ``entities``).
    """

    opened: list[dict[str, str]] = []
    changed: list[dict[str, str]] = []
    closed: list[dict[str, str]] = []
    limit: int | None = None
    truncated: bool = False


class ReconcileConflicts(_Forward):
    """Snapshot entities whose ``(kind, key)`` is open in the namespace by another
    ``(source, scope)``: ``items[{kind, key, source, scope}]`` (MEM-ADR-020, 2026-09-28).
    Information, not a rejection: each source still owns its versions."""

    items: list[dict[str, str]] = []
    limit: int | None = None
    truncated: bool = False


class ReconcileResult(_Forward):
    """Response of ``POST /api/memory/reconcile`` — snapshot reconciliation counters.

    ``dry_run`` — a plan, nothing written; ``state_token`` — fingerprint of the
    ``(source, scope)`` state: after a write the new state, for a plan the state it was
    built on (pass it back as ``expected_state`` to apply exactly this plan).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

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
    # Empty lists on a duplicate: the repeat changed nothing.
    changes: ReconcileChanges = ReconcileChanges()
    duplicate: bool = False
    dry_run: bool = Field(default=False, alias="dryRun")
    state_token: str | None = Field(default=None, alias="stateToken")
    conflicts: ReconcileConflicts = ReconcileConflicts()
    # Entity search index sync (kinds with ``searchable``): indexed/updated/removed/
    # unchanged; absent when the namespace has no index and on ``dryRun``.
    search_index: dict[str, int] | None = None


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


class EntityItem(_Forward):
    """An entity of ``POST /api/memory/entities:query`` — its version at ``as_of``."""

    kind: str
    key: str
    namespace: str | None = None
    title: str | None = None
    attributes: dict[str, Any] = {}
    # The snapshot source holding the version and its scope; ``source_path`` — citation.
    source: str | None = None
    scope: str | None = None
    snapshot_id: str | None = None
    source_path: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


class EntitiesPage(_Forward):
    """A page of entities in ``(kind, key, namespace)`` order (MEM-ADR-020).

    The listing ends only at ``next_cursor is None``: a page may be shorter than
    ``limit`` before the end (a rare ``where`` hits the per-request scan limit)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[EntityItem] = []
    next_cursor: str | None = Field(default=None, alias="nextCursor")
    as_of: str | None = None
    namespaces: list[str] = []
