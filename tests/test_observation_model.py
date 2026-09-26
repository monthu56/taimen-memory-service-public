"""Модель Observation (ADR-016): канонизация, идентичность, идемпотентность id."""

from __future__ import annotations

import pytest

from platform_memory.core.observations import Observation

pytestmark = pytest.mark.unit


def _payload(**overrides):
    base = {
        "source": {"system": "issue-tracker", "stream": "events", "externalId": "event-1842"},
        "kind": "work.completed",
        "occurredAt": "2026-08-11T10:00:00Z",
        "actor": {"type": "principal", "id": "alice"},
        "scopes": ["project:alpha"],
        "content": "Regression was fixed",
        "data": {"issue": "T-1842"},
        "provenance": {"uri": "https://tracker.local/event/1842"},
    }
    base.update(overrides)
    return base


def test_from_payload_canonical():
    obs = Observation.from_payload(_payload(), namespace="bank")
    assert obs.namespace == "bank"
    assert obs.source_system == "issue-tracker"
    assert obs.external_id == "event-1842"
    assert obs.kind == "work.completed"
    assert obs.occurred_at == "2026-08-11T10:00:00Z"
    assert obs.scopes == ["project:alpha"]
    assert obs.actor == {"type": "principal", "id": "alice"}


def test_occurred_at_normalized_to_utc():
    obs = Observation.from_payload(_payload(occurredAt="2026-08-11T13:00:00+03:00"))
    assert obs.occurred_at == "2026-08-11T10:00:00Z"


def test_observation_id_deterministic_by_source_identity():
    a = Observation.from_payload(_payload(content="Regression was fixed"), namespace="bank")
    # То же внешнее событие с чуть другим текстом — тот же id (идемпотентность доставки).
    b = Observation.from_payload(_payload(content="Regression fixed (retry)"), namespace="bank")
    assert a.observation_id() == b.observation_id()
    assert a.observation_id().startswith("obs-")


def test_observation_id_differs_across_namespaces():
    a = Observation.from_payload(_payload(), namespace="bank")
    b = Observation.from_payload(_payload(), namespace="mosmetro")
    assert a.observation_id() != b.observation_id()


def test_observation_id_without_external_id_uses_content_hash():
    p = _payload()
    del p["source"]
    a = Observation.from_payload(p)
    b = Observation.from_payload(p)
    assert a.observation_id() == b.observation_id()
    c = Observation.from_payload({**p, "content": "different"})
    assert c.observation_id() != a.observation_id()


def test_external_id_requires_system():
    p = _payload(source={"externalId": "event-1"})
    with pytest.raises(ValueError):
        Observation.from_payload(p)


def test_empty_observation_rejected():
    with pytest.raises(ValueError):
        Observation.from_payload({"kind": "noop"})


def test_bad_kind_rejected():
    with pytest.raises(ValueError):
        Observation.from_payload(_payload(kind="Work Completed!"))


def test_bad_actor_rejected():
    with pytest.raises(ValueError):
        Observation.from_payload(_payload(actor={"type": "principal"}))  # нет id


def test_scopes_canonicalized_and_deduped():
    obs = Observation.from_payload(
        _payload(scopes=["project:alpha", {"type": "task", "id": "T-1"}, "project:alpha"])
    )
    assert obs.scopes == ["project:alpha", "task:T-1"]


def test_content_hash_stable_and_sensitive():
    a = Observation.from_payload(_payload())
    b = Observation.from_payload(_payload())
    assert a.content_hash() == b.content_hash()
    c = Observation.from_payload(_payload(data={"issue": "T-9999"}))
    assert c.content_hash() != a.content_hash()
