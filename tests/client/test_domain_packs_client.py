"""Unit-тесты клиента: доменные пакеты, reconcile, typed context (MEM-ADR-020) —
wire-контракт без сети (MockTransport)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from platform_memory_client import (
    AsyncMemoryClient,
    EntitiesPage,
    MemoryClient,
    MemoryServiceError,
    MemorySnapshotStaleError,
    NamespaceKinds,
    PackageResult,
    ReconcileChanges,
    ReconcileConflicts,
    ReconcileResult,
    TypedContextPack,
)

pytestmark = pytest.mark.unit


def _handler(captured: list[httpx.Request], payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    return handler


def _sync(captured: list[httpx.Request], payload: dict) -> MemoryClient:
    http = httpx.Client(
        base_url="http://memory:8077", transport=httpx.MockTransport(_handler(captured, payload))
    )
    return MemoryClient("http://memory:8077", client=http)


def test_packages_contract() -> None:
    captured: list[httpx.Request] = []
    client = _sync(captured, {"status": "created", "pack": {"name": "tracker"}, "packages": []})
    out = client.register_package({"name": "tracker", "version": "1", "kinds": []})
    assert isinstance(out, PackageResult) and out.status == "created"
    assert captured[0].method == "POST" and captured[0].url.path == "/api/memory/packages"
    assert json.loads(captured[0].content)["name"] == "tracker"

    assert client.packages() == []
    client.package("tracker", version="1")
    assert captured[2].url.path == "/api/memory/packages/tracker"
    assert dict(captured[2].url.params) == {"version": "1"}


def test_tenant_packages_contract() -> None:
    """K007: пакет арендатора — scope/namespace в теле, namespace в чтении."""
    captured: list[httpx.Request] = []
    client = _sync(captured, {"status": "created", "pack": {"ref": "tenant:fleet@1"}})
    pack = {"name": "fleet", "version": "1", "kinds": [{"kind": "vehicle"}]}
    client.register_package(pack, tenant_namespace="tenant:t1")
    assert json.loads(captured[0].content) == {
        **pack,
        "scope": "tenant",
        "namespace": "tenant:t1",
    }
    assert "scope" not in pack  # вход не мутируется
    client.packages(namespace="tenant:t1")
    assert dict(captured[1].url.params) == {"namespace": "tenant:t1"}
    client.package("tenant:fleet", namespace="tenant:t1")
    assert captured[2].url.path == "/api/memory/packages/tenant:fleet"
    assert dict(captured[2].url.params) == {"namespace": "tenant:t1"}

    async def run() -> None:
        acaptured: list[httpx.Request] = []
        http = httpx.AsyncClient(
            base_url="http://memory:8077",
            transport=httpx.MockTransport(_handler(acaptured, {"packages": []})),
        )
        aclient = AsyncMemoryClient("http://memory:8077", client=http)
        await aclient.register_package(pack, tenant_namespace="tenant:t1")
        assert json.loads(acaptured[0].content)["scope"] == "tenant"
        assert await aclient.packages(namespace="tenant:t1") == []
        await aclient.package("tenant:fleet", version="1", namespace="tenant:t1")
        assert dict(acaptured[2].url.params) == {"version": "1", "namespace": "tenant:t1"}
        await http.aclose()

    asyncio.run(run())


def test_namespace_kinds_contract() -> None:
    captured: list[httpx.Request] = []
    reindex = {"indexed": 2, "updated": 0, "removed": 0, "unchanged": 0}
    client = _sync(
        captured,
        {"settings": {"strict": True}, "catalog": {}, "reindex": reindex, "merge": {"nodes": 3}},
    )
    out = client.set_namespace_kinds("tenant:t1", strict=True, packages=["tracker@1"])
    assert isinstance(out, NamespaceKinds) and out.settings["strict"] is True
    assert out.reindex == reindex
    assert out.merge == {"nodes": 3}
    assert captured[0].method == "PUT"
    assert captured[0].url.path == "/api/memory/namespaces/tenant:t1/kinds"
    assert json.loads(captured[0].content) == {"strict": True, "packages": ["tracker@1"]}
    client.namespace_kinds("tenant:t1")
    assert captured[1].method == "GET"


def test_reconcile_and_typed_context_contract() -> None:
    captured: list[httpx.Request] = []
    client = _sync(
        captured,
        {
            "opened": 2,
            "closed": 1,
            "unchanged": 3,
            "relations": {"opened": 1, "pending": 1, "resolved": 0},
            "changes": {
                "opened": [{"kind": "issue", "key": "PROJ-2"}],
                "changed": [],
                "closed": [{"kind": "issue", "key": "PROJ-1"}],
                "limit": 1000,
                "truncated": False,
            },
            "sections": [],
        },
    )
    snapshot = {
        "pack": "software-delivery@1",
        "source": "git:platform-web",
        "scope": "platform-web",
        "snapshotId": "platform-web@1",
        "observedAt": "2026-09-23T08:47:12+00:00",
        "entities": [],
        "relations": [],
    }
    res = client.reconcile(snapshot, namespace="tenant:t1", scopes=["workspace:w1"])
    assert isinstance(res, ReconcileResult) and (res.opened, res.closed) == (2, 1)
    assert res.relations["pending"] == 1
    assert isinstance(res.changes, ReconcileChanges)
    assert res.changes.closed == [{"kind": "issue", "key": "PROJ-1"}]
    assert (res.changes.changed, res.changes.truncated) == ([], False)
    # Документ снимка уходит как есть: namespace и scopes — отдельными полями.
    assert json.loads(captured[0].content) == {
        **snapshot,
        "namespace": "tenant:t1",
        "scopes": ["workspace:w1"],
    }
    pack = client.typed_context(
        {"anchors": [{"value": "PROJ-1"}], "traverse": [{"relation": "blocks"}]},
        namespaces=["tenant:t1"],
        run_id="r1",
    )
    assert isinstance(pack, TypedContextPack)
    assert captured[1].url.path == "/api/memory/context/typed"
    assert json.loads(captured[1].content)["scope"] == {"namespaces": ["tenant:t1"]}
    assert captured[1].headers["X-Run-Id"] == "r1"


def test_typed_context_where_sent_as_is() -> None:
    """K006: where верхнего уровня и шага уходят как есть; filtered якоря доходит."""
    captured: list[httpx.Request] = []
    client = _sync(
        captured,
        {
            "anchors": [{"input": {"value": "ACME"}, "resolved": [], "filtered": 2}],
            "stats": {"steps": [{"relation": "holds", "reached": 1, "filtered": 1}]},
        },
    )
    request = {
        "anchors": [{"value": "ACME"}],
        "where": [{"attr": "okpd2", "op": "prefix", "value": "62.01"}],
        "traverse": [
            {
                "relation": "holds",
                "where": [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}],
            }
        ],
    }
    pack = client.typed_context(request, namespaces=["tenant:t1"])
    body = json.loads(captured[0].content)
    assert body["where"] == request["where"] and body["traverse"] == request["traverse"]
    assert pack.anchors[0]["filtered"] == 2


def test_async_mirror() -> None:
    captured: list[httpx.Request] = []
    http = httpx.AsyncClient(
        base_url="http://memory:8077",
        transport=httpx.MockTransport(_handler(captured, {"opened": 1, "sections": []})),
    )
    client = AsyncMemoryClient("http://memory:8077", client=http)

    async def run():
        res = await client.reconcile({"source": "src", "snapshotId": "s1"})
        pack = await client.typed_context({"anchors": [{"value": "x"}]}, namespaces=["n"])
        await client.set_namespace_kinds("n", strict=False)
        return res, pack

    res, pack = asyncio.run(run())
    assert res.opened == 1 and isinstance(pack, TypedContextPack)
    assert res.changes.opened == [] and res.changes.truncated is False  # старый ответ
    # Ответ до K004: без dryRun, stateToken и conflicts.
    assert (res.dry_run, res.state_token, res.conflicts.items) == (False, None, [])
    assert [r.url.path for r in captured] == [
        "/api/memory/reconcile",
        "/api/memory/context/typed",
        "/api/memory/namespaces/n/kinds",
    ]


def test_reconcile_plan_and_apply_by_state() -> None:
    """Амендмент MEM-ADR-020 2026-09-28 (K004): dryRun/expectedState уходят только
    заданными, stateToken и conflicts читаются, 409 snapshot_stale — своё исключение."""
    captured: list[httpx.Request] = []
    conflict = {"kind": "sku", "key": "A-1", "source": "erp", "scope": ""}
    client = _sync(
        captured,
        {
            "opened": 1,
            "dryRun": True,
            "stateToken": "st1-abc",
            "conflicts": {"items": [conflict], "limit": 1000, "truncated": False},
        },
    )
    snapshot = {"source": "sheet", "snapshotId": "s1", "observedAt": "2026-09-28T00:00:00Z"}
    plan = client.reconcile(snapshot, namespace="tenant:t1", dry_run=True)
    assert plan.dry_run is True and plan.state_token == "st1-abc"
    assert isinstance(plan.conflicts, ReconcileConflicts)
    assert plan.conflicts.items == [conflict]
    assert json.loads(captured[0].content) == {
        **snapshot,
        "namespace": "tenant:t1",
        "dryRun": True,
    }
    client.reconcile(snapshot, expected_state=plan.state_token)
    assert json.loads(captured[1].content) == {**snapshot, "expectedState": "st1-abc"}
    client.reconcile(snapshot)
    assert json.loads(captured[2].content) == snapshot  # прежнее тело без новых полей

    detail = {
        "code": "snapshot_stale",
        "message": "состояние изменилось",
        "expectedState": "st1-abc",
        "stateToken": "st1-def",
    }
    http = httpx.Client(
        base_url="http://memory:8077",
        transport=httpx.MockTransport(lambda r: httpx.Response(409, json={"detail": detail})),
    )
    stale_client = MemoryClient("http://memory:8077", client=http)
    with pytest.raises(MemorySnapshotStaleError) as exc:
        stale_client.reconcile(snapshot, expected_state="st1-abc")
    assert isinstance(exc.value, MemoryServiceError) and exc.value.status_code == 409
    assert (exc.value.expected_state, exc.value.state_token) == ("st1-abc", "st1-def")

    # Прежний 409 «снимок старше принятого» (detail — строка) — общий MemoryServiceError.
    http = httpx.Client(
        base_url="http://memory:8077",
        transport=httpx.MockTransport(lambda r: httpx.Response(409, json={"detail": "старше"})),
    )
    with pytest.raises(MemoryServiceError) as exc:
        MemoryClient("http://memory:8077", client=http).reconcile(snapshot)
    assert not isinstance(exc.value, MemorySnapshotStaleError)
    assert exc.value.detail == "старше"


def test_async_reconcile_plan_fields() -> None:
    captured: list[httpx.Request] = []
    http = httpx.AsyncClient(
        base_url="http://memory:8077",
        transport=httpx.MockTransport(_handler(captured, {"dryRun": True, "stateToken": "t"})),
    )
    client = AsyncMemoryClient("http://memory:8077", client=http)
    res = asyncio.run(client.reconcile({"source": "s"}, dry_run=True, expected_state="t"))
    assert res.dry_run is True and res.state_token == "t"
    assert json.loads(captured[0].content) == {
        "source": "s",
        "dryRun": True,
        "expectedState": "t",
    }


def test_query_entities_contract() -> None:
    """K030: перечень сущностей — тело запроса, scope и страница с курсором."""
    captured: list[httpx.Request] = []
    client = _sync(
        captured,
        {
            "items": [
                {
                    "kind": "license",
                    "key": "lic:1",
                    "namespace": "tenant:t1",
                    "title": "Лицензия",
                    "attributes": {"validUntil": "2026-12-31"},
                    "source": "erp:main",
                    "scope": "",
                    "source_path": "snapshot:erp:main/s1",
                    "sources": [
                        {
                            "source": "erp:main",
                            "scope": "",
                            "snapshot_id": "s1",
                            "source_path": "snapshot:erp:main/s1",
                        },
                        {
                            "source": "crm:main",
                            "scope": "",
                            "snapshot_id": "c1",
                            "source_path": "snapshot:crm:main/c1",
                        },
                    ],
                }
            ],
            "nextCursor": "c2",
        },
    )
    where = [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}]
    page = client.query_entities(
        ["license"],
        namespaces=["tenant:t1"],
        where=where,
        as_of="2026-09-01T00:00:00Z",
        limit=50,
        cursor="c1",
        allowed_scopes=["workspace:w1"],
    )
    assert isinstance(page, EntitiesPage) and page.next_cursor == "c2"
    assert page.items[0].key == "lic:1" and page.items[0].attributes["validUntil"]
    # MEM-ADR-022: сущность сведена из версий нескольких источников.
    assert [s.source for s in page.items[0].sources] == ["erp:main", "crm:main"]
    assert captured[0].url.path == "/api/memory/entities:query"
    assert json.loads(captured[0].content) == {
        "kinds": ["license"],
        "where": where,
        "asOf": "2026-09-01T00:00:00Z",
        "limit": 50,
        "cursor": "c1",
        "allowedScopes": ["workspace:w1"],
        "scope": {"namespaces": ["tenant:t1"]},
    }
    client.query_entities(["license"])
    assert json.loads(captured[1].content) == {"kinds": ["license"]}


def test_query_entities_async() -> None:
    captured: list[httpx.Request] = []
    http = httpx.AsyncClient(
        base_url="http://memory:8077",
        transport=httpx.MockTransport(_handler(captured, {"items": [], "nextCursor": None})),
    )
    client = AsyncMemoryClient("http://memory:8077", client=http)
    page = asyncio.run(client.query_entities(["license"], namespaces=["tenant:t1"], cursor="c"))
    assert isinstance(page, EntitiesPage) and page.items == [] and page.next_cursor is None
    assert json.loads(captured[0].content) == {
        "kinds": ["license"],
        "cursor": "c",
        "scope": {"namespaces": ["tenant:t1"]},
    }
