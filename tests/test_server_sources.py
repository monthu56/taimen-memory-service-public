"""Тесты source-view: GET /api/brain/sources/{natural_key} — оригинал статьи целиком.

Без БД: GraphStore/VectorIndex подменяются фейками с раскладкой узлов по
namespace. Контракт: полный content + provenance, строгая изоляция namespace
(404 для чужой базы), PII-защита как на остальных маршрутах.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from platform_memory.server import app as app_mod

pytestmark = pytest.mark.unit

ARTICLE = "Карта москвича действует 5 лет. Продление — в МФЦ."
PHONE_TEXT = "Клиент Иванов, тел +7 999 123-45-67, вопрос по карте."


class _Env:
    """Узлы по (namespace, natural_key) и чанки по (namespace, natural_key)."""

    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}
        self.chunks: dict[tuple[str, str], list[str]] = {}
        self.audits: list[dict] = []


def _node(content: str | None = ARTICLE, **extra) -> dict:
    props = dict(extra.pop("props", {}))
    if content is not None:
        props["content"] = content
    return {
        "natural_key": "https://kb.example/card",
        "namespace": "bank",
        "type": "article",
        "title": "Карта москвича",
        "source_path": "agent:run/r-1",
        "origin": "agent",
        "confidence": 0.9,
        "last_seen": "2026-07-10T00:00:00",
        "props": {"source": "kb.example", "actor": "kb-import", "trace_id": "tr-9", **props},
        **extra,
    }


def _install(monkeypatch, env: _Env, *, protection=False, base="", admin=""):
    settings = app_mod.get_settings().model_copy(
        update={
            "pii_protection": protection,
            "server_api_key": base,
            "server_api_keys_pii": admin,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    default_ns = settings.default_namespace

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeGraph:
        def __init__(self, conn, graph_name, default_namespace=""):
            pass

        def get_node(self, natural_key, namespaces=()):
            nss = list(namespaces) or [default_ns]
            for ns in nss:
                node = env.nodes.get((ns, natural_key))
                if node is not None:
                    return node
            return None

        def record_audit(self, trace_id, actor, action, *, payload=None, namespace="", **kw):
            env.audits.append(
                {"trace_id": trace_id, "action": action, "namespace": namespace, "payload": payload}
            )
            return {"trace_id": trace_id}

    class FakeIndex:
        def __init__(self, conn, table, dim, default_namespace=""):
            pass

        def table_exists(self):
            return True

        def texts_for_node(self, node_key, namespace=""):
            return env.chunks.get((namespace or default_ns, node_key), [])

    monkeypatch.setattr(app_mod.dbmod, "connect", lambda s: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    monkeypatch.setattr(app_mod, "VectorIndex", FakeIndex)
    return TestClient(app_mod.app)


# ---------------- контракт ----------------


def test_source_returns_full_original_and_provenance(monkeypatch):
    env = _Env()
    env.nodes[("bank", "https://kb.example/card")] = _node()
    env.chunks[("bank", "https://kb.example/card")] = ["Карта москвича", "действует 5 лет"]
    client = _install(monkeypatch, env)

    resp = client.get("/api/brain/sources/https://kb.example/card", params={"namespace": "bank"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == ARTICLE  # оригинал целиком, не чанки
    assert body["content_source"] == "original"
    assert body["natural_key"] == "https://kb.example/card"
    assert body["namespace"] == "bank"
    assert body["title"] == "Карта москвича"
    assert body["chunks"] == 2
    prov = body["provenance"]
    assert prov["source"] == "kb.example"
    assert prov["actor"] == "kb-import"
    assert prov["trace_id"] == "tr-9"
    assert prov["origin"] == "agent"
    assert prov["source_path"] == "agent:run/r-1"


def test_source_returns_preserved_provenance_metadata(monkeypatch):
    env = _Env()
    env.nodes[("bank", "https://kb.example/card")] = _node(
        props={
            "provenance": {
                "repository": "example/repo",
                "commit_sha": "a" * 40,
                "path": "обзор.md",
            }
        }
    )
    client = _install(monkeypatch, env)

    body = client.get(
        "/api/brain/sources/https://kb.example/card", params={"namespace": "bank"}
    ).json()

    assert body["provenance"]["metadata"]["repository"] == "example/repo"
    assert body["provenance"]["metadata"]["commit_sha"] == "a" * 40


def test_source_falls_back_to_chunks_without_original(monkeypatch):
    env = _Env()
    env.nodes[("bank", "vault-note")] = {
        **_node(content=None),
        "natural_key": "vault-note",
        "origin": "vault",
    }
    env.chunks[("bank", "vault-note")] = ["Часть 1.", "Часть 2."]
    client = _install(monkeypatch, env)

    body = client.get("/api/brain/sources/vault-note", params={"namespace": "bank"}).json()

    assert body["content"] == "Часть 1.\n\nЧасть 2."
    assert body["content_source"] == "chunks"


# ---------------- изоляция namespace ----------------


def test_source_not_visible_from_other_namespace(monkeypatch):
    env = _Env()
    env.nodes[("bank", "https://kb.example/card")] = _node()
    client = _install(monkeypatch, env)

    resp = client.get(
        "/api/brain/sources/https://kb.example/card", params={"namespace": "mos-card"}
    )

    assert resp.status_code == 404


def test_source_default_namespace_without_param(monkeypatch):
    env = _Env()
    default_ns = app_mod.get_settings().default_namespace
    env.nodes[(default_ns, "kb-1")] = {**_node(), "natural_key": "kb-1", "namespace": default_ns}
    client = _install(monkeypatch, env)

    resp = client.get("/api/brain/sources/kb-1")

    assert resp.status_code == 200
    assert resp.json()["namespace"] == default_ns


def test_source_invalid_namespace_400(monkeypatch):
    client = _install(monkeypatch, _Env())
    resp = client.get("/api/brain/sources/kb-1", params={"namespace": "ПРОБЕЛЫ НЕЛЬЗЯ"})
    assert resp.status_code == 400


def test_source_collapsed_scheme_restored(monkeypatch):
    """Reverse-proxy схлопывает `//`: https:/kb… должен находить узел https://kb…"""
    env = _Env()
    env.nodes[("bank", "https://kb.example/card")] = _node()
    client = _install(monkeypatch, env)

    resp = client.get("/api/brain/sources/https:/kb.example/card", params={"namespace": "bank"})

    assert resp.status_code == 200
    assert resp.json()["natural_key"] == "https://kb.example/card"


