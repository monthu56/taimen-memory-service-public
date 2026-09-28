"""Фильтры ``where`` типизированного обхода (MEM-ADR-020, амендмент 2026-09-28, п.3; K006).

Операторы — чистые функции ``context/where.py``; якоря и шаги обхода — ``_Compiler`` на
графе в памяти (сущности без журнала: атрибуты — свойства узла), без БД.
"""

from __future__ import annotations

import pytest

from platform_memory.context.typed import TraverseStep, TypedContextRequest, _Compiler
from platform_memory.context.where import MAX_WHERE_CLAUSES, matches, parse_where
from platform_memory.core.kinds import KindCatalog, parse_pack

pytestmark = pytest.mark.unit

NS = "tenant:t1:ws:w1"


def _ok(attrs: dict, *clauses: dict) -> bool:
    return matches(attrs, parse_where(list(clauses)))


class TestOperators:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("62.01", True),
            ("62.01.11", True),
            ("62.01.11.000", True),
            ("62.011", False),
            ("62.0", False),
            ("62", False),
            ("63.01.11", False),
        ],
    )
    def test_prefix_by_segments_okpd2(self, code, expected):
        assert _ok({"okpd2": code}, {"attr": "okpd2", "op": "prefix", "value": "62.01"}) is expected

    def test_prefix_single_segment_and_non_string(self):
        clause = {"attr": "okpd2", "op": "prefix", "value": "62"}
        assert _ok({"okpd2": "62.01.11"}, clause)
        assert not _ok({"okpd2": "620.1"}, clause)
        assert not _ok({"okpd2": 62.01}, clause)

    @pytest.mark.parametrize(
        ("valid_until", "expected"),
        [
            ("2026-12-31", True),
            ("2026-12-31T23:59:59Z", False),  # позже полуночи UTC даты условия
            ("2026-12-31T00:00:00Z", True),  # включительно
            ("2026-12-31T02:00:00+03:00", True),  # 2026-12-30T23:00Z
            ("2026-06-01T12:00:00", True),  # без зоны — UTC
            ("2027-01-01", False),
            ("не дата", False),
            (20261231, False),  # число с датой не сравнивается
        ],
    )
    def test_lte_date_valid_until(self, valid_until, expected):
        clause = {"attr": "validUntil", "op": "lte", "value": "2026-12-31"}
        assert _ok({"validUntil": valid_until}, clause) is expected

    def test_gte_date_with_time_and_zone(self):
        clause = {"attr": "validUntil", "op": "gte", "value": "2026-10-01T00:00:00+03:00"}
        assert _ok({"validUntil": "2026-09-30T21:00:00Z"}, clause)
        assert not _ok({"validUntil": "2026-09-30"}, clause)

    def test_lte_gte_numbers(self):
        assert _ok({"price": 100}, {"attr": "price", "op": "lte", "value": 100})
        assert _ok({"price": 99.5}, {"attr": "price", "op": "lte", "value": 100})
        assert not _ok({"price": 100.01}, {"attr": "price", "op": "lte", "value": 100})
        assert _ok({"price": 5}, {"attr": "price", "op": "gte", "value": 5.0})
        # Строка с числом — другой класс; bool — не число.
        assert not _ok({"price": "100"}, {"attr": "price", "op": "lte", "value": 1000})
        assert not _ok({"price": True}, {"attr": "price", "op": "gte", "value": 0})
        # Диапазон — два условия по «И».
        both = [
            {"attr": "price", "op": "gte", "value": 10},
            {"attr": "price", "op": "lte", "value": 20},
        ]
        assert _ok({"price": 15}, *both)
        assert not _ok({"price": 25}, *both)

    def test_eq_numbers_numeric_rest_exact(self):
        assert _ok({"qty": 1.0}, {"attr": "qty", "op": "eq", "value": 1})
        assert not _ok({"qty": "1"}, {"attr": "qty", "op": "eq", "value": 1})
        assert _ok({"status": "active"}, {"attr": "status", "op": "eq", "value": "active"})
        assert not _ok({"status": "Active"}, {"attr": "status", "op": "eq", "value": "active"})
        assert _ok({"vat": True}, {"attr": "vat", "op": "eq", "value": True})
        assert not _ok({"vat": 1}, {"attr": "vat", "op": "eq", "value": True})
        assert not _ok({"vat": True}, {"attr": "vat", "op": "eq", "value": 1})

    def test_in_any_of(self):
        clause = {"attr": "unit", "op": "in", "value": ["796", "839", 5]}
        assert _ok({"unit": "839"}, clause)
        assert _ok({"unit": 5.0}, clause)
        assert not _ok({"unit": "166"}, clause)

    def test_exists(self):
        present = {"attr": "ktru", "op": "exists"}
        absent = {"attr": "ktru", "op": "exists", "value": False}
        assert _ok({"ktru": "26.20.11.110-00000001"}, present)
        assert _ok({"ktru": ""}, present)  # пустая строка — значение есть
        for attrs in ({}, {"ktru": None}, {"ktru": []}):
            assert not _ok(attrs, present)
            assert _ok(attrs, absent)
        assert _ok({"ktru": "x"}, {"attr": "ktru", "op": "exists", "value": True})

    def test_list_attribute_any_element(self):
        attrs = {"okpd2": ["26.20.11", "62.01.29"], "tags": []}
        assert _ok(attrs, {"attr": "okpd2", "op": "prefix", "value": "62.01"})
        assert _ok(attrs, {"attr": "okpd2", "op": "eq", "value": "26.20.11"})
        assert not _ok(attrs, {"attr": "okpd2", "op": "prefix", "value": "63"})
        assert not _ok(attrs, {"attr": "tags", "op": "eq", "value": "x"})

    def test_missing_attribute_fails_everything_but_exists_false(self):
        for clause in (
            {"attr": "okpd2", "op": "eq", "value": "62.01"},
            {"attr": "okpd2", "op": "in", "value": ["62.01"]},
            {"attr": "okpd2", "op": "prefix", "value": "62"},
            {"attr": "okpd2", "op": "lte", "value": 1},
            {"attr": "okpd2", "op": "gte", "value": "2026-01-01"},
            {"attr": "okpd2", "op": "exists"},
        ):
            assert not _ok({}, clause), clause
            assert not _ok({"okpd2": None}, clause), clause
        assert _ok({}, {"attr": "okpd2", "op": "exists", "value": False})

    def test_all_clauses_and(self):
        attrs = {"okpd2": "62.01.11", "status": "active"}
        assert _ok(
            attrs,
            {"attr": "okpd2", "op": "prefix", "value": "62.01"},
            {"attr": "status", "op": "eq", "value": "active"},
        )
        assert not _ok(
            attrs,
            {"attr": "okpd2", "op": "prefix", "value": "62.01"},
            {"attr": "status", "op": "eq", "value": "archived"},
        )
        assert matches(attrs, [])  # без условий — всё проходит


