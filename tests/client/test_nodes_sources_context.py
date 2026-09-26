"""Unit-тесты клиента: nodes/sources/delete_node, /api/memory, credential provider,
ошибки транспорта — wire-контракт без сети (MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from platform_memory_client import (
    AsyncMemoryClient,
    ContextPack,
    MemoryClient,
    MemoryServiceError,
    MemoryTransportError,
    NodesResult,
    SourceResult,
)

pytestmark = pytest.mark.unit


def _sync(captured: list[httpx.Request], response: httpx.Response, **kw) -> MemoryClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    http = httpx.Client(base_url="http://memory:8077", transport=httpx.MockTransport(handler))
    return MemoryClient("http://memory:8077", client=http, **kw)


def _async(captured: list[httpx.Request], response: httpx.Response, **kw) -> AsyncMemoryClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    http = httpx.AsyncClient(base_url="http://memory:8077", transport=httpx.MockTransport(handler))
    return AsyncMemoryClient("http://memory:8077", client=http, **kw)


def test_nodes_query_params_and_namespace() -> None:
    captured: list[httpx.Request] = []
    out = _sync(
        captured, httpx.Response(200, json={"count": 1, "nodes": [{"natural_key": "a"}]})
    ).nodes(type="article", limit=200, namespace="demo")
    assert isinstance(out, NodesResult)
    assert out.nodes[0]["natural_key"] == "a"
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/brain/nodes"
    assert dict(req.url.params) == {"namespace": "demo", "type": "article", "limit": "200"}


def test_source_keeps_slashes_in_key_and_run_header() -> None:
    captured: list[httpx.Request] = []
    out = _sync(
        captured, httpx.Response(200, json={"natural_key": "kb://x/y", "content": "text"})
    ).source("kb://demo/articles/x", namespace="demo", run_id="r1")
    assert isinstance(out, SourceResult)
    assert out.content == "text"
    assert captured[0].url.path == "/api/brain/sources/kb://demo/articles/x"
    assert captured[0].headers["X-Run-Id"] == "r1"


def test_delete_node_sends_actor_and_namespace() -> None:
    captured: list[httpx.Request] = []
    out = _sync(captured, httpx.Response(200, json={"deleted": True})).delete_node(
        "kb://demo/articles/x", namespace="demo", actor="kb-editor"
    )
    assert out.deleted is True
    assert captured[0].method == "DELETE"
    assert dict(captured[0].url.params) == {"namespace": "demo", "actor": "kb-editor"}


def test_default_namespace_is_not_sent() -> None:
    captured: list[httpx.Request] = []
    _sync(captured, httpx.Response(200, json={"nodes": []})).nodes()
    assert dict(captured[0].url.params) == {"limit": "50"}


def test_context_and_observations_contract() -> None:
    captured: list[httpx.Request] = []
    client = _sync(captured, httpx.Response(200, json={"sections": [], "results": []}))
    pack = client.context({"task": "t", "budget": "mid"}, namespaces=["ns"], run_id="r2")
    assert isinstance(pack, ContextPack)
    assert json.loads(captured[0].content) == {
        "task": "t",
        "budget": "mid",
        "scope": {"namespaces": ["ns"]},
    }
    assert captured[0].headers["X-Run-Id"] == "r2"
    client.observe_batch([{"kind": "note"}], namespace="ns")
    assert captured[1].url.path == "/api/memory/observations:batch"
    assert json.loads(captured[1].content) == {
        "observations": [{"kind": "note"}],
        "scope": {"namespace": "ns"},
    }


def test_callable_token_is_read_per_request() -> None:
    captured: list[httpx.Request] = []
    tokens = iter(["first", "second"])
    client = _sync(captured, httpx.Response(200, json={"nodes": []}), token=lambda: next(tokens))
    client.nodes()
    client.nodes()
    assert [r.headers["Authorization"] for r in captured] == ["Bearer first", "Bearer second"]


def test_service_error_flags() -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(MemoryServiceError) as exc:
        _sync(captured, httpx.Response(404, json={"detail": "нет узла"})).source("x")
    assert exc.value.not_found and not exc.value.unavailable
    with pytest.raises(MemoryServiceError) as exc:
        _sync(captured, httpx.Response(503, json={"detail": "db"})).nodes()
    assert exc.value.unavailable


def test_transport_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(base_url="http://memory:8077", transport=httpx.MockTransport(handler))
    with pytest.raises(MemoryTransportError) as exc:
        MemoryClient("http://memory:8077", client=http).healthz()
    assert exc.value.status_code == 0 and exc.value.unavailable


class _Provider:
    """Контракт CredentialProvider control-plane-client: async token()."""

    refreshable = True

    async def token(self) -> str:
        return "iam-access-token"


@pytest.mark.anyio
async def test_async_client_accepts_credential_provider_and_async_callable() -> None:
    captured: list[httpx.Request] = []
    async with _async(captured, httpx.Response(200, json={"nodes": []}), token=_Provider()) as c:
        await c.nodes(namespace="tenant:x")
    assert captured[0].headers["Authorization"] == "Bearer iam-access-token"

    async def issue() -> str:
        return "from-coroutine"

    captured.clear()
    async with _async(captured, httpx.Response(200, json={"content": ""}), token=issue) as c:
        await c.source("k")
    assert captured[0].headers["Authorization"] == "Bearer from-coroutine"
