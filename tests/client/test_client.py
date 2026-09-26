"""Unit tests for platform_memory_client — the thin HTTP SDK.

No network: an ``httpx.MockTransport`` captures the outgoing request (path, body,
headers) and returns canned service responses, so we assert both the wire contract
(what the client sends) and typed parsing (what it returns).
"""

from __future__ import annotations

import json

import httpx
import pytest

from platform_memory_client import (
    AsyncMemoryClient,
    MemoryClient,
    MemoryServiceError,
    RecallResult,
)

pytestmark = pytest.mark.unit


def _recording_transport(captured: list[httpx.Request], response: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return httpx.MockTransport(handler)


def _make_client(captured: list[httpx.Request], response: httpx.Response, **kw) -> MemoryClient:
    http = httpx.Client(
        base_url="http://memory:8077",
        transport=_recording_transport(captured, response),
        headers={"Authorization": f"Bearer {kw['api_key']}"} if kw.get("api_key") else {},
    )
    return MemoryClient("http://memory:8077", client=http)


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_recall_sends_contract_and_parses_response() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(
        200,
        json={
            "query": "who owns billing?",
            "budget": "high",
            "count": 1,
            "memories": [
                {"node_key": "n1", "title": "Billing", "source_path": "b.md", "text": "x"}
            ],
            "sources": [{"path": "b.md"}],
            "neighbors": [],
            "context": "ctx",
        },
    )
    client = _make_client(captured, resp)
    result = client.recall("who owns billing?", budget="high", hops=2)

    assert isinstance(result, RecallResult)
    assert result.count == 1
    assert result.memories[0].title == "Billing"
    assert str(captured[0].url).endswith("/recall")
    assert _body(captured[0]) == {"query": "who owns billing?", "budget": "high", "hops": 2}


def test_none_optionals_are_dropped_from_body() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(200, json={"query": "q", "count": 0, "memories": []})
    client = _make_client(captured, resp)
    client.recall("q")  # scope=None must not appear in the payload
    assert "scope" not in _body(captured[0])
    assert _body(captured[0]) == {"query": "q", "budget": "mid", "hops": 1}


def test_retain_uses_run_id_header_and_bare_alias() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(201, json={"retained": True, "type": "decision", "title": "T"})
    client = _make_client(captured, resp)
    out = client.retain("Billing owned by Payments.", type="decision", run_id="run-42")

    assert out.retained is True
    assert out.type == "decision"
    req = captured[0]
    assert str(req.url).endswith("/retain")
    assert req.headers["X-Run-Id"] == "run-42"
    assert _body(req)["content"] == "Billing owned by Payments."


def test_audit_contract() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(201, json={"recorded": True, "id": "e1"})
    client = _make_client(captured, resp)
    out = client.audit("trace-1", "checkout", actor="agent-x", payload={"k": 1})

    assert out.recorded is True
    body = _body(captured[0])
    assert body["trace_id"] == "trace-1"
    assert body["action"] == "checkout"
    assert body["payload"] == {"k": 1}


def test_query_hits_canonical_path() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(200, json={"question": "q?", "synthesized": True, "answer": "A"})
    client = _make_client(captured, resp)
    out = client.query("q?", k=5, synthesize=True)

    assert out.answer == "A"
    assert str(captured[0].url).endswith("/api/brain/query")
    assert _body(captured[0]) == {"question": "q?", "k": 5, "hops": 1, "synthesize": True}


def test_search_builds_filters() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(200, json={"query": "q", "mode": "structural", "count": 0, "results": []})
    client = _make_client(captured, resp)
    client.search("q", type="decision", status="Proposed", limit=5)
    assert _body(captured[0]) == {
        "query": "q",
        "filters": {"type": "decision", "status": "Proposed", "limit": 5},
    }


def test_auth_header_attached() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(200, json={"query": "q", "count": 0, "memories": []})
    client = _make_client(captured, resp, api_key="secret")
    client.recall("q")
    assert captured[0].headers["Authorization"] == "Bearer secret"


def test_non_2xx_raises_service_error_with_detail() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(401, json={"detail": "Требуется корректный Authorization"})
    client = _make_client(captured, resp)
    with pytest.raises(MemoryServiceError) as exc:
        client.recall("q")
    assert exc.value.status_code == 401
    assert "Authorization" in exc.value.detail


def test_forward_compatible_extra_fields_preserved() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(200, json={"query": "q", "count": 0, "memories": [], "new_field": 7})
    client = _make_client(captured, resp)
    result = client.recall("q")
    # Unknown fields the service adds later must not break parsing.
    assert result.model_dump().get("new_field") == 7


@pytest.mark.anyio
async def test_async_client_mirrors_sync() -> None:
    captured: list[httpx.Request] = []
    resp = httpx.Response(200, json={"query": "q", "count": 1, "memories": [{"title": "M"}]})
    http = httpx.AsyncClient(
        base_url="http://memory:8077",
        transport=_recording_transport(captured, resp),
    )
    async with AsyncMemoryClient("http://memory:8077", client=http) as client:
        result = await client.recall("q", budget="low")
    assert result.memories[0].title == "M"
    assert _body(captured[0]) == {"query": "q", "budget": "low", "hops": 1}