class TestValidation:
    @pytest.mark.parametrize(
        ("clause", "error"),
        [
            ({"attr": "bad attr", "op": "eq", "value": 1}, "attr"),
            ({"op": "eq", "value": 1}, "attr"),
            ({"attr": "a", "op": "like", "value": "x"}, "op"),
            ({"attr": "a", "op": "eq"}, "eq"),
            ({"attr": "a", "op": "eq", "value": [1]}, "eq"),
            ({"attr": "a", "op": "eq", "value": {"x": 1}}, "eq"),
            ({"attr": "a", "op": "in", "value": []}, "in"),
            ({"attr": "a", "op": "in", "value": "62"}, "in"),
            ({"attr": "a", "op": "in", "value": list(range(101))}, "in"),
            ({"attr": "a", "op": "in", "value": [None]}, "in"),
            ({"attr": "a", "op": "prefix", "value": ""}, "prefix"),
            ({"attr": "a", "op": "prefix", "value": ".62"}, "prefix"),
            ({"attr": "a", "op": "prefix", "value": "62."}, "prefix"),
            ({"attr": "a", "op": "prefix", "value": "62..01"}, "prefix"),
            ({"attr": "a", "op": "prefix", "value": 62}, "prefix"),
            ({"attr": "a", "op": "lte", "value": "вчера"}, "lte"),
            ({"attr": "a", "op": "gte", "value": True}, "gte"),
            ({"attr": "a", "op": "gte"}, "gte"),
            ({"attr": "a", "op": "exists", "value": "yes"}, "exists"),
            ("a = 1", "объект"),
        ],
    )
    def test_invalid_clause_is_value_error(self, clause, error):
        with pytest.raises(ValueError, match=error):
            parse_where([clause])

    def test_limits_and_shape(self):
        assert parse_where(None) == []
        assert len(parse_where([{"attr": "a", "op": "exists"}] * MAX_WHERE_CLAUSES)) == 20
        with pytest.raises(ValueError, match="не больше 20"):
            parse_where([{"attr": "a", "op": "exists"}] * 21)
        with pytest.raises(ValueError, match="список"):
            parse_where({"attr": "a", "op": "exists"})
        assert len(parse_where([{"attr": "a", "op": "in", "value": list(range(100))}])) == 1

    def test_request_parses_top_level_and_step_where(self):
        req = TypedContextRequest.from_payload(
            {
                "anchors": ["ACME"],
                "where": [{"attr": "okpd2", "op": "prefix", "value": "62.01"}],
                "traverse": [
                    {
                        "relation": "holds",
                        "where": [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}],
                    },
                    {"relation": "holds"},
                ],
            }
        )
        assert [c.op for c in req.where] == ["prefix"]
        assert [c.attr for c in req.traverse[0].where] == ["validUntil"]
        assert req.traverse[1].where == []
        with pytest.raises(ValueError, match=r"traverse\[0\]\.where\[0\]\.value"):
            TypedContextRequest.from_payload(
                {
                    "anchors": ["ACME"],
                    "traverse": [
                        {"relation": "holds", "where": [{"attr": "a", "op": "lte", "value": "x"}]}
                    ],
                }
            )


