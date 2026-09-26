"""Тесты семантического кэша конвейера (вендорено из graphify/cache.py, адаптировано)."""

from __future__ import annotations

import pytest

from platform_memory.ingest.cache import ChunkCache, EmbeddingCache, content_hash

pytestmark = pytest.mark.unit


def test_content_hash_stable_and_distinct():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


# ── ChunkCache (версионируется по CHUNKER_VERSION) ──────────────────────────────


def test_chunk_cache_roundtrip(tmp_path):
    cache = ChunkCache(tmp_path)
    assert cache.get_chunks("тело заметки") is None
    chunks = [{"text": "a", "heading": "H", "order": 0}]
    cache.put_chunks("тело заметки", chunks)
    assert cache.get_chunks("тело заметки") == chunks


def test_chunk_cache_versioned_by_chunker_version(tmp_path):
    cache = ChunkCache(tmp_path)
    cache.put_chunks("body", [{"text": "x", "heading": "", "order": 0}])
    # Путь кэша должен содержать версию чанкера.
    assert any("v" in p.name for p in (tmp_path / "chunks").iterdir())


# ── EmbeddingCache (НЕ версионируется по чанкеру; ключ — модель/размерность) ─────


def test_embedding_cache_namespaced_by_model_and_dim(tmp_path):
    c1 = EmbeddingCache(tmp_path, model="text-embedding-3-small", dim=1536)
    c2 = EmbeddingCache(tmp_path, model="other-model", dim=1536)
    c1.put_vector("hello", [0.1, 0.2])
    # Другая модель не видит вектор первой (разные namespace).
    assert c2.get_vector("hello") is None
    assert c1.get_vector("hello") == [0.1, 0.2]


def test_embed_with_cache_only_embeds_misses(tmp_path):
    cache = EmbeddingCache(tmp_path, model="m", dim=2)
    calls: list[list[str]] = []

    def fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[float(len(t)), 0.0] for t in texts]

    # Первый прогон: всё промахи.
    vecs, hits, misses = cache.embed_with_cache(["a", "bb", "a"], fake_embed)
    assert hits == 0 and misses == 2  # "a" и "bb" — два уникальных
    assert calls == [["a", "bb"]]  # дубликат "a" не переэмбеддился
    assert vecs[0] == vecs[2] == [1.0, 0.0] and vecs[1] == [2.0, 0.0]

    # Второй прогон тем же входом: всё из кэша, эмбеддер не зовётся.
    calls.clear()
    vecs2, hits2, misses2 = cache.embed_with_cache(["a", "bb"], fake_embed)
    assert hits2 == 2 and misses2 == 0
    assert calls == []  # Eden AI не перебиллен
    assert vecs2 == [[1.0, 0.0], [2.0, 0.0]]


def test_embed_with_cache_partial_hit(tmp_path):
    cache = EmbeddingCache(tmp_path, model="m", dim=1)
    cache.put_vector("known", [9.0])

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]

    vecs, hits, misses = cache.embed_with_cache(["known", "new"], fake_embed)
    assert hits == 1 and misses == 1
    assert vecs == [[9.0], [1.0]]
