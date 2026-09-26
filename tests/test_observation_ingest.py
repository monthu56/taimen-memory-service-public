"""Проекция assertions наблюдения в граф/индекс (ADR-016 §3, §29–30)."""

from __future__ import annotations

import pytest

from platform_memory.core.config import Settings
from platform_memory.core.observations import STATUS_FAILED, STATUS_PARTIAL, STATUS_PROCESSED
from platform_memory.observations.ingest import AssertionProjector, _process_record
from platform_memory.observations.store import ObservationRecord

pytestmark = pytest.mark.unit


class _FakeGraph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}

    def get_node(self, key, namespaces=()):
        return self.nodes.get(key)


class _FakeFacts:
    def __init__(self, graph):
        self.graph = graph
        self.entities: list[dict] = []
        self.facts: list[dict] = []

    def ensure_entity(self, key, **kw):
        self.entities.append({"key": key, **kw})
        self.graph.nodes[key] = {"natural_key": key, "type": kw.get("entity_type", "entity")}
        return key

    def assert_fact(self, subject, predicate, obj, **kw):
        self.facts.append({"subject": subject, "predicate": predicate, "object": obj, **kw})
        return {"fact_id": f"fact-{len(self.facts)}", "reinforced": False, "superseded": False}


class _FakeIndex:
    def __init__(self):
        self.chunks: list = []

    def add_chunks(self, chunks, embeddings, seen_run=""):
        self.chunks.extend(chunks)
        return len(chunks)


class _FakeEmbedder:
    dim = 8

    def embed_one(self, text):
        return [0.0] * self.dim

    def embed(self, texts):
        return [[0.0] * self.dim for _ in texts]


class _FakeObsStore:
    def __init__(self):
        self.statuses: list[tuple[str, str, str]] = []

    def set_status(self, oid, status, detail="", namespace=""):
        self.statuses.append((oid, status, detail))


def _projector(embedder=_FakeEmbedder()):
    graph = _FakeGraph()
    facts = _FakeFacts(graph)
    index = _FakeIndex()
    settings = Settings(_env_file=None)
    return AssertionProjector(graph, facts, index, embedder, settings), graph, facts, index


def _record(assertions, scopes=None):
    return ObservationRecord(
        observation_id="obs-abc",
        namespace="bank",
        kind="work.completed",
        occurred_at="2026-08-11T10:00:00Z",
        scopes=scopes or ["project:alpha"],
        content="Regression fixed",
        assertions=assertions,
        provenance={"uri": "https://tracker.local/event/1"},
    )


def test_entity_assertion_creates_node():
    projector, _, facts, _ = _projector()
    errors = projector.project(
        _record([{"assert": "entity", "entity": {"type": "person", "id": "alice"}}])
    )
    assert errors == []
    assert facts.entities[0]["key"] == "person:alice"
    assert facts.entities[0]["entity_type"] == "person"
    assert facts.entities[0]["scopes"] == ["project:alpha"]
    assert facts.entities[0]["source_path"] == "https://tracker.local/event/1"


def test_fact_assertion_creates_placeholders_and_fact():
    projector, graph, facts, _ = _projector()
    errors = projector.project(
        _record(
            [
                {
                    "assert": "fact",
                    "fact": {
                        "subject": {"type": "person", "id": "alice"},
                        "predicate": "WORKS_ON",
                        "object": "project:x",
                        "valid_from": "2026-08-01T00:00:00Z",
                        "confidence": 0.9,
                    },
                }
            ]
        )
    )
    assert errors == []
    # Оба конца созданы placeholder'ами до записи факта.
    assert "person:alice" in graph.nodes and "project:x" in graph.nodes
    fact = facts.facts[0]
    assert fact["predicate"] == "WORKS_ON"
    assert fact["evidence"] == "asserted"
    assert fact["observation_id"] == "obs-abc"
    assert fact["valid_from"] == "2026-08-01T00:00:00Z"
    assert fact["observed_at"] == "2026-08-11T10:00:00Z"
    assert fact["confidence"] == 0.9


def test_fact_assertion_passes_supersedes():
    projector, _, facts, _ = _projector()
    projector.project(
        _record(
            [
                {
                    "assert": "fact",
                    "fact": {
                        "subject": "person:alice",
                        "predicate": "WORKS_ON",
                        "object": "project:y",
                        "supersedes": "fact-old",
                    },
                }
            ]
        )
    )
    assert facts.facts[0]["supersedes"] == "fact-old"


def test_text_assertion_indexes_chunk_with_provenance():
    projector, graph, _, index = _projector()
    errors = projector.project(
        _record([{"assert": "text", "text": {"content": "Важная заметка", "title": "Note"}}])
    )
    assert errors == []
    chunk = index.chunks[0]
    assert chunk.node_key == "obs-abc:text:0"
    assert chunk.meta["observation_id"] == "obs-abc"
    assert chunk.meta["scopes"] == ["project:alpha"]
    assert chunk.source_path == "https://tracker.local/event/1"
    assert "obs-abc:text:0" in graph.nodes


def test_text_assertion_without_embedder_reports_error():
    projector, _, _, index = _projector(embedder=None)
    errors = projector.project(_record([{"assert": "text", "text": {"content": "x"}}]))
    assert len(errors) == 1
    assert index.chunks == []


def test_partial_errors_do_not_block_other_assertions():
    projector, _, facts, _ = _projector()
    errors = projector.project(
        _record(
            [
                {"assert": "bogus"},
                {"assert": "entity", "entity": {"type": "person", "id": "bob"}},
            ]
        )
    )
    assert len(errors) == 1
    assert facts.entities[0]["key"] == "person:bob"


def test_process_record_statuses():
    obs_store = _FakeObsStore()
    projector, _, _, _ = _projector()

    ok = _record([{"assert": "entity", "entity": {"type": "person", "id": "a"}}])
    assert _process_record((obs_store, projector), ok) == STATUS_PROCESSED

    partial = _record(
        [
            {"assert": "bogus"},
            {"assert": "entity", "entity": {"type": "person", "id": "b"}},
        ]
    )
    assert _process_record((obs_store, projector), partial) == STATUS_PARTIAL

    bad = _record([{"assert": "bogus"}])
    assert _process_record((obs_store, projector), bad) == STATUS_FAILED
    # Ошибка зафиксирована в status_detail.
    assert "assertion[0]" in obs_store.statuses[-1][2]


def test_process_record_without_assertions_is_processed():
    obs_store = _FakeObsStore()
    projector, _, _, _ = _projector()
    rec = _record([])
    assert _process_record((obs_store, projector), rec) == STATUS_PROCESSED
