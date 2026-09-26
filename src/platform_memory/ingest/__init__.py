"""platform_memory.ingest — проекция Obsidian-vault в граф + векторный индекс (read-only)."""

from platform_memory.ingest.entities import (
    EntityResult,
    extract_entities,
    resolve_document_entities,
)
from platform_memory.ingest.fuzzy_resolver import (
    MergeCandidate,
    ResolutionResult,
    resolve_entities,
)
from platform_memory.ingest.pipeline import IngestStats, ingest_vault

__all__ = [
    "ingest_vault",
    "IngestStats",
    "resolve_entities",
    "ResolutionResult",
    "MergeCandidate",
    "extract_entities",
    "resolve_document_entities",
    "EntityResult",
]
