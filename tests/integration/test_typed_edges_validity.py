"""Шаг typed-обхода на настоящем AGE: валидность рёбер до лимита чтения (ревью 367 п.1).

Рёбра фронта читаются из таблицы метки связи с лимитом (``typed_neighbors``,
``max_edges``). Валидность на ``as_of``, ``evidence_lost`` и namespace ребра
проверяются в SQL до лимита: у хаба, накопившего за сверки закрытые версии рёбер с
меньшими id, действующие рёбра не вытесняются. Здесь же — псевдоним узла только в
графе (без журнала) и индексы концов рёбер.
"""

from __future__ import annotations

import pytest

from platform_memory.context.typed import compile_typed_context
from platform_memory.core.db import agtype
from platform_memory.graph import store as store_mod
from platform_memory.graph.store import GraphStore

pytestmark = pytest.mark.integration

NS = "tenant:t1:ws:w1"
OTHER = "tenant:t1:ws:other"
CLOSED = 30


def _cypher(conn, graph: str, query: str, params: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM cypher('{graph}', $$ {query} $$, %s) AS (v agtype)",
            (agtype(params),),
        )


@pytest.fixture
def hub(stores):
    """Хаб с CLOSED закрытыми рёбрами (меньшие id) и тремя действующими после них.

    Ещё два ребра после действующих не проходятся: ``evidence_lost`` и ребро с
    namespace другой базы.
    """
    conn, graph, _, _, _ = stores
    g = graph.graph
    _cypher(
        conn,
        g,
        "CREATE (:component {natural_key: 'hub', namespace: $ns, type: 'component', "
        "title: 'hub'}) RETURN 1",
        {"ns": NS},
    )

    def edges(prefix: str, n: int, extra: str) -> None:
        _cypher(
            conn,
            g,
            "MATCH (h:component {natural_key: 'hub', namespace: $ns}) "
            "UNWIND range(1, $n) AS i "
            f"CREATE (f:source_file {{natural_key: '{prefix}' + toString(i), namespace: $ns, "
            f"type: 'source_file', title: '{prefix}' + toString(i)}})"
            f"-[:PART_OF {{fact_id: '{prefix}' + toString(i), namespace: $ns, "
            f"valid_from: '2026-01-01T00:00:00Z'{extra}}}]->(h) RETURN 1",
            {"ns": NS, "n": n},
        )

    edges("old", CLOSED, ", valid_to: '2026-02-01T00:00:00Z'")
    edges("live", 3, "")
    edges("lost", 1, ", evidence_lost: true")
    _cypher(
        conn,
        g,
        "MATCH (h:component {natural_key: 'hub', namespace: $ns}) "
        "CREATE (:source_file {natural_key: 'alien', namespace: $ns, type: 'source_file'})"
        "-[:PART_OF {fact_id: 'alien', namespace: $other}]->(h) RETURN 1",
        {"ns": NS, "other": OTHER},
    )
    return conn, graph


def _neighbors(graph: GraphStore, **kw):
    (node,) = graph.lookup_nodes({"component": ["hub"]}, namespaces=[NS])
    return graph.typed_neighbors(
        {node["_gid"]: NS}, "part_of", ["in"], labels=graph.graph_labels(), **kw
    )


def _keys(rows) -> list[str]:
    return sorted(r[2]["natural_key"] for r in rows)


def test_closed_edges_do_not_crowd_out_live_ones(hub):
    _, graph = hub
    rows, truncated = _neighbors(graph, max_edges=5)
    assert _keys(rows) == ["live1", "live2", "live3"]
    assert truncated is False


def test_as_of_interval_filtered_before_limit_and_truncation_reported(hub):
    _, graph = hub
    # На as_of внутри интервала закрытых — валидны и они, и действующие: 33 > 5.
    rows, truncated = _neighbors(graph, as_of="2026-01-15T00:00:00Z", max_edges=5)
    assert len(rows) == 5 and truncated is True
    rows, truncated = _neighbors(graph, as_of="2026-01-15T00:00:00Z", max_edges=100)
    assert len(rows) == CLOSED + 3 and truncated is False
    # До начала интервалов — ни одного ребра.
    rows, _ = _neighbors(graph, as_of="2025-12-31T00:00:00Z", max_edges=5)
    assert rows == []


def test_typed_context_step_reports_truncation(hub, settings, monkeypatch):
    conn, _ = hub
    real = GraphStore.typed_neighbors

    def small(self, *a, **kw):
        return real(self, *a, **{**kw, "max_edges": 5})

    monkeypatch.setattr(GraphStore, "typed_neighbors", small)
    payload = {
        "namespaces": [NS],
        "anchors": [{"value": "hub"}],
        "traverse": [{"relation": "part_of", "direction": "in", "limit": 50}],
    }
    pack = compile_typed_context(settings, payload, conn=conn)
    (step,) = pack["stats"]["steps"]
    assert step["reached"] == 3 and "truncated" not in step
    assert sorted(f["fact_id"] for f in pack["facts"]) == ["live1", "live2", "live3"]
    assert set(pack["stats"]["phases_ms"]) >= {"setup", "anchors", "traverse", "assemble"}

    pack = compile_typed_context(settings, {**payload, "as_of": "2026-01-15T00:00:00Z"}, conn=conn)
    (step,) = pack["stats"]["steps"]
    assert step["reached"] == 5 and step["truncated"] is True


def test_graph_only_alias_resolves_in_age(stores, settings):
    """Псевдоним узла без журнала (props.aliases) — совпадение в настоящем AGE (ревью 367 п.4)."""
    conn, graph, _, _, _ = stores
    _cypher(
        conn,
        graph.graph,
        "CREATE (:note {natural_key: 'note:payment-bug', namespace: $ns, type: 'note', "
        "title: 'payment bug', props: {aliases: ['payment-bug-alias']}}) RETURN 1",
        {"ns": NS},
    )
    _cypher(
        conn,
        graph.graph,
        "CREATE (:note {natural_key: 'note:elsewhere', namespace: $other, type: 'note', "
        "title: 'x', props: {aliases: ['payment-bug-alias']}}) RETURN 1",
        {"other": OTHER},
    )
    for anchor in ({"value": "payment-bug-alias"}, {"kind": "note", "value": "payment-bug-alias"}):
        pack = compile_typed_context(settings, {"namespaces": [NS], "anchors": [anchor]}, conn=conn)
        (out,) = pack["anchors"]
        assert out["matchedBy"] == "alias", out
        assert [r["natural_key"] for r in out["resolved"]] == ["note:payment-bug"]


def test_edge_label_gets_endpoint_indexes(stores):
    conn, graph, _, _, _ = stores
    assert graph.ensure_label_index("PART_OF", edge=True)
    names = store_mod._label_index_names("PART_OF", edge=True)
    assert names == ["PART_OF_pm_props_gin", "PART_OF_pm_start", "PART_OF_pm_end"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (graph.graph, "PART_OF"),
        )
        defs = dict(cur.fetchall())
    assert "(start_id)" in defs["PART_OF_pm_start"] and "(end_id)" in defs["PART_OF_pm_end"]
