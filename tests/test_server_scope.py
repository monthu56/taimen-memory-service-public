"""Тесты изоляции баз знаний: scope.namespace доведён от HTTP до движка (ADR-003).

Без БД: retrieve/write_and_index_fact/GraphStore подменяются фейками, которые
захватывают переданные namespaces. Регрессия: запросы без scope работают в
дефолтном namespace (пустые аргументы — дефолт применяет движок, как раньше).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from platform_memory.core.namespaces import MAX_READ_NAMESPACES
from fastapi.testclient import TestClient

from platform_memory.server import app as app_mod

pytestmark = pytest.mark.unit


class _Env:
    def __init__(self):
        self.read_namespaces: list = []  # что ушло в retrieve(namespaces=…)
        self.write_namespaces: list = []  # что ушло в write_and_index_fact(namespace=…)
        self.graph_calls: list[dict] = []  # вызовы методов GraphStore с ns


def _install(monkeypatch, env: _Env):
    def fake_retrieve(_settings, _q, k=8, hops=1, namespaces=(), **kw):
        env.read_namespaces.append(list(namespaces))
        return SimpleNamespace(hits=[], neighbors={}, sources=[], context="")

    def fake_write(_settings, natural_key, type_, title, **kw):
        env.write_namespaces.append(kw.get("namespace", ""))
        return {
            "natural_key": natural_key,
            "namespace": kw.get("namespace", ""),
            "origin": "agent",
        }

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeGraph:
        def __init__(self, conn, graph_name, default_namespace=""):
            pass

        def get_node(self, natural_key, namespaces=()):
            env.graph_calls.append({"op": "get_node", "namespaces": list(namespaces)})
            return {"natural_key": natural_key, "type": "article", "content": "x"}

        def expand_neighbors(self, keys, hops=1, namespaces=()):
            env.graph_calls.append({"op": "expand", "namespaces": list(namespaces)})
            return []

        def list_nodes(self, type=None, status=None, limit=20, namespaces=()):
            env.graph_calls.append({"op": "list", "namespaces": list(namespaces)})
            return []

        def delete_node(self, natural_key, namespace="", allowed_scopes=None):
            env.graph_calls.append({"op": "delete", "namespace": namespace})
            return {"type": "article", "title": "t", "namespace": namespace}

        def record_audit(self, trace_id, actor, action, *, namespace="", **kw):
            env.graph_calls.append({"op": "audit", "action": action, "namespace": namespace})
            return {"trace_id": trace_id}

        def trace_subgraph(self, trace_id, namespace="", allowed_scopes=None):
            env.graph_calls.append({"op": "trace", "namespace": namespace})
            return {"trace_id": trace_id, "events": [], "facts": []}

    class FakeIndex:
        def __init__(self, conn, table, dim, default_namespace=""):
            pass

        def delete_for_node(self, node_key, namespace="", allowed_scopes=None):
            return 1

    monkeypatch.setattr(app_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(app_mod, "write_and_index_fact", fake_write)
    monkeypatch.setattr(app_mod.dbmod, "connect", lambda s: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    monkeypatch.setattr(app_mod, "VectorIndex", FakeIndex)
    return TestClient(app_mod.app)


# ---------------- чтение: один ns и список ----------------


def test_recall_scope_namespace(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post("/api/brain/recall", json={"query": "q", "scope": {"namespace": "bank"}})
    assert resp.status_code == 200
    assert env.read_namespaces[-1] == ["bank"]


def test_query_scope_namespaces_list(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/brain/query",
        json={"question": "q", "scope": {"namespaces": ["bank", "shared"]}},
    )
    assert resp.status_code == 200
    assert env.read_namespaces[-1] == ["bank", "shared"]


def test_search_semantic_and_structural_scope(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    client.post("/api/brain/search", json={"query": "q", "scope": {"namespace": "bank"}})
    assert env.read_namespaces[-1] == ["bank"]
    client.post(
        "/api/brain/search",
        json={
            "query": "",
            "filters": {"type": "article"},
            "scope": {"namespace": "bank"},
        },
    )
    assert env.graph_calls[-1] == {"op": "list", "namespaces": ["bank"]}


def test_no_scope_means_engine_default(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    client.post("/api/brain/recall", json={"query": "q"})
    assert env.read_namespaces[-1] == []  # пусто → дефолт применяет движок (как раньше)


# ---------------- запись ----------------


def test_retain_writes_to_scope_namespace(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/brain/retain", json={"content": "текст", "scope": {"namespace": "bank"}}
    )
    assert resp.status_code == 201
    assert env.write_namespaces[-1] == "bank"


def test_retain_preserves_full_provenance_metadata(monkeypatch):
    env = _Env()
    captured: dict = {}

    def fake_write(_settings, natural_key, type_, title, **kw):
        captured.update(kw)
        return {"natural_key": natural_key, "namespace": kw.get("namespace", "")}

    client = _install(monkeypatch, env)
    monkeypatch.setattr(app_mod, "write_and_index_fact", fake_write)
    provenance = {
        "source": "git",
        "repository": "example/repo",
        "commit_sha": "a" * 40,
        "path": "обзор.md",
        "content_hash": "sha256:abc",
    }
    resp = client.post(
        "/api/brain/retain",
        json={"content": "документ", "provenance": provenance, "scope": {"namespace": "bank"}},
    )

    assert resp.status_code == 201
    assert captured["properties"]["provenance"] == provenance
    assert captured["properties"]["source"] == "git"


def test_facts_write_to_scope_namespace(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/brain/facts",
        json={
            "natural_key": "k1",
            "type": "note",
            "title": "t",
            "scope": {"namespace": "bank"},
        },
    )
    assert resp.status_code == 201
    assert env.write_namespaces[-1] == "bank"


# ---------------- узлы / трейс / аудит ----------------


def test_node_get_and_delete_namespace_param(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    client.get("/api/brain/nodes/kb-1", params={"namespace": "bank"})
    assert {"op": "get_node", "namespaces": ["bank"]} in env.graph_calls
    client.delete("/api/brain/nodes/kb-1", params={"namespace": "bank"})
    assert {"op": "delete", "namespace": "bank"} in env.graph_calls
    # аудит удаления — в том же namespace
    audit = [c for c in env.graph_calls if c["op"] == "audit" and c["action"] == "delete"][-1]
    assert audit["namespace"] == "bank"


def test_nodes_list_and_trace_namespace(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    client.get("/api/brain/nodes", params={"type": "article", "namespace": "bank"})
    assert env.graph_calls[-1] == {"op": "list", "namespaces": ["bank"]}
    client.get("/api/brain/trace/tr-1", params={"namespace": "bank"})
    assert env.graph_calls[-1] == {"op": "trace", "namespace": "bank"}


def test_audit_route_namespace(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    client.post(
        "/api/brain/audit",
        json={
            "trace_id": "tr",
            "action": "ping",
            "actor": "t",
            "scope": {"namespace": "bank"},
        },
    )
    audit = [c for c in env.graph_calls if c["op"] == "audit"][-1]
    assert audit["namespace"] == "bank"


# ---------------- валидация ----------------


def test_invalid_namespace_400(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/brain/recall",
        json={"query": "q", "scope": {"namespace": "ПРОБЕЛЫ НЕЛЬЗЯ"}},
    )
    assert resp.status_code == 400


def test_too_many_namespaces_400(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/brain/recall",
        json={
            "query": "q",
            "scope": {"namespaces": [f"ns{i}" for i in range(MAX_READ_NAMESPACES + 1)]},
        },
    )
    assert resp.status_code == 400
