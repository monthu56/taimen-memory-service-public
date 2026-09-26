"""Гранты по namespace на HTTP-слое (CB_API_KEYS, ADR-017) поверх модели допусков ADR-002.

Без БД: retrieve/сторы/документы подменяются фейками. Главные регрессии: прежние ключи
(CB_SERVER_API_KEY, CB_SERVER_API_KEYS_PII) остаются full-access по всем базам знаний и
не меняют допуск к ПДн; без реестра поведение сервиса прежнее. Ключ реестра получает
403 вне своего поддерева, 401 — как раньше для неверного токена.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from platform_memory.index.store import SearchHit
from platform_memory.server import app as app_mod
from platform_memory.server import memory_api as api_mod

pytestmark = pytest.mark.unit

NS_A = "tp:tenant:aaaa"
PHONE_TEXT = "Клиент Иванов, тел +7 999 123-45-67, вопрос по карте."

_KEYS = [
    {
        "name": "tp-api",
        "key": "tp-secret",
        "grants": [
            {"prefix": "tp:", "read": True, "write": True},
            {"prefix": "shared", "read": True, "write": False},
        ],
    },
    {
        "name": "admin-all",
        "key": "admin-secret",
        "pii": True,
        "grants": [{"prefix": "", "read": True, "write": True}],
    },
]


class _Env:
    def __init__(self):
        self.read_namespaces: list[list[str]] = []
        self.meta_filters: list = []
        self.write_namespaces: list[str] = []
        self.documents: list[dict] = []
        self.deleted_documents: list[dict] = []
        self.contexts: list[dict] = []


def _install(monkeypatch, env: _Env, **overrides) -> TestClient:
    settings = app_mod.get_settings().model_copy(
        update={
            "api_keys": json.dumps(_KEYS),
            "server_api_key": "",
            "server_api_keys_pii": "",
            "pii_protection": False,
            "default_namespace": "nexus",
            **overrides,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    def fake_retrieve(_settings, _q, k=8, hops=1, namespaces=(), meta_filter=None, **kw):
        env.read_namespaces.append(list(namespaces))
        env.meta_filters.append(meta_filter)
        hit = SearchHit(
            chunk_id=1,
            node_key="kb-1",
            source_path="/kb.md",
            title="Запись",
            heading="H",
            text=PHONE_TEXT,
            score=0.9,
            namespace=namespaces[0] if namespaces else "nexus",
            meta={"collection": "licenses"},
        )
        return SimpleNamespace(hits=[hit], neighbors={}, sources=[], context=PHONE_TEXT)

    def fake_write(_settings, natural_key, type_, title, **kw):
        env.write_namespaces.append(kw.get("namespace", ""))
        return {"natural_key": natural_key, "namespace": kw.get("namespace", "")}

    def fake_retain_document(_settings, *, namespace, natural_key, **kw):
        env.documents.append({"namespace": namespace, "natural_key": natural_key, **kw})
        return {
            "natural_key": natural_key,
            "namespace": namespace,
            "type": kw.get("node_type"),
            "chunks": len(kw.get("chunks") or []),
            "replaced": kw.get("replace"),
        }

    def fake_delete_document(_settings, natural_key, namespace, **kw):
        env.deleted_documents.append({"natural_key": natural_key, "namespace": namespace, **kw})
        return {"natural_key": natural_key, "namespace": namespace, "deleted": True}

    def fake_build_context(_settings, payload):
        env.contexts.append(payload)
        return SimpleNamespace(to_payload=lambda: {"query": payload.get("query"), "sections": []})

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeGraph:
        def __init__(self, conn, graph_name, default_namespace=""):
            pass

        def list_nodes(self, type=None, status=None, limit=20, namespaces=()):
            return []

        def record_audit(self, trace_id, actor, action, **kw):
            return {"trace_id": trace_id}

        def total_nodes(self):
            return 0

        def count_nodes_by_type(self, namespaces=None):
            return {}

        def count_edges_by_type(self, namespaces=None):
            return {}

        def trace_subgraph(self, trace_id, namespace=""):
            return {"trace_id": trace_id, "events": [], "facts": []}

    class FakeIndex:
        def __init__(self, conn, table, dim, default_namespace=""):
            pass

        def table_exists(self):
            return False

    monkeypatch.setattr(app_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(app_mod, "write_and_index_fact", fake_write)
    monkeypatch.setattr(app_mod, "retain_document", fake_retain_document)
    monkeypatch.setattr(app_mod, "delete_document", fake_delete_document)
    monkeypatch.setattr(app_mod.dbmod, "connect", lambda s: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    monkeypatch.setattr(app_mod, "VectorIndex", FakeIndex)
    monkeypatch.setattr(api_mod, "build_context", fake_build_context)
    return TestClient(app_mod.app)


def _recall(namespaces: list[str] | None = None) -> dict:
    body: dict = {"query": "тест"}
    if namespaces is not None:
        body["scope"] = {"namespaces": namespaces}
    return body


TP = {"Authorization": "Bearer tp-secret"}
ADMIN = {"Authorization": "Bearer admin-secret"}


# ---------------- реестр: 401 / 403 / 200 ----------------


def test_missing_or_wrong_key_is_401(monkeypatch):
    client = _install(monkeypatch, _Env())
    assert client.post("/api/brain/recall", json=_recall([NS_A])).status_code == 401
    wrong = {"Authorization": "Bearer wrong"}
    assert client.post("/api/brain/recall", json=_recall([NS_A]), headers=wrong).status_code == 401


def test_own_subtree_read_and_write_allowed(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    assert client.post("/api/brain/recall", json=_recall([NS_A]), headers=TP).status_code == 200
    write = client.post(
        "/api/brain/retain", json={"content": "факт", "scope": {"namespace": NS_A}}, headers=TP
    )
    assert write.status_code == 201
    assert env.write_namespaces[-1] == NS_A


def test_foreign_namespace_is_403(monkeypatch):
    client = _install(monkeypatch, _Env())
    res = client.post("/api/brain/recall", json=_recall(["nexus"]), headers=TP)
    assert res.status_code == 403
    assert "nexus" in res.json()["detail"]


def test_default_namespace_denied_for_scoped_key(monkeypatch):
    """Без scope запрос падает в default namespace сервиса — tp-ключу он не разрешён."""
    client = _install(monkeypatch, _Env())
    assert client.post("/api/brain/recall", json=_recall(), headers=TP).status_code == 403
    assert client.post("/api/brain/retain", json={"content": "x"}, headers=TP).status_code == 403


def test_mixed_scope_denied_when_one_namespace_foreign(monkeypatch):
    client = _install(monkeypatch, _Env())
    res = client.post(
        "/api/brain/query",
        json={"question": "q", "scope": {"namespaces": [NS_A, "nexus"]}},
        headers=TP,
    )
    assert res.status_code == 403


def test_shared_is_read_only_for_tp_key(monkeypatch):
    client = _install(monkeypatch, _Env())
    assert client.post("/api/brain/recall", json=_recall(["shared"]), headers=TP).status_code == 200
    write = client.post(
        "/api/brain/retain", json={"content": "x", "scope": {"namespace": "shared"}}, headers=TP
    )
    assert write.status_code == 403
    audit = client.post(
        "/api/brain/audit",
        json={"trace_id": "t", "action": "a", "scope": {"namespace": "shared"}},
        headers=TP,
    )
    assert audit.status_code == 403


def test_query_param_routes_authorized(monkeypatch):
    client = _install(monkeypatch, _Env())
    assert client.get("/api/brain/nodes", params={"namespace": NS_A}, headers=TP).status_code == 200
    assert client.get("/api/brain/nodes", headers=TP).status_code == 403  # default ns
    assert (
        client.get("/api/brain/trace/t1", params={"namespace": NS_A}, headers=TP).status_code == 200
    )
    assert (
        client.get("/api/brain/trace/t1", params={"namespace": "nexus"}, headers=TP).status_code
        == 403
    )


def test_global_stats_only_for_empty_prefix_grant(monkeypatch):
    client = _install(monkeypatch, _Env())
    assert client.get("/api/brain/stats", headers=TP).status_code == 403
    assert client.get("/api/brain/stats", headers=ADMIN).status_code == 200


def test_health_checks_token_only(monkeypatch):
    client = _install(monkeypatch, _Env())
    assert client.get("/api/brain/health", headers=TP).status_code == 200
    assert client.get("/api/brain/health").status_code == 401


def test_invalid_registry_is_500(monkeypatch):
    client = _install(monkeypatch, _Env(), api_keys="{not json")
    assert client.post("/api/brain/recall", json=_recall([NS_A]), headers=TP).status_code == 500


# ---------------- обратная совместимость: legacy-ключи и локальный режим ----------------


def test_legacy_key_is_full_access_across_namespaces(monkeypatch):
    client = _install(monkeypatch, _Env(), server_api_key="legacy-secret")
    legacy = {"Authorization": "Bearer legacy-secret"}
    assert (
        client.post("/api/brain/recall", json=_recall(["nexus"]), headers=legacy).status_code == 200
    )
    write = client.post(
        "/api/brain/retain", json={"content": "x", "scope": {"namespace": "any"}}, headers=legacy
    )
    assert write.status_code == 201
    assert client.get("/api/brain/stats", headers=legacy).status_code == 200


def test_pii_key_is_full_access_and_full_pii(monkeypatch):
    client = _install(monkeypatch, _Env(), server_api_keys_pii="admin-pii", pii_protection=True)
    pii = {"Authorization": "Bearer admin-pii"}
    res = client.post("/api/brain/recall", json=_recall(["nexus"]), headers=pii)
    assert res.status_code == 200
    assert "pii_masked" not in res.json()
    assert "+7 999 123-45-67" in res.json()["context"]


def test_registry_key_masked_unless_pii_flag(monkeypatch):
    client = _install(monkeypatch, _Env(), pii_protection=True)
    masked = client.post("/api/brain/recall", json=_recall([NS_A]), headers=TP).json()
    assert masked["pii_masked"] is True
    assert "+7 999 123-45-67" not in masked["context"]
    full = client.post("/api/brain/recall", json=_recall([NS_A]), headers=ADMIN).json()
    assert "pii_masked" not in full
    assert "+7 999 123-45-67" in full["context"]


def test_no_keys_at_all_is_local_mode(monkeypatch):
    client = _install(monkeypatch, _Env(), api_keys="")
    assert client.post("/api/brain/recall", json=_recall(["nexus"])).status_code == 200


# ---------------- /api/memory/* авторизуется теми же грантами ----------------


def test_memory_api_context_and_observations_authorized(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    ok = client.post(
        "/api/memory/context", json={"query": "q", "scope": {"namespaces": [NS_A]}}, headers=TP
    )
    assert ok.status_code == 200
    assert env.contexts[-1]["namespaces"] == [NS_A]
    denied = client.post(
        "/api/memory/context", json={"query": "q", "scope": {"namespaces": ["nexus"]}}, headers=TP
    )
    assert denied.status_code == 403
    obs = client.post(
        "/api/memory/observations",
        json={"content": "x", "scope": {"namespace": "shared"}},
        headers=TP,
    )
    assert obs.status_code == 403  # shared — только чтение
    assert (
        client.get(
            "/api/memory/observations", params={"namespace": "nexus"}, headers=TP
        ).status_code
        == 403
    )


# ---------------- filters.meta и documents ----------------


def test_search_passes_meta_filter_to_engine(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    res = client.post(
        "/api/brain/search",
        json={
            "query": "q",
            "filters": {"meta": {"collection": "licenses"}},
            "scope": {"namespace": NS_A},
        },
        headers=TP,
    )
    assert res.status_code == 200
    assert env.meta_filters[-1] == {"collection": "licenses"}
    body = res.json()
    assert body["results"][0]["meta"] == {"collection": "licenses"}
    assert body["results"][0]["namespace"] == NS_A
    # структурный режим и невалидный meta — фильтр не пробрасывается
    client.post(
        "/api/brain/search",
        json={"query": "q", "filters": {"meta": "x"}, "scope": {"namespace": NS_A}},
        headers=TP,
    )
    assert env.meta_filters[-1] is None


def test_document_ingest_authorized_by_write_grant(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    body = {
        "namespace": NS_A,
        "natural_key": "doc:x",
        "title": "Док",
        "chunks": [{"text": "текст"}],
    }
    res = client.post("/api/brain/documents", json=body, headers=TP)
    assert res.status_code == 201
    assert res.json()["chunks"] == 1 and res.json()["namespace"] == NS_A
    assert env.documents[-1]["chunks"] == [{"text": "текст", "heading": ""}]
    assert (
        client.post(
            "/api/brain/documents", json={**body, "namespace": "nexus"}, headers=TP
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/brain/documents", json={**body, "namespace": "shared"}, headers=TP
        ).status_code
        == 403
    )


def test_document_scope_namespace_alias_and_conflict(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    body = {"natural_key": "doc:y", "title": "Док", "chunks": [], "scope": {"namespace": NS_A}}
    assert client.post("/api/brain/documents", json=body, headers=TP).status_code == 201
    assert env.documents[-1]["namespace"] == NS_A
    conflict = {**body, "namespace": "tp:tenant:bbbb"}
    assert client.post("/api/brain/documents", json=conflict, headers=TP).status_code == 400


def test_document_ingest_marks_pii_when_protection_on(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, pii_protection=True)
    body = {
        "namespace": NS_A,
        "natural_key": "doc:p",
        "title": "Док",
        "chunks": [{"text": PHONE_TEXT}],
    }
    res = client.post("/api/brain/documents", json=body, headers=TP)
    assert res.status_code == 201
    assert res.json()["pii_categories"] == ["phone"]
    assert env.documents[-1]["properties"] == {"pii": True, "pii_categories": ["phone"]}


def test_document_ingest_too_many_chunks_is_422(monkeypatch):
    client = _install(monkeypatch, _Env())
    body = {
        "namespace": NS_A,
        "natural_key": "doc:big",
        "title": "Док",
        "chunks": [{"text": "t"}] * 501,
    }
    assert client.post("/api/brain/documents", json=body, headers=TP).status_code == 422


def test_document_delete_authorized_and_normalizes_key(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    res = client.delete(
        "/api/brain/documents/https:/kb.local/doc", params={"namespace": NS_A}, headers=TP
    )
    assert res.status_code == 200
    assert env.deleted_documents[-1]["natural_key"] == "https://kb.local/doc"
    assert env.deleted_documents[-1]["namespace"] == NS_A
    assert (
        client.delete(
            "/api/brain/documents/doc:x", params={"namespace": "nexus"}, headers=TP
        ).status_code
        == 403
    )
