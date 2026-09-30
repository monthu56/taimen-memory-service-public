"""Перечень сущностей вида с фильтрами на реальной БД (MEM-ADR-020, амендмент
«перечень сущностей»; K030).

Сущности приходят сверкой снимков: перечень читает версии журнала на ``as_of``.
Проверяются ``validUntil lte``, ``prefix``, курсор без пропусков и повторов,
видимость scopes, неизвестный вид и HTTP-маршрут. Запуск — см. conftest.
"""

from __future__ import annotations

import pytest

from platform_memory.context import entities as entities_mod
from platform_memory.context.entities import query_entities
from platform_memory.domain.reconcile import reconcile

pytestmark = pytest.mark.integration

NS = "tenant:t1:ws:kb"
NS2 = "tenant:t1:ws:other"
T1 = "2026-09-01T00:00:00Z"
T2 = "2026-09-20T00:00:00Z"
BETWEEN = "2026-09-10T00:00:00Z"

LICENSES = {
    "lic:fstek": "2026-06-30",
    "lic:fsb": "2027-03-01T00:00:00Z",
    "lic:mchs": "2026-12-31",
}
OFFERS = {
    "offer:dev": "62.01",
    "offer:custom": "62.01.11",
    "offer:other": "62.011",
    "offer:pc": "26.20.11",
}


def _snapshot(sid: str, observed_at: str, licenses: dict[str, str], **extra) -> dict:
    return {
        "source": "erp:main",
        "scope": "",
        "snapshotId": sid,
        "observedAt": observed_at,
        "entities": [
            *(
                {"kind": "license", "key": k, "title": k.upper(), "attributes": {"validUntil": v}}
                for k, v in licenses.items()
            ),
            *({"kind": "offer", "key": k, "attributes": {"okpd2": v}} for k, v in OFFERS.items()),
            {"kind": "offer", "key": "offer:plain"},
        ],
        "relations": [],
        **extra,
    }


@pytest.fixture
def kb(stores, settings):
    conn = stores[0]
    reconcile(settings, snapshot=_snapshot("s1", T1, LICENSES), namespace=NS, conn=conn)
    return conn


def _query(settings, conn, **payload) -> dict:
    payload.setdefault("namespaces", [NS])
    return query_entities(settings, payload, conn=conn)


def _keys(page: dict) -> list[str]:
    return [item["key"] for item in page["items"]]


def test_valid_until_lte(settings, kb):
    page = _query(
        settings,
        kb,
        kinds=["license"],
        where=[{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}],
    )
    assert _keys(page) == ["lic:fstek", "lic:mchs"]
    assert page["nextCursor"] is None
    item = page["items"][0]
    assert item == {
        "kind": "license",
        "key": "lic:fstek",
        "namespace": NS,
        "title": "LIC:FSTEK",
        "attributes": {"validUntil": "2026-06-30"},
        "source": "erp:main",
        "scope": "",
        "snapshot_id": "s1",
        "source_path": "snapshot:erp:main/s1",
        "valid_from": T1,
        "valid_to": None,
        # MEM-ADR-022: источники сведения (здесь — один).
        "sources": [
            {
                "source": "erp:main",
                "scope": "",
                "snapshot_id": "s1",
                "source_path": "snapshot:erp:main/s1",
            }
        ],
    }


def test_prefix_by_segments(settings, kb):
    page = _query(
        settings, kb, kinds=["offer"], where=[{"attr": "okpd2", "op": "prefix", "value": "62.01"}]
    )
    assert _keys(page) == ["offer:custom", "offer:dev"]


def test_order_by_kind_then_key(settings, kb):
    page = _query(settings, kb, kinds=["offer", "license"])
    assert [(i["kind"], i["key"]) for i in page["items"]] == sorted(
        [("license", k) for k in LICENSES] + [("offer", k) for k in [*OFFERS, "offer:plain"]]
    )


def test_as_of_reads_version_of_that_moment(settings, kb):
    changed = {**LICENSES, "lic:fstek": "2028-01-01"}
    del changed["lic:mchs"]
    reconcile(settings, snapshot=_snapshot("s2", T2, changed), namespace=NS, conn=kb)
    where = [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}]

    now = _query(settings, kb, kinds=["license"], where=where)
    assert _keys(now) == []
    before = _query(settings, kb, kinds=["license"], where=where, asOf=BETWEEN)
    assert _keys(before) == ["lic:fstek", "lic:mchs"]
    assert before["as_of"] == BETWEEN
    assert _keys(_query(settings, kb, kinds=["license"], asOf="2026-08-01T00:00:00Z")) == []


