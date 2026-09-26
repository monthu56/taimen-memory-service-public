"""platform_memory.index — векторный индекс (pgvector) + эмбеддинги."""

from platform_memory.index.embeddings import Embedder, build_embedder
from platform_memory.index.store import SearchHit, VectorIndex

__all__ = ["Embedder", "build_embedder", "VectorIndex", "SearchHit"]
