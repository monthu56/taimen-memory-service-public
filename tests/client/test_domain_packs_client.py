"""Unit-тесты клиента: доменные пакеты, reconcile, typed context (MEM-ADR-020) —
wire-контракт без сети (MockTransport)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from platform_memory_client import (
    AsyncMemoryClient,
    MemoryClient,
    NamespaceKinds,
    PackageResult,
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


def test_namespace_kinds_contract() -> None:
    captured: list[httpx.Request] = []
    client = _sync(captured, {"settings": {"strict": True}, "catalog": {}})
    out = client.set_namespace_kinds("tenant:t1", strict=True, packages=["tracker@1"])
    assert isinstance(out, NamespaceKinds) and out.settings["strict"] is True
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
    assert [r.url.path for r in captured] == [
        "/api/memory/reconcile",
        "/api/memory/context/typed",
        "/api/memory/namespaces/n/kinds",
    ]