def test_cursor_no_gaps_no_repeats(settings, kb, monkeypatch):
    many = {f"lic:{i:03d}": "2026-01-01" for i in range(23)}
    reconcile(
        settings,
        snapshot={**_snapshot("n2", T1, many), "source": "erp:second"},
        namespace=NS2,
        conn=kb,
    )
    reconcile(settings, snapshot=_snapshot("s2", T2, {**LICENSES, **many}), namespace=NS, conn=kb)
    expected = sorted([(k, NS) for k in {**LICENSES, **many}] + [(k, NS2) for k in many])

    def walk(limit: int, **extra) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        cursor = None
        for _ in range(200):
            page = _query(
                settings,
                kb,
                kinds=["license"],
                namespaces=[NS, NS2],
                limit=limit,
                cursor=cursor,
                **extra,
            )
            assert len(page["items"]) <= limit
            seen += [(i["key"], i["namespace"]) for i in page["items"]]
            cursor = page["nextCursor"]
            if cursor is None:
                return seen
        raise AssertionError("курсор не дошёл до конца")

    for limit in (1, 7, 49, 500):
        assert walk(limit) == expected, limit
    # Лимит просмотра меньше страницы: короткие страницы, но без пропусков и повторов.
    monkeypatch.setattr(entities_mod, "MAX_SCAN", 5)
    assert walk(3, where=[{"attr": "validUntil", "op": "lte", "value": "2026-01-01"}]) == [
        x for x in expected if x[0].startswith("lic:0")
    ]


def test_scopes_visibility(settings, kb):
    reconcile(
        settings,
        snapshot={
            **_snapshot("p1", T1, {"lic:secret": "2026-01-01"}),
            "source": "erp:private",
            "entities": [
                {"kind": "license", "key": "lic:secret", "attributes": {"validUntil": "2026-01-01"}}
            ],
        },
        namespace=NS,
        scopes=["workspace:w1"],
        conn=kb,
    )
    everyone = _query(settings, kb, kinds=["license"])
    assert "lic:secret" in _keys(everyone)
    outsider = _query(settings, kb, kinds=["license"], allowed_scopes=["workspace:w2"])
    assert _keys(outsider) == sorted(LICENSES)
    member = _query(settings, kb, kinds=["license"], allowed_scopes=["workspace:w1"])
    assert _keys(member) == sorted([*LICENSES, "lic:secret"])


def test_unknown_kind_is_empty(settings, kb):
    page = _query(settings, kb, kinds=["nosuchkind"])
    assert page["items"] == [] and page["nextCursor"] is None


def test_no_ledger_is_empty(settings, stores):
    page = _query(settings, stores[0], kinds=["license"])
    assert page["items"] == [] and page["nextCursor"] is None


def test_http_entities_query(test_settings, clean_db, monkeypatch):
    from fastapi.testclient import TestClient

    import platform_memory.server.app as mod

    settings = test_settings.model_copy_with(
        server_api_key="", server_api_keys_pii="", api_keys="", iam_enabled=False
    )
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    client = TestClient(mod.app)
    resp = client.post(
        "/api/memory/reconcile", json={**_snapshot("s1", T1, LICENSES), "namespace": NS}
    )
    assert resp.status_code == 200, resp.text

    body = {
        "namespaces": [NS],
        "kinds": ["license"],
        "where": [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}],
        "limit": 1,
    }
    first = client.post("/api/memory/entities:query", json=body)
    assert first.status_code == 200, first.text
    assert _keys(first.json()) == ["lic:fstek"]
    cursor = first.json()["nextCursor"]
    second = client.post(
        "/api/memory/entities:query",
        json={**body, "namespaces": None, "scope": {"namespace": NS}, "cursor": cursor},
    )
    assert second.status_code == 200, second.text
    assert _keys(second.json()) == ["lic:mchs"] and second.json()["nextCursor"] is None

    bad = client.post("/api/memory/entities:query", json={**body, "cursor": "garbage"})
    assert bad.status_code == 400
    bad_where = client.post(
        "/api/memory/entities:query",
        json={**body, "where": [{"attr": "validUntil", "op": "lte"}]},
    )
    assert bad_where.status_code == 400
