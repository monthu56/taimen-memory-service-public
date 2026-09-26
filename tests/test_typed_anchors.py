"""Разрешение якорей typed-контекста (MEM-ADR-020, амендмент «typed через resolve»).

Якорь разрешается тем же ``resolve_candidates``, что и канал resolve: точный ключ →
псевдоним → idPatterns → нормализованная форма → суффикс. Чистая логика на фейковых
графе и журнале (формат строк журнала — как в ``test_context_resolve``), без БД.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_memory.context.typed import (
    MAX_ANCHORS,
    AnchorRef,
    TypedContextRequest,
    _Compiler,
)
from platform_memory.core.kinds import KindCatalog, parse_pack
from tests.test_context_resolve import NS, FakeLedger, _row

NS2 = "tenant:t1:ws:w2"

pytestmark = pytest.mark.unit

PACK = Path(__file__).parent / "fixtures" / "software_delivery" / "pack.json"

LEDGER_ROWS = [
    _row("POST /api/v1/goals/{}", "endpoint", aliases=["/api/v1/goals/{goal_id}"], rid=1),
    _row("GET /api/v1/goals/{}", "endpoint", aliases=["/api/v1/goals/{goal_id}"], rid=2),
    _row("control-plane:src/control_plane/api/v1/goals.py", "source_file", rid=3),
    _row("control-plane:work_rules", "table", rid=4),
    _row("platform-web:work_rules", "table", rid=5),
    # Версия есть в журнале, узел удалён через API — якорем не всплывает.
    _row("control-plane:deleted_table", "table", rid=6),
]


class FakeGraph:
    """Граф в памяти: узлы с ``type``/``natural_key``, поиск по ключу и псевдониму в метках."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.calls: list[str] = []

    def graph_labels(self):
        labels = sorted({n["type"] for n in self.nodes})
        return {i: (label, "v") for i, label in enumerate(labels, start=3)}

    def lookup_nodes(self, keys_by_label, aliases_by_label=None, namespaces=()):
        aliases_by_label = aliases_by_label or {}
        self.calls += [c for c, g in (("keys", keys_by_label), ("alias", aliases_by_label)) if g]
        return [
            n
            for n in self.nodes
            if n["namespace"] in namespaces
            and (
                n["natural_key"] in keys_by_label.get(n["type"], ())
                or set(n["props"].get("aliases", [])) & set(aliases_by_label.get(n["type"], ()))
            )
        ]


class NoVersions(FakeLedger):
    def entity_versions(self, namespaces, keys):
        return {}


def _node(key: str, kind: str, *, aliases=()) -> dict:
    return {
        "namespace": NS,
        "type": kind,
        "natural_key": key,
        "title": key,
        "props": {"aliases": list(aliases)},
    }


GRAPH_NODES = [
    _node(r["key"], r["kind"], aliases=r["payload"]["aliases"])
    for r in LEDGER_ROWS
    if r["key"] != "control-plane:deleted_table"
] + [_node("note:payment-bug", "note", aliases=["payment-bug-alias"])]


@pytest.fixture(scope="module")
def catalog() -> KindCatalog:
    return KindCatalog.build([parse_pack(json.loads(PACK.read_text(encoding="utf-8")))])


def _resolve(catalog, value: str, kind: str = "", *, ledger=True):
    graph = FakeGraph(GRAPH_NODES)
    journal = NoVersions(LEDGER_ROWS) if ledger else None
    req = TypedContextRequest(anchors=[AnchorRef(value, kind)], namespaces=[NS])
    compiler = _Compiler(graph, journal, {NS: catalog}, req, None)
    out = compiler.resolve_anchor(req.anchors[0], lambda *_: [])
    return out, graph, journal


def _resolve_many(catalog, anchors, *, rows=LEDGER_ROWS, nodes=GRAPH_NODES, nss=(NS,)):
    graph = FakeGraph(nodes)
    journal = NoVersions(rows)
    req = TypedContextRequest(anchors=[AnchorRef(v, k) for v, k in anchors], namespaces=list(nss))
    compiler = _Compiler(graph, journal, {ns: catalog for ns in nss}, req, None)
    return compiler.resolve_anchors(req.anchors, lambda *_: []), graph, journal


def _keys(out) -> list[str]:
    return sorted(r["natural_key"] for r in out["resolved"])


def test_exact_key_unchanged_and_ledger_not_queried(catalog):
    out, graph, journal = _resolve(catalog, "POST /api/v1/goals/{}", "endpoint")
    assert _keys(out) == ["POST /api/v1/goals/{}"]
    assert out["matchedBy"] == "natural_key"
    assert out["resolved"][0]["evidence"] == "asserted"
    assert "ambiguous" not in out and "candidates" not in out
    assert journal.calls == [] and graph.calls == ["keys"]


