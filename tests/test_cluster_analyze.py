"""Тесты кластеризации и анализа графа (вендорено из graphify, адаптировано под наш граф)."""

from __future__ import annotations

import networkx as nx
import pytest

from platform_memory.graph.analyze import (
    cross_community_connections,
    god_nodes,
    suggest_questions,
)
from platform_memory.graph.cluster import (
    cluster,
    cohesion_score,
    remap_communities_to_previous,
    score_all,
)

pytestmark = pytest.mark.unit


def _two_clusters() -> nx.Graph:
    """Две плотные клики A/B, соединённые одним мостом."""
    G = nx.Graph()
    for n in ("a1", "a2", "a3"):
        G.add_node(n, label=n, type="task", source_file=f"{n}.md")
    for n in ("b1", "b2", "b3"):
        G.add_node(n, label=n, type="decision", source_file=f"{n}.md")
    for u, v in [("a1", "a2"), ("a2", "a3"), ("a1", "a3")]:
        G.add_edge(u, v, relation="LINKS_TO")
    for u, v in [("b1", "b2"), ("b2", "b3"), ("b1", "b3")]:
        G.add_edge(u, v, relation="LINKS_TO")
    G.add_edge("a1", "b1", relation="LINKS_TO")  # мост
    return G


# ── cluster ─────────────────────────────────────────────────────────────────────


def test_cluster_empty_graph():
    assert cluster(nx.Graph()) == {}


def test_cluster_no_edges_each_node_isolated():
    G = nx.Graph()
    G.add_nodes_from(["x", "y"])
    out = cluster(G)
    assert sorted(len(v) for v in out.values()) == [1, 1]


def test_cluster_finds_two_communities():
    out = cluster(_two_clusters())
    # все 6 узлов распределены; ожидаем минимум 2 сообщества
    all_nodes = {n for nodes in out.values() for n in nodes}
    assert all_nodes == {"a1", "a2", "a3", "b1", "b2", "b3"}
    assert len(out) >= 2


def test_cluster_is_deterministic():
    G = _two_clusters()
    assert cluster(G) == cluster(G)


def test_cluster_accepts_directed():
    G = _two_clusters().to_directed()
    out = cluster(G)
    assert {n for nodes in out.values() for n in nodes} == set(G.nodes())


# ── cohesion ────────────────────────────────────────────────────────────────────


def test_cohesion_full_clique_is_one():
    G = _two_clusters()
    assert cohesion_score(G, ["a1", "a2", "a3"]) == 1.0


def test_cohesion_singleton_is_one():
    assert cohesion_score(nx.Graph(), ["solo"]) == 1.0


def test_score_all_returns_per_community():
    G = _two_clusters()
    comms = cluster(G)
    scores = score_all(G, comms)
    assert set(scores.keys()) == set(comms.keys())


# ── remap ───────────────────────────────────────────────────────────────────────


def test_remap_preserves_ids_by_overlap():
    new = {0: ["b1", "b2", "b3"], 1: ["a1", "a2", "a3"]}
    prev = {"a1": 0, "a2": 0, "a3": 0, "b1": 1, "b2": 1, "b3": 1}
    out = remap_communities_to_previous(new, prev)
    # сообщество с a* должно получить id 0 (совпадение с prev), b* — id 1
    assert set(out[0]) == {"a1", "a2", "a3"}
    assert set(out[1]) == {"b1", "b2", "b3"}


# ── god_nodes ───────────────────────────────────────────────────────────────────


def test_god_nodes_ranks_by_degree():
    G = _two_clusters()
    gods = god_nodes(G, top_n=2)
    # a1 и b1 имеют по 3 ребра (включая мост) — самые связанные
    top_labels = {g["label"] for g in gods}
    assert "a1" in top_labels


def test_god_nodes_excludes_hub_types():
    G = nx.Graph()
    G.add_node("project:nexus", label="nexus", type="project")
    G.add_node("T-1", label="T-1", type="task")
    G.add_edge("project:nexus", "T-1")
    G.add_edge("project:nexus", "T-2")
    G.add_node("T-2", label="T-2", type="task")
    gods = god_nodes(G, top_n=5)
    assert all(g["type"] != "project" for g in gods)


# ── cross_community_connections ─────────────────────────────────────────────────


def test_cross_community_finds_bridge():
    G = _two_clusters()
    comms = {0: ["a1", "a2", "a3"], 1: ["b1", "b2", "b3"]}
    bridges = cross_community_connections(G, comms)
    assert len(bridges) == 1
    pair = {bridges[0]["source"], bridges[0]["target"]}
    assert pair == {"a1", "b1"}


def test_cross_community_betweenness_fallback_without_communities():
    G = _two_clusters()
    out = cross_community_connections(G, None, top_n=3)
    assert out and all("betweenness" in o["note"] for o in out)


# ── suggest_questions ───────────────────────────────────────────────────────────


def test_suggest_questions_returns_signals():
    G = _two_clusters()
    comms = {0: ["a1", "a2", "a3"], 1: ["b1", "b2", "b3"]}
    qs = suggest_questions(G, comms)
    assert qs
    assert all("question" in q and "why" in q for q in qs)


def test_suggest_questions_no_signal_on_trivial_graph():
    # Единственный узел — синтетический хаб: исключён из всех сигналов → no_signal.
    G = nx.Graph()
    G.add_node("project:nexus", label="nexus", type="project")
    qs = suggest_questions(G, {0: ["project:nexus"]})
    assert qs[0]["type"] == "no_signal"
