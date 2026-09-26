"""Temporal-факты (ADR-016): детерминизм fact_id и представление конфликтов."""

from __future__ import annotations

import pytest

from platform_memory.graph.facts import EVIDENCE_RANK, FactStore, fact_id_for, normalize_ts

pytestmark = pytest.mark.unit


def test_normalize_ts_offset_and_microseconds():
    """Лексикографика temporal-строк требует единого формата (adversarial №1)."""
    assert normalize_ts("2026-08-01T13:00:00+03:00") == "2026-08-01T10:00:00Z"
    assert normalize_ts("2026-08-01T10:00:00.123456Z") == "2026-08-01T10:00:00Z"
    assert normalize_ts("2026-08-01T10:00:00") == "2026-08-01T10:00:00Z"  # naive -> UTC
    assert normalize_ts("") == ""
    assert normalize_ts(None) is None
    with pytest.raises(ValueError):
        normalize_ts("вчера вечером")


def test_fact_id_deterministic():
    a = fact_id_for("bank", "person:alice", "WORKS_ON", "project:x", "2026-08-01T00:00:00Z")
    b = fact_id_for("bank", "person:alice", "WORKS_ON", "project:x", "2026-08-01T00:00:00Z")
    assert a == b
    assert a.startswith("fact-")


def test_fact_id_differs_by_validity_and_namespace():
    base = ("person:alice", "WORKS_ON", "project:x")
    a = fact_id_for("bank", *base, "2026-08-01T00:00:00Z")
    b = fact_id_for("bank", *base, "2026-09-01T00:00:00Z")
    c = fact_id_for("metro", *base, "2026-08-01T00:00:00Z")
    assert len({a, b, c}) == 3


def test_evidence_rank_order():
    assert EVIDENCE_RANK["asserted"] > EVIDENCE_RANK["extracted"] > EVIDENCE_RANK["inferred"]


def _fact(fid, subj, pred, obj, vf="", vt=None, superseded_by=None):
    return {
        "fact_id": fid,
        "subject": subj,
        "predicate": pred,
        "object": obj,
        "valid_from": vf,
        "valid_to": vt,
        "superseded_by": superseded_by,
    }


def test_conflicts_overlapping_objects_marked():
    facts = [
        _fact("f1", "person:alice", "WORKS_ON", "project:x", "2026-01-01"),
        _fact("f2", "person:alice", "WORKS_ON", "project:y", "2026-06-01"),
    ]
    FactStore.find_conflicts(facts)
    assert facts[0].get("conflict") is True
    assert facts[1].get("conflict") is True
    assert "f2" in facts[0]["conflicts_with"]
    assert "f1" in facts[1]["conflicts_with"]


def test_superseded_pair_is_not_conflict():
    facts = [
        _fact("f1", "person:alice", "WORKS_ON", "project:x", "2026-01-01", "2026-06-01", "f2"),
        _fact("f2", "person:alice", "WORKS_ON", "project:y", "2026-06-01"),
    ]
    FactStore.find_conflicts(facts)
    assert facts[0].get("conflict") is None
    assert facts[1].get("conflict") is None


def test_disjoint_intervals_not_conflict():
    facts = [
        _fact("f1", "person:alice", "WORKS_ON", "project:x", "2026-01-01", "2026-03-01"),
        _fact("f2", "person:alice", "WORKS_ON", "project:y", "2026-06-01"),
    ]
    FactStore.find_conflicts(facts)
    assert facts[0].get("conflict") is None


def test_same_object_reinforcement_not_conflict():
    facts = [
        _fact("f1", "person:alice", "WORKS_ON", "project:x", "2026-01-01"),
        _fact("f2", "person:alice", "WORKS_ON", "project:x", "2026-06-01"),
    ]
    FactStore.find_conflicts(facts)
    assert facts[0].get("conflict") is None


def test_different_subjects_not_conflict():
    facts = [
        _fact("f1", "person:alice", "WORKS_ON", "project:x", "2026-01-01"),
        _fact("f2", "person:bob", "WORKS_ON", "project:y", "2026-01-01"),
    ]
    FactStore.find_conflicts(facts)
    assert facts[0].get("conflict") is None