# ---------------- ПДн (ADR-002) ----------------


def test_source_masked_for_base_token(monkeypatch):
    env = _Env()
    env.nodes[("bank", "kb-pii")] = {
        **_node(content=PHONE_TEXT),
        "natural_key": "kb-pii",
        "props": {"content": PHONE_TEXT, "pii": True, "pii_categories": ["phone"]},
    }
    client = _install(monkeypatch, env, protection=True, base="base", admin="admin")

    body = client.get(
        "/api/brain/sources/kb-pii",
        params={"namespace": "bank"},
        headers={"Authorization": "Bearer base"},
    ).json()

    assert body["pii_masked"] is True
    assert "[ПДн:phone]" in body["content"]
    assert "+7 999" not in str(body)
    assert env.audits == []  # маскированная выдача не журналируется


def test_source_full_token_logged_in_namespace(monkeypatch):
    env = _Env()
    env.nodes[("bank", "kb-pii")] = {
        **_node(content=PHONE_TEXT),
        "natural_key": "kb-pii",
    }
    client = _install(monkeypatch, env, protection=True, base="base", admin="admin")

    body = client.get(
        "/api/brain/sources/kb-pii",
        params={"namespace": "bank"},
        headers={"Authorization": "Bearer admin", "X-Run-Id": "tr-src"},
    ).json()

    assert "pii_masked" not in body
    assert "+7 999 123-45-67" in body["content"]
    audit = env.audits[-1]
    assert audit["action"] == "pii_access"
    assert audit["trace_id"] == "tr-src"
    assert audit["namespace"] == "bank"  # журнал — в аудит-контуре своей KB
    assert audit["payload"]["route"] == "source"