# --- якоря и обход на графе в памяти ---

CATALOG = KindCatalog.build(
    [
        parse_pack(
            {
                "name": "company",
                "version": "1",
                "kinds": [{"kind": "item"}, {"kind": "company"}, {"kind": "license"}],
                "relations": [{"relation": "holds"}, {"relation": "replaces"}],
            }
        )
    ]
)


def _node(gid: int, key: str, kind: str, *, aliases=(), **attrs) -> dict:
    return {
        "_gid": gid,
        "namespace": NS,
        "type": kind,
        "natural_key": key,
        "title": key,
        "props": {"aliases": list(aliases), **attrs},
    }


class Graph:
    """Граф в памяти: поиск узлов по ключу/псевдониму и рёбра одной связи."""

    def __init__(self, nodes, edges=(), *, alias_nodes=None):
        self.nodes = nodes
        # Узлы, которые находит поиск по псевдониму (по умолчанию — все).
        self.alias_nodes = nodes if alias_nodes is None else alias_nodes
        self.by_gid = {n["_gid"]: n for n in nodes}
        self.edges = list(edges)  # (src_gid, dst_gid, relation)

    def graph_labels(self):
        labels = sorted({n["type"] for n in self.nodes})
        return {100 + i: (label, "v") for i, label in enumerate(labels)}

    def lookup_nodes(self, keys_by_label, aliases_by_label=None, namespaces=()):
        aliases_by_label = aliases_by_label or {}
        by_key = [n for n in self.nodes if n["natural_key"] in keys_by_label.get(n["type"], ())]
        by_alias = [
            n
            for n in self.alias_nodes
            if set(n["props"]["aliases"]) & set(aliases_by_label.get(n["type"], ()))
        ]
        return list({id(n): n for n in by_key + by_alias}.values())

    def nodes_by_keys(self, keys, namespaces=()):
        return [n for n in self.nodes if n["natural_key"] in set(keys)]

    def typed_neighbors(self, gids, relation, directions, *, labels=None, as_of=""):
        rows = []
        for direction in directions:
            for rid, (src, dst, rel) in enumerate(self.edges):
                if rel != relation:
                    continue
                here, there = (src, dst) if direction == "out" else (dst, src)
                if here in gids:
                    fact = {"fact_id": f"{rel}:{src}-{dst}", "namespace": NS}
                    rows.append((here, direction, self.by_gid[there], fact, rid))
        return rows, False


def _compiler(graph, anchors, *, where=None, allow_semantic=False):
    req = TypedContextRequest.from_payload(
        {
            "anchors": anchors,
            "namespaces": [NS],
            "allow_semantic": allow_semantic,
            **({"where": where} if where is not None else {}),
        }
    )
    return _Compiler(graph, None, {NS: CATALOG}, req, None), req


ITEMS = [
    _node(1, "item:1", "item", aliases=["ноутбук"], okpd2="62.01.11"),
    _node(2, "item:2", "item", aliases=["ноутбук"], okpd2="62.011"),
    _node(3, "item:3", "item", aliases=["ноутбук"], okpd2="26.20.11"),
]
OKPD2 = [{"attr": "okpd2", "op": "prefix", "value": "62.01"}]


