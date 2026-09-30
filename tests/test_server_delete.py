"""Тесты механизма удаления узлов: DELETE /api/brain/nodes/{key} + защита аудит-контура.

Без БД: сторы подменяются фейками через monkeypatch модуля server.app — проверяем
HTTP-контракт (200/404/400), обязательность аудита и сквозной trace_id.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from platform_memory.graph.store import PROTECTED_TYPES, assert_deletable
from platform_memory.server import app as app_mod
from platform_memory.server.app import normalize_natural_key

pytestmark = pytest.mark.unit


# ---------------- нормализация ключа (reverse-proxy схлопывает //) ----------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https:/e2e.test/x", "https://e2e.test/x"),  # схлопнуто traefik'ом
        ("http:/mos.ru/a", "http://mos.ru/a"),
        ("https://ok.ru/a", "https://ok.ru/a"),  # уже корректный — не трогаем
        ("kb-1", "kb-1"),  # обычный ключ — не трогаем
        ("fact:tr:abc", "fact:tr:abc"),  # двоеточия без слэша — не трогаем
    ],
)
def test_normalize_natural_key(raw, expected):
    assert normalize_natural_key(raw) == expected


# ---------------- чистая функция защиты ----------------


def test_assert_deletable_allows_regular_types():
    for t in ("article", "note", "service", ""):
        assert_deletable(t)  # не бросает


@pytest.mark.parametrize("protected", sorted(PROTECTED_TYPES))
def test_assert_deletable_rejects_audit_contour(protected):
    with pytest.raises(ValueError):
        assert_deletable(protected)


# ---------------- фейковые сторы ----------------


class _Env:
    """Состояние одного теста: узлы, записанные аудиты, удалённые из индекса ключи."""

    def __init__(self, nodes: dict[str, dict]):
        self.nodes = nodes
        self.audits: list[dict] = []
        self.index_deleted: list[str] = []
        self.graph_default_ns: list[str] = []  # default_namespace каждого созданного GraphStore


def _install_fakes(monkeypatch, env: _Env) -> TestClient:
    class FakeConn:
        def close(self) -> None:
            pass

    class FakeGraph:
        def __init__(self, conn, graph_name, default_namespace=""):
            env.graph_default_ns.append(default_namespace)

        def trace_subgraph(self, trace_id, namespace="", allowed_scopes=None):
            return {"trace_id": trace_id, "events": [], "facts": []}

        def delete_node(self, natural_key, namespace="", allowed_scopes=None):
            node = env.nodes.get(natural_key)
            if node is None:
                return None
            assert_deletable(str(node.get("type") or ""))
            del env.nodes[natural_key]
            return node

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

    class FakeIndex:
        def __init__(self, conn, table, dim, default_namespace=""):
            pass

        def delete_for_node(self, node_key, namespace="", allowed_scopes=None):
            env.index_deleted.append(node_key)
            return 3

    monkeypatch.setattr(app_mod.dbmod, "connect", lambda settings: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    monkeypatch.setattr(app_mod, "VectorIndex", FakeIndex)
    return TestClient(app_mod.app)


# ---------------- HTTP-контракт ----------------


def test_delete_happy_path(monkeypatch):
    env = _Env({"kb-1": {"type": "article", "title": "Статья", "namespace": "demo"}})
    client = _install_fakes(monkeypatch, env)

    resp = client.delete("/api/brain/nodes/kb-1", params={"actor": "e2e"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["natural_key"] == "kb-1"
    assert body["type"] == "article" and body["title"] == "Статья"
    assert body["chunks_deleted"] == 3
    assert body["trace_id"]  # авто-uuid, непустой
    # узел удалён из графа и индекса
    assert "kb-1" not in env.nodes
    assert env.index_deleted == ["kb-1"]
    # аудит обязателен: одно событие delete со снапшотом
    assert len(env.audits) == 1
    audit = env.audits[0]
    assert audit["action"] == "delete" and audit["actor"] == "e2e"
    assert audit["payload"]["natural_key"] == "kb-1"
    assert audit["payload"]["type"] == "article"
    assert audit["payload"]["chunks_deleted"] == 3
    assert audit["trace_id"] == body["trace_id"]


def test_delete_missing_node_404_without_audit(monkeypatch):
    env = _Env({})
    client = _install_fakes(monkeypatch, env)

    resp = client.delete("/api/brain/nodes/no-such")

    assert resp.status_code == 404
    assert env.audits == [] and env.index_deleted == []


def test_delete_protected_type_400_keeps_node(monkeypatch):
    env = _Env({"audit:tr:abc": {"type": "audit_event", "title": "delete"}})
    client = _install_fakes(monkeypatch, env)

    resp = client.delete("/api/brain/nodes/audit:tr:abc")

    assert resp.status_code == 400
    assert "audit_event" in resp.json()["detail"]
    assert "audit:tr:abc" in env.nodes  # узел не тронут
    assert env.audits == [] and env.index_deleted == []


def test_delete_trace_id_from_x_run_id_header(monkeypatch):
    env = _Env({"kb-2": {"type": "note", "title": "N"}})
    client = _install_fakes(monkeypatch, env)

    resp = client.delete("/api/brain/nodes/kb-2", headers={"X-Run-Id": "run-77"})

    assert resp.status_code == 200
    assert resp.json()["trace_id"] == "run-77"
    assert env.audits[0]["trace_id"] == "run-77"


def test_delete_query_trace_id_beats_header(monkeypatch):
    env = _Env({"kb-3": {"type": "note", "title": "N"}})
    client = _install_fakes(monkeypatch, env)

    resp = client.delete(
        "/api/brain/nodes/kb-3",
        params={"trace_id": "tr-explicit"},
        headers={"X-Run-Id": "run-88"},
    )

    assert resp.json()["trace_id"] == "tr-explicit"
    assert env.audits[0]["trace_id"] == "tr-explicit"


def test_delete_url_like_key_with_slashes(monkeypatch):
    """retain кладёт external_id=URL как natural_key — DELETE обязан принимать ключи со слэшами."""
    key = "https://www.mos.ru/city-card"
    env = _Env({key: {"type": "article", "title": "Карта"}})
    client = _install_fakes(monkeypatch, env)

    resp = client.delete(f"/api/brain/nodes/{key}")

    assert resp.status_code == 200
    assert resp.json()["natural_key"] == key
    assert key not in env.nodes


def test_routes_use_settings_default_namespace(monkeypatch):
    """Аудит-контур жив только если чтение и запись работают в одном namespace.

    Регрессия: хендлеры создавали GraphStore без settings.default_namespace — при
    кастомном CB_DEFAULT_NAMESPACE запись шла в него, а trace/audit читали fallback.
    """
    env = _Env({"kb-9": {"type": "note", "title": "N"}})
    client = _install_fakes(monkeypatch, env)
    expected = app_mod.get_settings().default_namespace

    client.delete("/api/brain/nodes/kb-9")  # delete-хендлер
    client.get("/api/brain/trace/tr-1")  # трейс-хендлер
    client.post(
        "/api/brain/audit",
        json={"trace_id": "tr-1", "action": "ping", "actor": "t"},
    )  # аудит-хендлер

    assert env.graph_default_ns and all(ns == expected for ns in env.graph_default_ns)


def test_delete_key_collapsed_by_reverse_proxy(monkeypatch):
    """Traefik схлопывает `//` в пути: `https://x` приходит как `https:/x` — восстанавливаем."""
    key = "https://www.mos.ru/city-card"
    env = _Env({key: {"type": "article", "title": "Карта"}})
    client = _install_fakes(monkeypatch, env)

    resp = client.delete("/api/brain/nodes/https:/www.mos.ru/city-card")  # один слэш

    assert resp.status_code == 200
    assert resp.json()["natural_key"] == key
    assert key not in env.nodes
