"""Разбор документа снимка (формат SNAPSHOT.md) без БД — MEM-ADR-020, амендмент.

Фикстуры — реальные снимки пакета software-delivery (``tests/fixtures/software_delivery``):
снимок принимается как есть, observedAt — время снимка, provenance входит в
содержимое, псевдонимы ключа выводятся шаблонами пакета, строгий режим проверяет связи.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from platform_memory.core.kinds import KindCatalog, UnknownRelationError, parse_pack
from platform_memory.domain.reconcile import (
    Ref,
    SnapshotError,
    changes_payload,
    fact_id_for_version,
    parse_snapshot,
    provenance_path,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "software_delivery"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def catalog() -> KindCatalog:
    return KindCatalog.build([parse_pack(_load("pack.json"))], strict=True)


@pytest.fixture
def cp() -> dict:
    return _load("control-plane.snapshot.json")


class TestParseRealSnapshot:
    def test_document_accepted_as_is(self, catalog, cp):
        snap = parse_snapshot(cp, catalog)
        assert (snap.source, snap.scope, snap.pack) == (
            "git:control-plane",
            "control-plane",
            "software-delivery@1",
        )
        assert snap.snapshot_id == cp["snapshotId"]
        assert snap.observed_at == "2026-09-23T08:45:00Z"
        assert len(snap.entities) == len(cp["entities"])
        assert len(snap.relations) == len(cp["relations"])

    def test_entity_content_aliases_and_citation(self, catalog, cp):
        snap = parse_snapshot(cp, catalog)
        endpoint = next(e for e in snap.entities if e.key == "GET /api/v1/runs/{}/checkpoints")
        assert endpoint.aliases == ["/api/v1/runs/{run_id}/checkpoints"]
        assert endpoint.provenance["line"] == 199
        assert endpoint.content()["provenance"] == endpoint.provenance  # входит в хэш
        assert endpoint.source_path.endswith(":src/control_plane/api/v1/runs.py:199")
        adr = next(e for e in snap.entities if e.kind == "adr")
        assert adr.aliases == ["ADR-0007", "CP-ADR-0007"]

    def test_relations_keep_both_ends_with_kinds(self, catalog, cp):
        snap = parse_snapshot(cp, catalog)
        call = next(r for r in snap.relations if r.relation == "calls")
        assert (call.src.kind, call.dst.kind) == ("client_method", "endpoint")
        assert call.content()["from"] == {"kind": call.src.kind, "key": call.src.key}


class TestRejections:
    @pytest.mark.parametrize(
        "field, fragment",
        [
            ("observedAt", "observedAt"),
            ("snapshotId", "snapshotId"),
            ("source", "source"),
        ],
    )
    def test_required_fields(self, catalog, cp, field, fragment):
        bad = {k: v for k, v in cp.items() if k != field}
        with pytest.raises(SnapshotError, match=fragment):
            parse_snapshot(bad, catalog)

    def test_pack_must_be_enabled_and_version_match(self, catalog, cp):
        with pytest.raises(SnapshotError, match="не включён"):
            parse_snapshot({**cp, "pack": "tracker@1"}, catalog)
        with pytest.raises(SnapshotError, match="версия"):
            parse_snapshot({**cp, "pack": "software-delivery@2"}, catalog)

    def test_duplicates_and_reserved_attributes(self, catalog, cp):
        dup = copy.deepcopy(cp)
        dup["entities"].append(copy.deepcopy(dup["entities"][0]))
        with pytest.raises(SnapshotError, match="дважды"):
            parse_snapshot(dup, catalog)
        dup = copy.deepcopy(cp)
        dup["relations"].append(copy.deepcopy(dup["relations"][0]))
        with pytest.raises(SnapshotError, match="дважды"):
            parse_snapshot(dup, catalog)
        reserved = copy.deepcopy(cp)
        reserved["entities"][0]["attributes"]["provenance"] = "x"
        with pytest.raises(SnapshotError, match="зарезервированные"):
            parse_snapshot(reserved, catalog)

    def test_strict_relation_check(self, catalog, cp):
        bad = copy.deepcopy(cp)
        bad["relations"][0]["relation"] = "imports"
        with pytest.raises(UnknownRelationError):
            parse_snapshot(bad, catalog)

    def test_scope_must_be_string(self, catalog, cp):
        with pytest.raises(SnapshotError, match="scope"):
            parse_snapshot({**cp, "scope": {"namespace": "x"}}, catalog)


def test_provenance_path_and_fact_id_bound_to_source():
    prov = {"repo": "control-plane", "sha": "f602", "path": "a.py", "line": 7}
    assert provenance_path(prov) == "control-plane@f602:a.py:7"
    assert provenance_path({"path": "a.py"}) == "a.py"
    assert provenance_path(None) == ""
    a = fact_id_for_version("ns", "git:a", "a", "calls\x1fx", "s1")
    assert a == fact_id_for_version("ns", "git:a", "a", "calls\x1fx", "s1")
    assert a != fact_id_for_version("ns", "git:b", "a", "calls\x1fx", "s1")
    assert a != fact_id_for_version("ns", "git:a", "a", "calls\x1fx", "s2")


def test_changes_payload_sorted_limited_and_flags_truncation():
    keys = {
        "opened": [Ref("endpoint", "b"), Ref("adr", "z"), Ref("endpoint", "a")],
        "closed": [Ref("adr", "x")],
    }
    out = changes_payload(keys, 2)
    assert out == {
        "opened": [{"kind": "adr", "key": "z"}, {"kind": "endpoint", "key": "a"}],
        "changed": [],
        "closed": [{"kind": "adr", "key": "x"}],
        "limit": 2,
        "truncated": True,
    }
    assert changes_payload(keys, 3)["truncated"] is False
    assert changes_payload({}, 5) == {
        "opened": [],
        "changed": [],
        "closed": [],
        "limit": 5,
        "truncated": False,
    }
