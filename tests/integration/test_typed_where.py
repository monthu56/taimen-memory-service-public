"""Фильтры ``where`` типизированного обхода на реальной БД (MEM-ADR-020, амендмент
2026-09-28, п.3; K006).

Сущности приходят сверкой снимка: атрибуты берутся из версии журнала на ``as_of``.
Проверяются иерархия ОКПД2 (``prefix`` по сегментам), ``validUntil lte <дата>``,
фильтр якорей и HTTP-маршрут. Запуск — см. conftest (CB_TEST_DATABASE_URL).
"""

from __future__ import annotations

import pytest

from platform_memory.context.typed import compile_typed_context
from platform_memory.domain.reconcile import reconcile
from platform_memory.domain.registry import open_registry

pytestmark = pytest.mark.integration

NS = "tenant:t1:ws:kb"
T1 = "2026-09-01T00:00:00Z"
T2 = "2026-09-20T00:00:00Z"
BETWEEN = "2026-09-10T00:00:00Z"

PACK = {
    "name": "company-kb",
    "version": "1",
    "kinds": [
        {"kind": "company", "kindAliases": ["organization"]},
        {"kind": "offer"},
        {"kind": "license"},
    ],
    "relations": [
        {"relation": "offers", "fromKinds": ["company"], "toKinds": ["offer"]},
        {"relation": "holds", "fromKinds": ["company"], "toKinds": ["license"]},
    ],
}

OFFERS = {
    "offer:dev": "62.01",
    "offer:custom": "62.01.11",
    "offer:custom-deep": "62.01.11.000",
    "offer:other": "62.011",
    "offer:pc": "26.20.11",
}
LICENSES = {
    "lic:fstek": "2026-06-30",
    "lic:fsb": "2027-03-01T00:00:00Z",
    "lic:mchs": "2026-12-31",
}


def _entities(valid_until: dict[str, str]) -> list[dict]:
    return [
        {"kind": "company", "key": "acme", "title": "ACME"},
        *(
            {"kind": "offer", "key": k, "aliases": ["разработка"], "attributes": {"okpd2": v}}
            for k, v in OFFERS.items()
        ),
        {"kind": "offer", "key": "offer:plain", "aliases": ["разработка"]},
        *(
            {"kind": "license", "key": k, "attributes": {"validUntil": v}}
            for k, v in valid_until.items()
        ),
    ]


def _relations() -> list[dict]:
    def rel(name: str, kind: str, key: str) -> dict:
        return {
            "relation": name,
            "from": {"kind": "company", "key": "acme"},
            "to": {"kind": kind, "key": key},
        }

    return [
        *(rel("offers", "offer", k) for k in [*OFFERS, "offer:plain"]),
        *(rel("holds", "license", k) for k in LICENSES),
    ]


def _snapshot(sid: str, observed_at: str, valid_until: dict[str, str]) -> dict:
    return {
        "source": "erp:main",
        "scope": "",
        "snapshotId": sid,
        "observedAt": observed_at,
        "entities": _entities(valid_until),
        "relations": _relations(),
    }


@pytest.fixture
def kb(stores, settings):
    conn = stores[0]
    reg = open_registry(conn, settings)
    reg.ensure_schema()
    reg.register(PACK)
    reg.put_settings(NS, strict=False, packages=["company-kb"])
    reconcile(settings, snapshot=_snapshot("s1", T1, LICENSES), namespace=NS, conn=conn)
    return conn


def _typed(settings, conn, **payload) -> dict:
    payload.setdefault("namespaces", [NS])
    return compile_typed_context(settings, payload, conn=conn)


def _reached(pack: dict) -> list[str]:
    return sorted(
        item["natural_key"]
        for section in pack["sections"]
        for item in section["items"]
        if not item["anchor"]
    )


def _step(relation: str, *where: dict) -> dict:
    return {"relation": relation, "limit": 50, "where": list(where)}


def test_okpd2_prefix_by_segments(settings, kb):
    pack = _typed(
        settings,
        kb,
        anchors=[{"kind": "company", "value": "acme"}],
        traverse=[_step("offers", {"attr": "okpd2", "op": "prefix", "value": "62.01"})],
    )
    assert _reached(pack) == ["offer:custom", "offer:custom-deep", "offer:dev"]
    assert sorted(f["object"] for f in pack["facts"]) == _reached(pack)
    (step,) = pack["stats"]["steps"]
    assert (step["reached"], step["filtered"]) == (3, 3)  # other, pc, plain (без атрибута)

    deeper = _typed(
        settings,
        kb,
        anchors=[{"kind": "company", "value": "acme"}],
        traverse=[_step("offers", {"attr": "okpd2", "op": "prefix", "value": "62.01.11"})],
    )
    assert _reached(deeper) == ["offer:custom", "offer:custom-deep"]