class TestAnchorsWhere:
    def test_candidates_filtered_and_counted(self):
        compiler, req = _compiler(Graph(ITEMS), [{"kind": "item", "value": "ноутбук"}], where=OKPD2)
        (out,) = compiler.resolve_anchors(req.anchors, lambda *_: [])
        assert [r["natural_key"] for r in out["resolved"]] == ["item:1"]
        assert out["matchedBy"] == "alias"
        assert out["filtered"] == 2
        # Отброшенные кандидаты в выдачу не попадают.
        assert set(compiler.entities) == {(NS, "item:1")}

    def test_no_filtered_field_without_where(self):
        compiler, req = _compiler(Graph(ITEMS), [{"kind": "item", "value": "ноутбук"}])
        (out,) = compiler.resolve_anchors(req.anchors, lambda *_: [])
        assert len(out["resolved"]) == 3 and "filtered" not in out

    def test_all_filtered_goes_to_next_step_including_semantic(self):
        """Шаг без прошедших кандидатов неуспешен: разрешение идёт дальше, до смыслового."""
        nodes = [ITEMS[1], _node(4, "item:4", "item", okpd2="62.01.29")]
        compiler, req = _compiler(
            Graph([*nodes, ITEMS[2]], alias_nodes=nodes),
            [{"value": "ноутбук"}],
            where=OKPD2,
            allow_semantic=True,
        )
        seen: list[str] = []

        def semantic(value, ns, kinds):
            # Индекс сущностей (K005): (вид, ключ) и score; фрагментов нет.
            seen.append(value)
            return [(("item", "item:4"), 0.9), (("item", "item:3"), 0.8)], []

        (out,) = compiler.resolve_anchors(req.anchors, semantic)
        assert seen == ["ноутбук"]
        assert [r["natural_key"] for r in out["resolved"]] == ["item:4"]
        assert out["resolved"][0]["method"] == "semantic"
        # item:2 — по псевдониму, item:3 — смысловой кандидат.
        assert out["filtered"] == 2

    def test_nothing_left_is_unresolved(self):
        compiler, req = _compiler(
            Graph(ITEMS),
            [{"kind": "item", "value": "ноутбук"}],
            where=[{"attr": "okpd2", "op": "prefix", "value": "99"}],
        )
        (out,) = compiler.resolve_anchors(req.anchors, lambda *_: [])
        assert out["resolved"] == [] and out["filtered"] == 3
        assert compiler.entities == {}


class TestTraverseWhere:
    """ACME -holds-> лицензии; истёкшая лицензия дальше по replaces не ведёт."""

    NODES = [
        _node(10, "acme", "company"),
        _node(11, "lic:old", "license", validUntil="2026-06-30"),
        _node(12, "lic:new", "license", validUntil="2027-06-30"),
        _node(13, "lic:none", "license"),
        _node(14, "lic:older", "license", validUntil="2025-01-01"),
    ]
    EDGES = [
        (10, 11, "holds"),
        (10, 12, "holds"),
        (10, 13, "holds"),
        (11, 14, "replaces"),
    ]

    def _run(self, *steps: TraverseStep):
        compiler, req = _compiler(Graph(self.NODES, self.EDGES), [{"value": "acme"}])
        anchors = compiler.resolve_anchors(req.anchors, lambda *_: [])
        start = [(r["namespace"], r["natural_key"]) for a in anchors for r in a["resolved"]]
        previous = start
        for i, step in enumerate(steps):
            previous = compiler.traverse(i, step, start if step.start == "anchors" else previous)
        return compiler

    def test_valid_until_lte_date(self):
        step = TraverseStep(
            "holds", where=parse_where([{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}])
        )
        compiler = self._run(step)
        assert sorted(k for _, k in compiler.entities) == ["acme", "lic:old"]
        assert list(compiler.facts) == ["holds:10-11"]
        assert compiler.filtered == {0: 2}

    def test_filtered_entity_does_not_continue_traversal(self):
        valid_now = parse_where([{"attr": "validUntil", "op": "gte", "value": "2026-09-28"}])
        compiler = self._run(
            TraverseStep("holds", where=valid_now),
            TraverseStep("replaces", start="previous"),
        )
        # lic:old истекла: не в выдаче, и lic:older через неё не достигается.
        assert sorted(k for _, k in compiler.entities) == ["acme", "lic:new"]
        assert list(compiler.facts) == ["holds:10-12"]

    def test_filtered_do_not_count_towards_limit(self):
        step = TraverseStep(
            "holds",
            limit=1,
            where=parse_where([{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}]),
        )
        compiler = self._run(step)
        # Рёбра идут по ключу: lic:new и lic:none отброшены раньше, лимит заняла lic:old.
        assert sorted(k for _, k in compiler.entities) == ["acme", "lic:old"]

    def test_step_where_does_not_touch_other_steps(self):
        expired = parse_where([{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}])
        compiler = self._run(
            TraverseStep("holds", where=expired),
            TraverseStep("holds"),
        )
        assert sorted(k for _, k in compiler.entities) == [
            "acme",
            "lic:new",
            "lic:none",
            "lic:old",
        ]
        assert compiler.filtered == {0: 2, 1: 0}