def test_alias_with_original_param_names(catalog):
    out, _, _ = _resolve(catalog, "/api/v1/goals/{goal_id}")
    assert _keys(out) == ["GET /api/v1/goals/{}", "POST /api/v1/goals/{}"]
    # Псевдоним — точный шаг: несколько эндпоинтов пути — не неоднозначность.
    assert out["matchedBy"] == "alias" and "ambiguous" not in out


def test_other_placeholder_name_normalized(catalog):
    out, _, _ = _resolve(catalog, "POST /api/v1/goals/{id}", "endpoint")
    assert _keys(out) == ["POST /api/v1/goals/{}"]
    assert out["matchedBy"] == "normalized"
    assert out["resolved"][0]["evidence"] == "asserted"
    assert "ambiguous" not in out


def test_endpoint_without_method_resolves_all_methods_as_ambiguous(catalog):
    out, _, _ = _resolve(catalog, "/api/v1/goals/{id}", "endpoint")
    assert _keys(out) == ["GET /api/v1/goals/{}", "POST /api/v1/goals/{}"]
    assert out["matchedBy"] == "suffix"
    assert {r["evidence"] for r in out["resolved"]} == {"extracted"}
    assert out["ambiguous"] is True
    assert sorted(c["natural_key"] for c in out["candidates"]) == _keys(out)


def test_file_without_repo_prefix(catalog):
    out, _, _ = _resolve(catalog, "src/control_plane/api/v1/goals.py", "source_file")
    assert _keys(out) == ["control-plane:src/control_plane/api/v1/goals.py"]
    assert out["matchedBy"] == "suffix" and "ambiguous" not in out


def test_table_by_name_and_kind_filter(catalog):
    out, _, _ = _resolve(catalog, "work_rules", "table")
    assert _keys(out) == ["control-plane:work_rules", "platform-web:work_rules"]
    assert out["matchedBy"] == "suffix" and out["ambiguous"] is True
    # Подсказка вида отсекает сущности других видов.
    other, _, _ = _resolve(catalog, "work_rules", "endpoint")
    assert other["resolved"] == [] and "matchedBy" not in other


def test_ledger_entity_without_graph_node_stays_unresolved(catalog):
    out, _, _ = _resolve(catalog, "deleted_table", "table")
    assert out["resolved"] == [] and "matchedBy" not in out


def test_graph_only_entities_still_resolve(catalog):
    # Сущности без журнала: ключ ``<kind>:<value>`` и псевдоним узла графа.
    by_key, _, _ = _resolve(catalog, "payment-bug", "note", ledger=False)
    assert _keys(by_key) == ["note:payment-bug"] and by_key["matchedBy"] == "natural_key"
    by_alias, _, _ = _resolve(catalog, "payment-bug-alias")
    assert _keys(by_alias) == ["note:payment-bug"] and by_alias["matchedBy"] == "alias"


def test_without_ledger_no_suffix(catalog):
    out, _, _ = _resolve(catalog, "work_rules", "table", ledger=False)
    assert out["resolved"] == []


def test_anchor_limits_unchanged():
    anchors = [{"value": f"A-{i}"} for i in range(MAX_ANCHORS)]
    assert MAX_ANCHORS == 20
    assert len(TypedContextRequest.from_payload({"anchors": anchors}).anchors) == 20
    with pytest.raises(ValueError, match="anchors"):
        TypedContextRequest.from_payload({"anchors": [*anchors, {"value": "extra"}]})


def test_anchors_batched_across_anchors_and_namespaces(catalog):
    """Все якоря и namespaces — одним заходом: журнал не больше трёх запросов, граф — два."""
    anchors = [
        ("POST /api/v1/goals/{}", "endpoint"),  # точный ключ — журнал не нужен
        ("/api/v1/goals/{goal_id}", ""),  # псевдоним
        ("POST /api/v1/goals/{id}", "endpoint"),  # нормализованная форма
        ("src/control_plane/api/v1/goals.py", "source_file"),  # суффикс
        ("work_rules", "table"),  # суффикс, неоднозначно
        ("payment-bug-alias", ""),  # псевдоним узла без журнала
    ]
    outs, graph, journal = _resolve_many(catalog, anchors, nss=(NS, NS2))
    assert [o.get("matchedBy") for o in outs] == [
        "natural_key",
        "alias",
        "normalized",
        "suffix",
        "suffix",
        "alias",
    ]
    assert journal.calls == ["keys", "aliases", "suffixes"]
    assert graph.calls == ["keys", "keys", "alias"]