def test_valid_until_lte_date_on_as_of_version(settings, kb):
    expiring = {"attr": "validUntil", "op": "lte", "value": "2026-12-31"}
    pack = _typed(
        settings,
        kb,
        anchors=[{"value": "acme"}],
        traverse=[_step("holds", expiring)],
    )
    assert _reached(pack) == ["lic:fstek", "lic:mchs"]

    # s2: ФСТЭК продлена — сейчас не истекает, а на момент между снимками — истекала.
    reconcile(
        settings,
        snapshot=_snapshot("s2", T2, {**LICENSES, "lic:fstek": "2028-06-30"}),
        namespace=NS,
        conn=kb,
    )
    now = _typed(settings, kb, anchors=[{"value": "acme"}], traverse=[_step("holds", expiring)])
    assert _reached(now) == ["lic:mchs"]
    past = _typed(
        settings,
        kb,
        anchors=[{"value": "acme"}],
        traverse=[_step("holds", expiring)],
        as_of=BETWEEN,
    )
    assert _reached(past) == ["lic:fstek", "lic:mchs"]

    active = _typed(
        settings,
        kb,
        anchors=[{"value": "acme"}],
        traverse=[_step("holds", {"attr": "validUntil", "op": "gte", "value": "2027-01-01"})],
    )
    assert _reached(active) == ["lic:fsb", "lic:fstek"]


def test_eq_in_exists_on_step(settings, kb):
    def reach(*where: dict) -> list[str]:
        return _reached(
            _typed(settings, kb, anchors=[{"value": "acme"}], traverse=[_step("offers", *where)])
        )

    assert reach({"attr": "okpd2", "op": "eq", "value": "62.011"}) == ["offer:other"]
    assert reach({"attr": "okpd2", "op": "in", "value": ["26.20.11", "62.01"]}) == [
        "offer:dev",
        "offer:pc",
    ]
    assert reach({"attr": "okpd2", "op": "exists", "value": False}) == ["offer:plain"]
    assert len(reach({"attr": "okpd2", "op": "exists"})) == 5


def test_top_level_where_filters_anchor_candidates(settings, kb):
    """Псевдоним «разработка» подходит ко всем предложениям; where оставляет 62.01.*."""
    pack = _typed(
        settings,
        kb,
        anchors=[{"kind": "offer", "value": "разработка"}],
        where=[{"attr": "okpd2", "op": "prefix", "value": "62.01"}],
    )
    (anchor,) = pack["anchors"]
    assert sorted(r["natural_key"] for r in anchor["resolved"]) == [
        "offer:custom",
        "offer:custom-deep",
        "offer:dev",
    ]
    assert anchor["filtered"] == 3
    keys = {e["natural_key"] for e in pack["used"]["entities"]}
    assert keys == {"offer:custom", "offer:custom-deep", "offer:dev"}

    none = _typed(
        settings,
        kb,
        anchors=[{"kind": "offer", "value": "разработка"}],
        where=[{"attr": "okpd2", "op": "prefix", "value": "99"}],
    )
    assert none["unresolved"] == [{"kind": "offer", "value": "разработка"}]
    assert none["anchors"][0]["filtered"] == 6


def test_http_where(test_settings, clean_db, monkeypatch):
    from fastapi.testclient import TestClient

    import platform_memory.server.app as mod

    settings = test_settings.model_copy_with(
        server_api_key="", server_api_keys_pii="", api_keys="", iam_enabled=False
    )
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    client = TestClient(mod.app)
    assert client.post("/api/memory/packages", json=PACK).status_code == 201
    put = client.put(f"/api/memory/namespaces/{NS}/kinds", json={"packages": ["company-kb"]})
    assert put.status_code == 200
    resp = client.post(
        "/api/memory/reconcile", json={**_snapshot("s1", T1, LICENSES), "namespace": NS}
    )
    assert resp.status_code == 200

    body = {
        "anchors": [{"kind": "organization", "value": "acme"}],
        "where": [{"attr": "okpd2", "op": "exists", "value": False}],
        "traverse": [_step("holds", {"attr": "validUntil", "op": "lte", "value": "2026-12-31"})],
        "scope": {"namespaces": [NS]},
    }
    pack = client.post("/api/memory/context/typed", json=body)
    assert pack.status_code == 200, pack.text
    assert pack.json()["anchors"][0]["filtered"] == 0
    assert _reached(pack.json()) == ["lic:fstek", "lic:mchs"]
    trace = client.get(
        f"/api/memory/context/trace/{pack.json()['trace_id']}", params={"namespace": NS}
    ).json()
    assert "where" in str(trace)

    bad = client.post(
        "/api/memory/context/typed",
        json={**body, "traverse": [_step("holds", {"attr": "validUntil", "op": "lte"})]},
    )
    assert bad.status_code == 400
