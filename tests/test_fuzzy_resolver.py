"""Тесты fuzzy entity resolution (вендорено из graphify, адаптировано под Node)."""

from __future__ import annotations

import pytest

from platform_memory.core.models import Edge, Node
from platform_memory.ingest.fuzzy_resolver import (
    MergeCandidate,
    apply_merges,
    identity_keys,
    resolve_entities,
)

pytestmark = pytest.mark.unit


def _person(nk: str, title: str, **props) -> Node:
    return Node(type="person", natural_key=nk, title=title, properties=dict(props))


def _org(nk: str, title: str, **props) -> Node:
    return Node(type="org", natural_key=nk, title=title, properties=dict(props))


# ── identity_keys ───────────────────────────────────────────────────────────────


def test_identity_keys_natural_id():
    assert "biz-010" in identity_keys(Node(type="decision", natural_key="BIZ-010", title="x"))


def test_identity_keys_props():
    n = _person("person:ivan-1", "Иван Петров", inn="770101", email="Ivan@X.ru")
    keys = identity_keys(n)
    assert "inn:770101" in keys
    assert "email:ivan@x.ru" in keys  # casefold


def test_free_entity_has_no_identity():
    assert identity_keys(_person("person:ivan-1", "Иван Петров")) == set()


# ── авто-слияние свободных сущностей ────────────────────────────────────────────


def test_exact_normalized_titles_auto_merge():
    nodes = [
        _org("org:a", "ООО «Ромашка»"),
        _org("org:b", "ооо ромашка"),  # та же норма после _norm
    ]
    res = resolve_entities(nodes)
    assert res.merged_count == 1
    # один из двух становится survivor, второй — в merges
    assert set(res.merges.keys()) | set(res.merges.values()) == {"org:a", "org:b"}


def test_high_similarity_auto_merges():
    nodes = [
        _org("org:a", "Telegram Messenger LLC"),
        _org("org:b", "Telegram Messengr LLC"),  # опечатка, длинная метка
    ]
    res = resolve_entities(nodes)
    assert res.merged_count == 1


# ── приоритет natural-key / арбитраж ────────────────────────────────────────────


def test_conflicting_inn_never_merges():
    # Одинаковое имя, но разные ИНН — это разные юрлица, слияния быть не должно.
    nodes = [
        _org("org:a", "ООО Ромашка", inn="111"),
        _org("org:b", "ООО Ромашка", inn="222"),
    ]
    res = resolve_entities(nodes)
    assert res.merges == {}
    assert res.candidates == []


def test_anchored_node_is_survivor():
    # Заякоренный (с email) должен победить свободного с тем же именем.
    nodes = [
        _person("person:free", "Иван Петров"),
        _person("person:anchored", "Иван Петров", email="ivan@x.ru"),
    ]
    res = resolve_entities(nodes)
    assert res.merges == {"person:free": "person:anchored"}


def test_shared_inn_allows_merge():
    # Один и тот же ИНН + похожие имена — слияние разрешено (идентичности пересекаются).
    nodes = [
        _org("org:a", "Сбербанк России", inn="7707083893"),
        _org("org:b", "Сбербанк Рассии", inn="7707083893"),
    ]
    res = resolve_entities(nodes)
    assert res.merged_count == 1


# ── community boost ─────────────────────────────────────────────────────────────


def test_community_boost_promotes_to_merge():
    a = _org("org:a", "Алгоритмика Холдинг")
    b = _org("org:b", "Алгоритмика Холднг")  # лёгкая опечатка
    without = resolve_entities([a, b])
    with_comm = resolve_entities([a, b], communities={"org:a": 1, "org:b": 1})
    # community-буст не должен уменьшать число слияний
    assert with_comm.merged_count >= without.merged_count


# ── гарды коротких/вариантных меток ─────────────────────────────────────────────


def test_short_variant_labels_not_merged():
    # M1 / M2 — варианты, не дубликаты.
    nodes = [_org("org:a", "M1"), _org("org:b", "M2")]
    res = resolve_entities(nodes)
    assert res.merges == {}


def test_prefix_extension_not_auto_merged():
    nodes = [
        _org("org:a", "ИнвестКапитал"),
        _org("org:b", "ИнвестКапиталГрупп"),  # строгое префиксное расширение
    ]
    res = resolve_entities(nodes)
    assert "org:a" not in res.merges and "org:b" not in res.merges


# ── apply_merges ────────────────────────────────────────────────────────────────


def test_apply_merges_rewires_edges_and_drops_loops():
    nodes = [_org("org:a", "X"), _org("org:b", "X"), _person("p:1", "Z")]
    edges = [
        Edge(type="LINKS_TO", src="p:1", dst="org:b"),
        Edge(type="LINKS_TO", src="org:a", dst="org:b"),  # станет петлёй
    ]
    merges = {"org:b": "org:a"}
    out_nodes, out_edges = apply_merges(nodes, edges, merges)
    assert {n.natural_key for n in out_nodes} == {"org:a", "p:1"}
    # петля org:a->org:a удалена; ребро p:1->org:b перенаправлено на org:a
    assert [(e.src, e.dst) for e in out_edges] == [("p:1", "org:a")]


def test_empty_and_singleton_inputs():
    assert resolve_entities([]).merges == {}
    assert resolve_entities([_org("org:a", "X")]).merges == {}


def test_candidate_dataclass_shape():
    c = MergeCandidate(survivor="a", duplicate="b", score=80.0, reason="fuzzy")
    assert c.survivor == "a" and c.reason == "fuzzy"


# ── петля обратной связи арбитража ──────────────────────────────────────────────


def test_forced_merge_unions_even_conflicting_identity():
    # Арбитр одобрил слияние двух орг с разными ИНН — решение человека важнее гарда.
    nodes = [
        _org("org:a", "Ромашка Северная", inn="111"),
        _org("org:b", "Ромашка Южная", inn="222"),
    ]
    res = resolve_entities(nodes, forced_merges=[("org:a", "org:b")])
    assert res.merged_count == 1


def test_blocked_pair_not_auto_merged():
    # Отклонённую пару не сливаем, даже если заголовки идентичны (точная норма).
    nodes = [_org("org:a", "Acme Corp"), _org("org:b", "Acme Corp")]
    res = resolve_entities(nodes, blocked_pairs={frozenset({"org:a", "org:b"})})
    assert res.merges == {}


def test_blocked_pair_not_offered_as_candidate():
    a = _org("org:a", "Алгоритмика Холдинг")
    b = _org("org:b", "Алгоритмика Холднг")  # арбитражная близость
    with_block = resolve_entities([a, b], blocked_pairs={frozenset({"org:a", "org:b"})})
    assert with_block.merges == {}
    assert all(
        frozenset({c.survivor, c.duplicate}) != frozenset({"org:a", "org:b"})
        for c in with_block.candidates
    )