def _mixed_rows():
    rows = [r for r in LEDGER_ROWS if r["namespace"] == NS]
    return rows + [_row("billing:work_rules", "table", ns=NS2, rid=50)]


def _mixed_nodes():
    return GRAPH_NODES + [
        {**_node("billing:work_rules", "table"), "namespace": NS2},
        {**_node("work_rules", "table"), "namespace": NS},
    ]


def test_ambiguous_counts_only_inexact_matches_across_namespaces(catalog):
    # NS — точный ключ ``work_rules``, NS2 — единственное совпадение суффиксом: выбора нет.
    rows = _mixed_rows() + [_row("work_rules", "table", rid=60)]
    outs, _, _ = _resolve_many(
        catalog, [("work_rules", "table")], rows=rows, nodes=_mixed_nodes(), nss=(NS, NS2)
    )
    out = outs[0]
    assert [(r["namespace"], r["method"]) for r in out["resolved"]] == [
        (NS, "natural_key"),
        (NS2, "suffix"),
    ]
    assert out["matchedBy"] == "natural_key"
    assert "ambiguous" not in out and "candidates" not in out


def test_ambiguous_across_namespaces_lists_inexact_candidates(catalog):
    outs, _, _ = _resolve_many(
        catalog,
        [("work_rules", "table")],
        rows=_mixed_rows(),
        nodes=[n for n in _mixed_nodes() if n["natural_key"] != "work_rules"],
        nss=(NS, NS2),
    )
    out = outs[0]
    assert out["ambiguous"] is True
    assert sorted((c["namespace"], c["natural_key"]) for c in out["candidates"]) == [
        (NS, "control-plane:work_rules"),
        (NS, "platform-web:work_rules"),
        (NS2, "billing:work_rules"),
    ]


def test_suffix_truncation_flagged_on_anchor(catalog):
    rows = [_row(f"repo{i:02d}:audit_log", "table", rid=100 + i) for i in range(12)]
    nodes = [_node(r["key"], "table") for r in rows]
    outs, _, _ = _resolve_many(
        catalog,
        [("audit_log", "table"), ("work_rules", "table")],
        rows=rows + LEDGER_ROWS,
        nodes=nodes + GRAPH_NODES,
    )
    many, few = outs
    assert many["matchedBy"] == "suffix" and len(many["resolved"]) == 10
    assert many["truncated"] is True and many["ambiguous"] is True
    assert len(few["resolved"]) == 2 and "truncated" not in few


def test_suffix_truncation_counts_only_matching_kind(catalog):
    """Сущности чужого вида с тем же суффиксом не дают ложного ``truncated`` (ревью 367 п.3)."""
    rows = [_row(f"repo{i:02d}:audit_log", "source_file", rid=100 + i) for i in range(12)]
    rows += [_row(f"svc{i}:audit_log", "table", rid=200 + i) for i in range(2)]
    nodes = [_node(r["key"], r["kind"]) for r in rows]
    outs, _, journal = _resolve_many(
        catalog, [("audit_log", "table")], rows=rows + LEDGER_ROWS, nodes=nodes + GRAPH_NODES
    )
    (out,) = outs
    assert out["matchedBy"] == "suffix" and _keys(out) == ["svc0:audit_log", "svc1:audit_log"]
    assert "truncated" not in out and out["ambiguous"] is True


def test_suffix_kinds_filter_before_limit(catalog):
    """Вид кандидата уходит фильтром в запрос суффиксов: чужие виды не занимают лимит."""
    rows = [_row(f"repo{i:02d}:audit_log", "source_file", rid=100 + i) for i in range(30)]
    rows += [_row("zz:audit_log", "table", rid=300)]
    nodes = [_node(r["key"], r["kind"]) for r in rows]
    outs, _, _ = _resolve_many(
        catalog, [("audit_log", "table")], rows=rows + LEDGER_ROWS, nodes=nodes + GRAPH_NODES
    )
    assert _keys(outs[0]) == ["zz:audit_log"] and "truncated" not in outs[0]


def test_truncated_on_exact_journal_hits_over_limit(catalog):
    """``truncated`` — и когда точному шагу журнала подошло больше MAX_RESOLVED сущностей."""
    rows = [
        _row(f"GET /api/v1/x{i}/{{}}", "endpoint", aliases=["shared-alias"], rid=400 + i)
        for i in range(25)
    ]
    nodes = [_node(r["key"], r["kind"], aliases=["shared-alias"]) for r in rows]
    outs, _, _ = _resolve_many(catalog, [("shared-alias", "endpoint")], rows=rows, nodes=nodes)
    assert outs[0]["matchedBy"] == "alias" and len(outs[0]["resolved"]) == 20
    assert outs[0]["truncated"] is True
