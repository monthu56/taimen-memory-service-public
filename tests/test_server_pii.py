"""Тесты защиты ПДн на HTTP-слое: допуски токенов, маскирование выдачи, журнал доступа.

Без БД: retrieve/сторы подменяются фейками; настройки — model_copy реальных settings
с нужными флагами. Главный регрессионный тест: при выключенной CB_PII_PROTECTION
поведение прежнее (ни маскирования, ни новых полей, ни журнала).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from platform_memory.index.store import SearchHit
from platform_memory.retrieval.query import Source
from platform_memory.server import app as app_mod

pytestmark = pytest.mark.unit

PHONE_TEXT = "Клиент Иванов, тел +7 999 123-45-67, вопрос по карте."


class _Env:
    def __init__(self):
        self.audits: list[dict] = []


def _hit(text: str = PHONE_TEXT) -> SearchHit:
    return SearchHit(
        chunk_id=1,
        node_key="kb-1",
        source_path="/kb.md",
        title="Запись",
        heading="H",
        text=text,
        score=0.9,
    )


def _install(monkeypatch, env: _Env, *, protection: bool, base="base", admin="admin"):
    settings = app_mod.get_settings().model_copy(
        update={
            "pii_protection": protection,
            "server_api_key": base,
            "server_api_keys_pii": admin,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    def fake_retrieve(_settings, _q, k=8, hops=1, namespaces=(), **kw):
        return SimpleNamespace(
            hits=[_hit()],
            neighbors={},
            sources=[Source("/kb.md", "kb-1", "Запись")],
            context=PHONE_TEXT,
        )

    monkeypatch.setattr(app_mod, "retrieve", fake_retrieve)

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeGraph:
        def __init__(self, conn, graph_name, default_namespace=""):
            pass

        def get_node(self, natural_key, namespaces=()):
            return {
                "natural_key": natural_key,
                "type": "article",
                "content": PHONE_TEXT,
            }

        def expand_neighbors(self, keys, hops=1, namespaces=()):
            return []

        def record_audit(self, trace_id, actor, action, *, payload=None, **kw):
            env.audits.append(
                {
                    "trace_id": trace_id,
                    "actor": actor,
                    "action": action,
                    "payload": payload,
                }
            )
            return {"trace_id": trace_id}

    monkeypatch.setattr(app_mod.dbmod, "connect", lambda s: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    return TestClient(app_mod.app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------- допуски ----------------


def test_masked_token_gets_masked_query(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, protection=True)

    resp = client.post("/api/brain/query", json={"question": "q"}, headers=_bearer("base"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["pii_masked"] is True
    assert "[ПДн:phone]" in body["context"]
    assert "+7 999" not in str(body)  # телефон не утёк ни в одном поле
    assert env.audits == []  # маскированная выдача не журналируется


def test_full_token_gets_original_and_audit(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, protection=True)

    resp = client.post(
        "/api/brain/query",
        json={"question": "q"},
        headers={**_bearer("admin"), "X-Run-Id": "tr-pii"},
    )

    body = resp.json()
    assert "pii_masked" not in body
    assert "+7 999 123-45-67" in body["context"]
    # журнал доступа: одно событие pii_access с категориями и меткой токена
    assert len(env.audits) == 1
    audit = env.audits[0]
    assert audit["action"] == "pii_access" and audit["actor"] == "pii-full"
    assert audit["trace_id"] == "tr-pii"
    assert audit["payload"]["route"] == "query"
    assert audit["payload"]["categories"].get("phone", 0) >= 1


def test_wrong_token_401(monkeypatch):
    client = _install(monkeypatch, _Env(), protection=True)
    assert (
        client.post("/api/brain/query", json={"question": "q"}, headers=_bearer("nope")).status_code
        == 401
    )


def test_recall_and_node_masked(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, protection=True)

    recall = client.post("/api/brain/recall", json={"query": "q"}, headers=_bearer("base")).json()
    assert recall["pii_masked"] is True
    assert "[ПДн:phone]" in recall["memories"][0]["text"]

    node = client.get("/api/brain/nodes/kb-1", headers=_bearer("base")).json()
    assert node["pii_masked"] is True
    assert "[ПДн:phone]" in node["node"]["content"]


# ---------------- регрессия: защита выключена ----------------


def test_protection_off_keeps_behavior(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, protection=False)

    body = client.post("/api/brain/query", json={"question": "q"}, headers=_bearer("base")).json()

    assert "pii_masked" not in body  # ни нового поля,
    assert "+7 999 123-45-67" in body["context"]  # ни маскирования,
    assert env.audits == []  # ни журнала


def test_protection_off_base_token_still_works(monkeypatch):
    client = _install(monkeypatch, _Env(), protection=False)
    assert (
        client.post("/api/brain/query", json={"question": "q"}, headers=_bearer("base")).status_code
        == 200
    )


# ---------------- retain: маркировка при записи ----------------


def _install_retain(monkeypatch, *, protection: bool):
    settings = app_mod.get_settings().model_copy(
        update={
            "pii_protection": protection,
            "server_api_key": "",
            "server_api_keys_pii": "",
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    captured: dict = {}

    def fake_write(_settings, natural_key, type_, title, **kw):
        captured.update({"natural_key": natural_key, "properties": kw.get("properties")})
        return {"natural_key": natural_key, "namespace": "demo", "origin": "agent"}

    monkeypatch.setattr(app_mod, "write_and_index_fact", fake_write)
    return TestClient(app_mod.app), captured


def test_retain_autodetects_pii(monkeypatch):
    client, captured = _install_retain(monkeypatch, protection=True)

    resp = client.post("/api/brain/retain", json={"content": PHONE_TEXT, "type": "article"})

    assert resp.status_code == 201
    assert resp.json()["pii_categories"] == ["phone"]
    assert captured["properties"]["pii"] is True
    assert captured["properties"]["pii_categories"] == ["phone"]


def test_retain_explicit_pii_merges(monkeypatch):
    client, captured = _install_retain(monkeypatch, protection=True)

    resp = client.post(
        "/api/brain/retain",
        json={"content": PHONE_TEXT, "pii": True, "pii_categories": ["fio"]},
    )

    assert resp.json()["pii_categories"] == ["fio", "phone"]  # явное + авто
    assert captured["properties"]["pii_categories"] == ["fio", "phone"]


def test_retain_clean_content_unmarked(monkeypatch):
    client, captured = _install_retain(monkeypatch, protection=True)

    resp = client.post("/api/brain/retain", json={"content": "Карта действует 5 лет."})

    assert "pii_categories" not in resp.json()
    assert "pii" not in captured["properties"]


def test_retain_protection_off_no_marking(monkeypatch):
    client, captured = _install_retain(monkeypatch, protection=False)

    resp = client.post("/api/brain/retain", json={"content": PHONE_TEXT})

    assert "pii_categories" not in resp.json()
    assert "pii" not in captured["properties"]
