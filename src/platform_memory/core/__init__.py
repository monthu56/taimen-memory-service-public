"""platform_memory.core — общие модели, конфиг, онтология, namespaces и доступ к БД."""

from platform_memory.core.config import Settings, get_settings
from platform_memory.core.models import Chunk, Edge, Node, Provenance
from platform_memory.core.namespaces import (
    FALLBACK_NAMESPACE,
    SHARED_NAMESPACE,
    validate_namespace,
)

__all__ = [
    "Settings",
    "get_settings",
    "Node",
    "Edge",
    "Chunk",
    "Provenance",
    "FALLBACK_NAMESPACE",
    "SHARED_NAMESPACE",
    "validate_namespace",
]
