"""Unit-тесты клиента: scope-параметры и document-API (wire-контракт, без сети)."""

from __future__ import annotations

import json

import httpx
import pytest

from platform_memory_client import (
    DocumentChunk,
    DocumentDeleteResult,
    DocumentIngestResult,
    MemoryClient,
)

pytestmark = pytest.mark.unit


def _client(captured: list[httpx.Request], payload: dict) -> MemoryClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    http = httpx.Client(base_url="http://memory:8077", transport=httpx.MockTransport(handler))
    return MemoryClient("http://memory:8077", client=http)


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_recall_namespaces_build_scope() -> None:
    captured: list[httpx.Request] = []
    client = _client(captured, {"query": "q", "count": 0, "memories": [], "sources": []})
    client.recall("q", namespaces=["tp:tenant:a", "shared"])
    assert _body(captured[0])["scope"] == {"namespaces": ["tp:tenant:a", "shared"]}


def test_retain_namespace_builds_write_scope() -> None:
    captured: list[httpx.Request] = []
    client = _client(captured, {"retained": True})
    client.retain("факт", namespace="tp:tenant:a:team:t")
    assert _body(captured[0])["scope"] == {"namespace": "tp:tenant:a:team:t"}


def test_retain_without_namespace_omits_scope() -> None:
    captured: list[httpx.Request] = []
    client = _client(captured, {"retained": True})
    client.retain("факт")
    assert "scope" not in _body(captured[0])


def test_search_meta_filter_and_scope() -> None:
    captured: list[httpx.Request] = []
    client = _client(captured, {"query": "q", "count": 0, "results": []})
    client.search("q", meta={"collection": "licenses"}, namespaces=["tp:tenant:a"])
    body = _body(captured[0])
    assert body["filters"]["meta"] == {"collection": "licenses"}
    assert body["scope"] == {"namespaces": ["tp:tenant:a"]}


def test_retain_document_sends_contract_and_parses() -> None:
    captured: list[httpx.Request] = []
    client = _client(
        captured,
        {"natural_key": "doc:1", "namespace": "tp:tenant:a", "chunks": 2, "replaced": True},
    )
    result = client.retain_document(
        "doc:1",
        "Устав",
        [DocumentChunk(text="глава 1", order=0), {"text": "глава 2", "order": 1}],
        namespace="tp:tenant:a",
        source_path="s3://bucket/ustav.pdf",
        meta={"collection": "company_profile"},
    )
    assert isinstance(result, DocumentIngestResult)
    assert result.chunks == 2
    body = _body(captured[0])
    assert body["natural_key"] == "doc:1"
    assert body["namespace"] == "tp:tenant:a"
    assert body["source_path"] == "s3://bucket/ustav.pdf"
    assert body["meta"] == {"collection": "company_profile"}
    assert body["chunks"] == [
        {"text": "глава 1", "heading": "", "order": 0},
        {"text": "глава 2", "order": 1},
    ]
    assert body["replace"] is True
    assert captured[0].url.path == "/api/brain/documents"


def test_delete_document_uses_query_param() -> None:
    captured: list[httpx.Request] = []
    client = _client(
        captured, {"natural_key": "doc:1", "namespace": "tp:tenant:a", "deleted": True}
    )
    result = client.delete_document("doc:1", namespace="tp:tenant:a")
    assert isinstance(result, DocumentDeleteResult)
    assert result.deleted is True
    request = captured[0]
    assert request.method == "DELETE"
    assert request.url.path == "/api/brain/documents/doc:1"
    assert request.url.params["namespace"] == "tp:tenant:a"


def test_read_narrowing_sent_only_when_given() -> None:
    """allowed_* на recall/search/query — сужение видимости (MEM-ADR-021)."""
    captured: list[httpx.Request] = []
    client = _client(
        captured,
        {"query": "q", "question": "q", "count": 0, "memories": [], "results": [], "sources": []},
    )
    client.recall("q", allowed_scopes=["workspace:w1"])
    client.search("q", allowed_namespaces=["tp:tenant:a"], allowed_scopes=[])
    client.query("q", synthesize=False)
    recall, search, query = (_body(r) for r in captured)
    assert recall["allowedScopes"] == ["workspace:w1"] and "allowedNamespaces" not in recall
    assert search["allowedNamespaces"] == ["tp:tenant:a"]
    assert search["allowedScopes"] == []  # пустой список уходит как есть: «ничего», не «всё»
    assert "allowedScopes" not in query and "allowedNamespaces" not in query
