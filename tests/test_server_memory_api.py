"""Контракт /api/memory/* (ADR-016 §33): observations, context, trace, delete.

Паттерн проекта: фейки подменяют модульные имена в server.memory_api (движок
вызывается late-binding), get_settings — в server.app. БД не требуется.
"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

import platform_memory.server.app as app_mod
import platform_memory.server.memory_api as api_mod
from platform_memory.context.models import ContextItem, ContextPack, ContextSection
from platform_memory.observations.store import ObservationRecord

pytestmark = pytest.mark.unit

SECRET_PHONE = "+7 999 123-45-67"


class _Env:
    """Захваченное состояние фейкового движка."""

    def __init__(self):
        self.retained: list[dict] = []
        self.batch_calls: list[list[dict]] = []
        self.context_requests: list[dict] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.records: dict[str, ObservationRecord] = {}
        self.traces: dict[str, dict] = {}


class _FakeConn:
    def close(self):
        pass


class _FakeObsStore:
    env: _Env

    def __init__(self, conn, table, default_namespace=""):
        self.default_namespace = default_namespace

    def table_exists(self):
        return True

    def get(self, oid, namespaces=()):
        return self.env.records.get(oid)

    def list_recent(self, namespaces=(), *, scopes=(), kinds=(), status="", limit=50):
        return list(self.env.records.values())[:limit]

    def counts_by_status(self, namespaces=()):
        return {"processed": len(self.env.records)}


class _FakeTraceStore:
    env: _Env

    def __init__(self, conn, table, default_namespace=""):
        pass

    def table_exists(self):
        return True

    def get(self, trace_id, namespaces=()):
        return self.env.traces.get(trace_id)


def _install(monkeypatch, env: _Env, **settings_overrides) -> TestClient:
    settings = app_mod.get_settings().model_copy(
        update={
            "server_api_key": "",
            "server_api_keys_pii": "",
            "pii_protection": False,
            **settings_overrides,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    def fake_retain(s, payload, *, namespace="", **kw):
        env.retained.append({"payload": payload, "namespace": namespace})
        duplicate = any(
            r["payload"].get("external_id") == payload.get("external_id")
            and payload.get("external_id")
            for r in env.retained[:-1]
        )
        return {"observation_id": "obs-test1", "duplicate": duplicate, "status": "processed"}

    def fake_retain_batch(s, payloads, *, namespace="", **kw):
        env.batch_calls.append(list(payloads))
        results, accepted, failed = [], 0, 0
        for i, p in enumerate(payloads):
            if p.get("bad"):
                failed += 1
                results.append({"index": i, "error": "невалидное наблюдение"})
            else:
                accepted += 1
                results.append(
                    {
                        "index": i,
                        "observation_id": f"obs-{i}",
                        "duplicate": False,
                        "status": "processed",
                    }
                )
        return {"results": results, "accepted": accepted, "duplicates": 0, "failed": failed}

    def fake_build_context(s, payload, **kw):
        env.context_requests.append(payload)
        return ContextPack(
            query=payload.get("query", ""),
            sections=[
                ContextSection(
                    "documents",
                    [
                        ContextItem(
                            kind="document",
                            id="chunk:1",
                            text=f"Телефон клиента {SECRET_PHONE} тут",
                            source_path="vault/doc.md",
                        )
                    ],
                )
            ],
            sources=[{"source_path": "vault/doc.md", "node_key": "n1", "title": "Doc"}],
            token_estimate=42,
            trace_id="ctx-abc",
        )

    def fake_delete(s, oid, *, namespace="", mode="redact", actor="api", trace_id=""):
        if oid not in env.records:
            return None
        env.deleted.append((oid, mode, namespace))
        return {"observation_id": oid, "mode": mode, "chunks_deleted": 1, "facts_evidence_lost": 0}

    _FakeObsStore.env = env
    _FakeTraceStore.env = env
    monkeypatch.setattr(api_mod, "retain_observation", fake_retain)
    monkeypatch.setattr(api_mod, "retain_observations", fake_retain_batch)
    monkeypatch.setattr(api_mod, "build_context", fake_build_context)
    monkeypatch.setattr(api_mod, "delete_observation", fake_delete)
    monkeypatch.setattr(api_mod, "consolidate", lambda s, **kw: {"processed": 0, "statuses": {}})
    monkeypatch.setattr(api_mod, "ObservationStore", _FakeObsStore)
    monkeypatch.setattr(api_mod, "ContextTraceStore", _FakeTraceStore)
    monkeypatch.setattr(api_mod.dbmod, "connect", lambda s, **kw: _FakeConn())
    return TestClient(app_mod.app)


def test_observe_created_and_scope_namespace(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/memory/observations",
        json={"kind": "work.completed", "content": "done", "scope": {"namespace": "bank"}},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["observation_id"] == "obs-test1"
    assert body["duplicate"] is False
    assert env.retained[0]["namespace"] == "bank"
    # scope не протекает в payload наблюдения
    assert "scope" not in env.retained[0]["payload"]


def test_observe_duplicate_reported(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    obs = {"content": "x", "external_id": "e1", "source": {"system": "s"}}
    assert client.post("/api/memory/observations", json=obs).json()["duplicate"] is False
    assert client.post("/api/memory/observations", json=obs).json()["duplicate"] is True


def test_observe_invalid_namespace_400(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/memory/observations",
        json={"content": "x", "scope": {"namespace": "BAD NS"}},
    )
    assert resp.status_code == 400


def test_batch_partial_errors(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/memory/observations:batch",
        json={"observations": [{"content": "a"}, {"bad": True}], "scope": {"namespace": "bank"}},
    )
    assert resp.status_code == 207
    body = resp.json()
    assert body["accepted"] == 1
    assert body["failed"] == 1
    assert "error" in body["results"][1]


def test_batch_over_limit_413(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, observations_max_batch=2)
    resp = client.post(
        "/api/memory/observations:batch",
        json={"observations": [{"content": str(i)} for i in range(3)]},
    )
    assert resp.status_code == 413


def test_get_observation_found_and_404(monkeypatch):
    env = _Env()
    env.records["obs-1"] = ObservationRecord(
        observation_id="obs-1", namespace="nexus", content="hello", status="processed"
    )
    client = _install(monkeypatch, env)
    assert client.get("/api/memory/observations/obs-1").json()["content"] == "hello"
    assert client.get("/api/memory/observations/obs-nope").status_code == 404


def test_context_passes_scope_namespaces(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/api/memory/context",
        json={
            "query": "deploy issue",
            "scope": {"namespaces": ["bank", "shared"]},
            "scopes": ["project:x"],
            "budget": {"tokens": 4000},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == "ctx-abc"
    assert body["sections"][0]["items"][0]["id"] == "chunk:1"
    sent = env.context_requests[0]
    assert sent["namespaces"] == ["bank", "shared"]
    assert sent["scopes"] == ["project:x"]
    assert "scope" not in sent


def test_context_pii_masked_for_base_token(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, server_api_key="base-key", pii_protection=True)
    resp = client.post(
        "/api/memory/context",
        json={"query": "q"},
        headers={"Authorization": "Bearer base-key"},
    )
    assert resp.status_code == 200
    assert SECRET_PHONE not in resp.text  # ПДн не утекли ни в одном поле
    assert resp.json().get("pii_masked") is True


def test_observation_content_masked_for_base_token(monkeypatch):
    env = _Env()
    env.records["obs-1"] = ObservationRecord(
        observation_id="obs-1", namespace="nexus", content=f"тел. {SECRET_PHONE}"
    )
    client = _install(monkeypatch, env, server_api_key="base-key", pii_protection=True)
    resp = client.get(
        "/api/memory/observations/obs-1", headers={"Authorization": "Bearer base-key"}
    )
    assert SECRET_PHONE not in resp.text


def test_auth_required_when_key_configured(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, server_api_key="secret")
    assert client.post("/api/memory/context", json={"query": "q"}).status_code == 401
    assert (
        client.post(
            "/api/memory/observations",
            json={"content": "x"},
        ).status_code
        == 401
    )
    ok = client.post(
        "/api/memory/context",
        json={"query": "q"},
        headers={"Authorization": "Bearer secret"},
    )
    assert ok.status_code == 200


def test_trace_found_and_404(monkeypatch):
    env = _Env()
    env.traces["ctx-abc"] = {"trace_id": "ctx-abc", "decisions": {"stats": {}}}
    client = _install(monkeypatch, env)
    assert client.get("/api/memory/context/trace/ctx-abc").json()["trace_id"] == "ctx-abc"
    assert client.get("/api/memory/context/trace/ctx-zzz").status_code == 404


def test_delete_observation_modes(monkeypatch):
    env = _Env()
    env.records["obs-1"] = ObservationRecord(observation_id="obs-1", namespace="nexus")
    client = _install(monkeypatch, env)
    resp = client.delete("/api/memory/observations/obs-1?mode=purge")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert env.deleted[0][1] == "purge"
    assert client.delete("/api/memory/observations/obs-x").status_code == 404
    assert client.delete("/api/memory/observations/obs-1?mode=explode").status_code == 422


def test_observations_list(monkeypatch):
    env = _Env()
    env.records["obs-1"] = ObservationRecord(observation_id="obs-1", namespace="nexus", content="c")
    client = _install(monkeypatch, env)
    body = client.get("/api/memory/observations").json()
    assert body["count"] == 1
    assert body["statuses"] == {"processed": 1}


def test_consolidate_endpoint(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post("/api/memory/consolidate")
    assert resp.status_code == 200
    assert resp.json()["processed"] == 0


def test_brain_routes_untouched(monkeypatch):
    """Обратная совместимость: /api/brain/* остаются в приложении (инвариант №2)."""
    paths = set(app_mod.app.openapi()["paths"])
    for expected in (
        "/api/brain/query",
        "/api/brain/recall",
        "/api/brain/retain",
        "/api/brain/search",
        "/api/brain/facts",
        "/api/brain/stats",
        "/api/memory/observations",
        "/api/memory/context",
    ):
        assert expected in paths, expected
