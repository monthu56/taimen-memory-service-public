"""platform_memory_client — thin, typed HTTP client for memory-service.

A lightweight consumer SDK (httpx + pydantic only) that lets any host — support
bots, platform services, workers — call memory-service over HTTP without importing
the engine's stack. Sync and async variants share one wire contract; the bearer is
a static key grant (ADR-017) or an IAM access token (ADR-018), given as a string or
a callable / credential provider that is asked on every request.

    from platform_memory_client import MemoryClient

    with MemoryClient("http://memory:8077", token="…") as mem:
        ctx = mem.recall("who owns billing?", budget="high")
        mem.retain("Billing owned by team Payments.", type="decision")
"""

from platform_memory_client.client import (
    AsyncMemoryClient,
    MemoryClient,
    MemoryServiceError,
    MemorySnapshotStaleError,
    MemoryTransportError,
)
from platform_memory_client.models import (
    AuditResult,
    ContextPack,
    DocumentChunk,
    DocumentDeleteResult,
    DocumentIngestResult,
    EntitiesPage,
    EntityItem,
    EntitySource,
    Memory,
    NamespaceKinds,
    NodeDeleteResult,
    NodeResult,
    NodesResult,
    ObservationBatchResult,
    ObservationResult,
    PackageResult,
    QueryResult,
    RecallResult,
    ReconcileChanges,
    ReconcileConflicts,
    ReconcileResult,
    RetainResult,
    SearchResult,
    SourceResult,
    TypedContextPack,
)

__all__ = [
    "MemoryClient",
    "AsyncMemoryClient",
    "MemoryServiceError",
    "MemoryTransportError",
    "MemorySnapshotStaleError",
    "Memory",
    "RecallResult",
    "SearchResult",
    "RetainResult",
    "AuditResult",
    "QueryResult",
    "DocumentChunk",
    "DocumentIngestResult",
    "DocumentDeleteResult",
    "NodesResult",
    "NodeResult",
    "SourceResult",
    "NodeDeleteResult",
    "ObservationResult",
    "ObservationBatchResult",
    "ContextPack",
    "PackageResult",
    "NamespaceKinds",
    "ReconcileChanges",
    "ReconcileConflicts",
    "ReconcileResult",
    "TypedContextPack",
    "EntitiesPage",
    "EntityItem",
    "EntitySource",
]
