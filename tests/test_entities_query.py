"""Перечень сущностей вида (K030): разбор запроса, курсор и страницы без БД.

Журнал подменяется фейком с тем же контрактом ``entity_page``: порядок
``(kind, key, namespace)``, строки строго после ``after``, не больше ``limit``.
"""

from __future__ import annotations

import pytest

from platform_memory.context import entities as mod
from platform_memory.context.entities import (
    EntityQueryRequest,
    decode_cursor,
    encode_cursor,
    page_entities,
)

pytestmark = pytest.mark.unit


class _Ledger:
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: (r["kind"], r["key"], r["namespace"]))
        self.calls = 0

    def entity_page(self, namespaces, kinds, *, as_of, allowed_scopes, after, limit):
        self.calls += 1
        out = [
            r
            for r in self.rows
            if r["kind"] in kinds
            and r["namespace"] in namespaces
            and (after is None or (r["kind"], r["key"], r["namespace"]) > after)
        ]
        return out[:limit]


def _row(kind, key, ns="ns", **attrs):
    return {
        "kind": kind,
        "key": key,
        "namespace": ns,
        "source": "s",
        "scope": "",
        "snapshot_id": "1",
        "payload": {"attributes": attrs},
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": None,
    }


def _req(**payload):
    payload.setdefault("kinds", ["k"])
    req = EntityQueryRequest.from_payload(payload)
    req.namespaces = req.namespaces or ["ns"]
    return req


def _walk(ledger, **payload):
    seen, cursor = [], None
    for _ in range(1000):
        items, cursor, _ = page_entities(ledger, _req(cursor=cursor, **payload), None)
        assert len(items) <= payload.get("limit", mod.DEFAULT_LIMIT)
        seen += [(r["key"], r["namespace"]) for r in items]
        if cursor is None:
            return seen
    raise AssertionError("курсор не дошёл до конца")


class TestParse:
    def test_defaults_and_camel_case(self):
        req = EntityQueryRequest.from_payload(
            {"kinds": ["license", "license"], "asOf": "2026-09-01T03:00:00+03:00"}
        )
        assert req.kinds == ["license"]
        assert req.as_of == "2026-09-01T00:00:00Z"
        assert (req.limit, req.after, req.where, req.allowed_scopes) == (100, None, [], None)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"kinds": []},
            {"kinds": [f"k{i}" for i in range(21)]},
            {"kinds": ["bad kind"]},
            {"kinds": ["k"], "limit": 0},
            {"kinds": ["k"], "limit": 501},
            {"kinds": ["k"], "limit": "10"},
            {"kinds": ["k"], "asOf": "вчера"},
            {"kinds": ["k"], "where": [{"attr": "a", "op": "lte", "value": "x"}]},
            {"kinds": ["k"], "cursor": "не-курсор"},
            {"kinds": ["k"], "cursor": 5},
        ],
    )
    def test_invalid(self, payload):
        with pytest.raises(ValueError):
            EntityQueryRequest.from_payload(payload)


class TestCursor:
    def test_round_trip(self):
        pos = ("license", "лиц/1:a", "tenant:t1")
        assert decode_cursor(encode_cursor(pos)) == pos
        assert decode_cursor(None) is None and decode_cursor("") is None

    def test_foreign_json_rejected(self):
        import base64

        foreign = base64.urlsafe_b64encode(b'["license","k","ns"]').decode().rstrip("=")
        with pytest.raises(ValueError):
            decode_cursor(foreign)


class TestPages:
    def test_where_filter_and_order(self):
        ledger = _Ledger(
            [
                _row("k", "b", validUntil="2026-06-30"),
                _row("k", "a", validUntil="2027-01-01"),
                _row("k", "c", validUntil="2026-12-31"),
                _row("k", "d"),
                _row("other", "a", validUntil="2026-01-01"),
            ]
        )
        where = [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}]
        items, cursor, _ = page_entities(ledger, _req(where=where), None)
        assert [r["key"] for r in items] == ["b", "c"] and cursor is None

    @pytest.mark.parametrize("limit", [1, 2, 3, 7, 10, 11])
    def test_cursor_no_gaps_no_repeats(self, limit):
        rows = [_row("k", f"{i:02d}", ns, even=i % 2 == 0) for i in range(10) for ns in "xy"]
        ledger = _Ledger(rows)
        expected = sorted((r["key"], r["namespace"]) for r in rows)
        assert _walk(ledger, limit=limit, namespaces=["x", "y"]) == expected
        even = [x for x in expected if int(x[0]) % 2 == 0]
        where = [{"attr": "even", "op": "eq", "value": True}]
        assert _walk(ledger, limit=limit, namespaces=["x", "y"], where=where) == even

    def test_full_last_page_has_no_cursor(self):
        ledger = _Ledger([_row("k", str(i)) for i in range(4)])
        items, cursor, _ = page_entities(ledger, _req(limit=4), None)
        assert len(items) == 4 and cursor is None

    def test_scan_limit_short_pages(self, monkeypatch):
        monkeypatch.setattr(mod, "MAX_SCAN", 4)
        rows = [_row("k", f"{i:02d}", hit=i >= 9) for i in range(12)]
        ledger = _Ledger(rows)
        items, cursor, scanned = page_entities(
            ledger, _req(limit=2, where=[{"attr": "hit", "op": "eq", "value": True}]), None
        )
        # Редкий фильтр: страница пуста, но перечень не кончился.
        assert (items, scanned) == ([], 4) and cursor is not None
        where = [{"attr": "hit", "op": "eq", "value": True}]
        assert [k for k, _ in _walk(ledger, limit=2, where=where)] == ["09", "10", "11"]

    def test_unknown_kind_empty(self):
        ledger = _Ledger([_row("k", "a")])
        items, cursor, _ = page_entities(ledger, _req(kinds=["nope"]), None)
        assert (items, cursor) == ([], None)
