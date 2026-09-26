"""Тесты Memory Console (/console): гейт, allowlist KB, серверные операции, утечки.

Без БД: движок подменяется фейками через monkeypatch модуля server.app — Console
обязана ходить в движок только через него (in-process), поэтому фейки действуют
и на неё. Ключевые контракты: выключена по умолчанию (404), namespace строго из
CB_CONSOLE_NAMESPACES, каждая операция несёт выбранный namespace в движок,
API-ключ сервиса не попадает в HTML.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from platform_memory.index.store import SearchHit
from platform_memory.server import app as app_mod

pytestmark = pytest.mark.unit

SECRET = "super-secret-api-key-42"

ARTICLE = {
    "natural_key": "https://kb.example/card",
    "namespace": "bank",
    "type": "article",
    "title": "Карта москвича",
    "source_path": "agent:run/r-1",
    "origin": "agent",
    "last_seen": "2026-07-10T00:00:00",
    "props": {"content": "Карта действует 5 лет.", "source": "kb.example"},
}


class _Env:
    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}
        self.audits: list[dict] = []
        self.index_deleted: list[tuple[str, str]] = []
        self.write_namespaces: list[str] = []
        self.read_namespaces: list[list[str]] = []
        self.questions: list[str] = []


def _install(
    monkeypatch,
    env: _Env,
    *,
    enabled: bool = True,
    namespaces: str = "bank,mos-card",
    api_key: str = "",
):
    settings = app_mod.get_settings().model_copy(
        update={
            "console_enabled": enabled,
            "console_namespaces": namespaces,
            "server_api_key": api_key,
            "server_api_keys_pii": "",
            "pii_protection": False,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    def fake_retrieve(_settings, q, k=8, hops=1, namespaces=(), **kw):
        env.read_namespaces.append(list(namespaces))
        hit = SearchHit(
            chunk_id=1,
            node_key="https://kb.example/card",
            source_path="agent:run/r-1",
            title="Карта москвича",
            heading="Срок действия",
            text="Карта действует 5 лет.",
            score=0.8712,
        )
        return SimpleNamespace(hits=[hit], neighbors=[], sources=[], context="")

    def fake_query(_settings, q, k=8, hops=1, namespaces=(), **kw):
        env.read_namespaces.append(list(namespaces))
        env.questions.append(q)
        return SimpleNamespace(
            text="Карта действует 5 лет; продление — в МФЦ по заявлению.",
            sources=[
                SimpleNamespace(
                    source_path="agent:run/r-1",
                    node_key="https://kb.example/card",
                    title="Карта москвича",
                )
            ],
            hits=1,
            neighbors=0,
        )

    def fake_write(_settings, natural_key, type_, title, **kw):
        ns = kw.get("namespace", "")
        env.write_namespaces.append(ns)
        env.nodes[(ns, natural_key)] = {
            "natural_key": natural_key,
            "namespace": ns,
            "type": type_,
            "title": title,
            "props": kw.get("properties") or {},
        }
        return {"natural_key": natural_key, "namespace": ns, "origin": "agent"}

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeGraph:
        def __init__(self, conn, graph_name, default_namespace=""):
            pass

        def get_node(self, natural_key, namespaces=()):
            for ns in list(namespaces) or ["nexus"]:
                node = env.nodes.get((ns, natural_key))
                if node is not None:
                    return node
            return None

        def list_nodes(self, type=None, status=None, limit=50, namespaces=()):
            nss = set(namespaces)
            out = [
                n
                for (ns, _k), n in env.nodes.items()
                if ns in nss and (type is None or n.get("type") == type)
            ]
            return out[:limit]

        def delete_node(self, natural_key, namespace=""):
            return env.nodes.pop((namespace, natural_key), None)

        def record_audit(self, trace_id, actor, action, *, payload=None, namespace="", **kw):
            event = {
                "trace_id": trace_id,
                "actor": actor,
                "action": action,
                "payload": payload,
                "namespace": namespace,
            }
            env.audits.append(event)
            env.nodes[(namespace, f"audit:{trace_id}:{len(env.audits)}")] = {
                "natural_key": f"audit:{trace_id}:{len(env.audits)}",
                "namespace": namespace,
                "type": "audit_event",
                "title": action,
                "props": {
                    "action": action,
                    "actor": actor,
                    "ts": f"2026-07-18T00:00:{len(env.audits):02d}",
                    "trace_id": trace_id,
                    "payload": payload or {},
                },
            }
            return {"trace_id": trace_id}

        def count_nodes_by_type(self, namespaces=None):
            out: dict[str, int] = {}
            for (ns, _k), n in env.nodes.items():
                if namespaces is None or ns in set(namespaces):
                    out[n.get("type", "")] = out.get(n.get("type", ""), 0) + 1
            return out

    class FakeIndex:
        def __init__(self, conn, table, dim, default_namespace=""):
            pass

        def table_exists(self):
            return True

        def count_chunks(self, namespaces=()):
            return 7

        def count_for_node(self, node_key, namespace=""):
            return 2

        def texts_for_node(self, node_key, namespace=""):
            return []

        def delete_for_node(self, node_key, namespace=""):
            env.index_deleted.append((namespace, node_key))
            return 2

    monkeypatch.setattr(app_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(app_mod, "run_query", fake_query)
    monkeypatch.setattr(app_mod, "write_and_index_fact", fake_write)
    monkeypatch.setattr(app_mod.dbmod, "connect", lambda s: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    monkeypatch.setattr(app_mod, "VectorIndex", FakeIndex)
    return TestClient(app_mod.app)


# ---------------- гейт и allowlist ----------------


def test_console_disabled_404_everywhere(monkeypatch):
    client = _install(monkeypatch, _Env(), enabled=False)
    for path in (
        "/console",
        "/console/articles",
        "/console/import",
        "/console/ask",
        "/console/search",
        "/console/audit",
    ):
        assert client.get(path).status_code == 404, path
    assert client.post("/console/import", data={"ns": "bank", "content": "x"}).status_code == 404
    assert client.post("/console/ask", data={"ns": "bank", "q": "вопрос"}).status_code == 404


def test_console_disabled_by_default_in_settings():
    from platform_memory.core.config import Settings

    assert Settings.model_fields["console_enabled"].default is False


def test_console_offers_only_allowlisted_namespaces(monkeypatch):
    client = _install(monkeypatch, _Env(), namespaces="bank,mos-card")
    html = client.get("/console").text
    assert '<option value="bank"' in html
    assert '<option value="mos-card"' in html
    assert "nexus" not in html  # дефолтный ns движка не предлагается, если не в allowlist


def test_console_rejects_namespace_outside_allowlist(monkeypatch):
    client = _install(monkeypatch, _Env(), namespaces="bank")
    assert client.get("/console", params={"ns": "other"}).status_code == 400
    assert client.get("/console/articles", params={"ns": "nexus"}).status_code == 400
    resp = client.post("/console/import", data={"ns": "other", "content": "текст"})
    assert resp.status_code == 400
    assert client.post("/console/ask", data={"ns": "other", "q": "вопрос"}).status_code == 400


def test_console_default_ns_is_first_of_allowlist(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env, namespaces="mos-card,bank")
    html = client.get("/console/search", params={"q": "вопрос"}).text
    assert env.read_namespaces[-1] == ["mos-card"]
    assert 'value="mos-card" selected' in html


# ---------------- операции всегда в выбранном namespace ----------------


def test_import_writes_to_selected_namespace_with_audit(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/console/import",
        data={
            "ns": "mos-card",
            "content": "Полный текст статьи.",
            "title": "Статья",
            "external_id": "https://kb.example/a1",
            "source": "kb.example",
        },
    )
    assert resp.status_code == 200
    assert env.write_namespaces == ["mos-card"]
    imports = [a for a in env.audits if a["action"] == "import"]
    assert len(imports) == 1 and imports[0]["namespace"] == "mos-card"
    assert imports[0]["trace_id"]  # correlation id операции есть
    assert imports[0]["trace_id"] in resp.text  # и показан администратору


def test_import_empty_content_400_nothing_written(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post("/console/import", data={"ns": "bank", "content": "   "})
    assert resp.status_code == 400
    assert env.write_namespaces == [] and env.audits == []


def test_import_is_idempotent_by_external_id(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    for _ in range(2):
        client.post(
            "/console/import",
            data={"ns": "bank", "content": "Текст.", "external_id": "https://kb.example/x"},
        )
    keys = [k for (ns, k) in env.nodes if ns == "bank" and not k.startswith("audit:")]
    assert keys == ["https://kb.example/x"]  # один узел, не дубль


def test_articles_listed_from_selected_namespace_only(monkeypatch):
    env = _Env()
    env.nodes[("bank", ARTICLE["natural_key"])] = dict(ARTICLE)
    env.nodes[("mos-card", "kb-2")] = {
        **ARTICLE,
        "natural_key": "kb-2",
        "namespace": "mos-card",
        "title": "Другая статья",
    }
    client = _install(monkeypatch, env)
    html = client.get("/console/articles", params={"ns": "bank"}).text
    assert "Карта москвича" in html
    assert "Другая статья" not in html  # чужая KB не видна


def test_article_view_isolated_by_namespace(monkeypatch):
    env = _Env()
    env.nodes[("bank", ARTICLE["natural_key"])] = dict(ARTICLE)
    client = _install(monkeypatch, env)
    ok = client.get("/console/articles/view", params={"ns": "bank", "key": ARTICLE["natural_key"]})
    assert ok.status_code == 200
    assert "Карта действует 5 лет." in ok.text  # полный оригинал
    other = client.get(
        "/console/articles/view", params={"ns": "mos-card", "key": ARTICLE["natural_key"]}
    )
    assert other.status_code == 404  # из другой KB статья не видна


def test_delete_requires_confirmation(monkeypatch):
    env = _Env()
    env.nodes[("bank", "kb-1")] = {**ARTICLE, "natural_key": "kb-1"}
    client = _install(monkeypatch, env)
    resp = client.post("/console/articles/delete", data={"ns": "bank", "key": "kb-1"})
    assert resp.status_code == 400
    assert ("bank", "kb-1") in env.nodes  # узел не тронут
    assert env.index_deleted == []


def test_delete_confirmed_removes_in_namespace_with_audit(monkeypatch):
    env = _Env()
    env.nodes[("bank", "kb-1")] = {**ARTICLE, "natural_key": "kb-1"}
    client = _install(monkeypatch, env)
    resp = client.post(
        "/console/articles/delete", data={"ns": "bank", "key": "kb-1", "confirm": "yes"}
    )
    assert resp.status_code == 200
    assert ("bank", "kb-1") not in env.nodes
    assert env.index_deleted == [("bank", "kb-1")]
    deletes = [a for a in env.audits if a["action"] == "delete"]
    assert len(deletes) == 1 and deletes[0]["namespace"] == "bank"


def test_search_uses_selected_namespace_and_links_to_source(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    html = client.get("/console/search", params={"ns": "bank", "q": "срок карты"}).text
    assert env.read_namespaces[-1] == ["bank"]
    assert "0.8712" in html  # score виден
    assert "agent:run/r-1" in html  # цитата-источник видна
    assert "/console/articles/view?ns=bank&amp;key=https%3A//kb.example/card" in html


def test_ask_uses_selected_namespace_and_cites_sources(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    html = client.post("/console/ask", data={"ns": "mos-card", "q": "как продлить карту"}).text
    assert env.read_namespaces[-1] == ["mos-card"]
    assert env.questions == ["как продлить карту"]
    assert "Карта действует 5 лет" in html  # синтезированный ответ показан
    assert "agent:run/r-1" in html  # цитата-источник видна
    assert "/console/articles/view?ns=mos-card&amp;key=https%3A//kb.example/card" in html


def test_ask_question_never_accepted_via_url(monkeypatch):
    """Вопрос — только в теле POST: в нём бывают ПДн, в query string (логи) им не место."""
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.get("/console/ask", params={"ns": "bank", "q": "клиент Иванов, тел +7916…"})
    assert resp.status_code == 200  # форма открывается
    assert env.questions == []  # но вопрос из URL движку не передан


def test_ask_without_question_renders_form_only(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post("/console/ask", data={"ns": "bank", "q": "   "})
    assert resp.status_code == 200
    assert env.questions == []  # движок (и LLM) не вызывались


def test_ask_cross_origin_post_rejected(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    resp = client.post(
        "/console/ask",
        data={"ns": "bank", "q": "вопрос"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert env.questions == []  # движок не вызван


def test_ask_engine_error_shown_to_operator_not_500(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)

    def boom(*a, **kw):
        raise RuntimeError("LLM недоступен: connection refused")

    monkeypatch.setattr(app_mod, "run_query", boom)
    resp = client.post("/console/ask", data={"ns": "bank", "q": "вопрос"})
    assert resp.status_code == 200
    assert "LLM недоступен" in resp.text


def test_audit_page_shows_namespace_events(monkeypatch):
    env = _Env()
    client = _install(monkeypatch, env)
    client.post(
        "/console/import",
        data={"ns": "bank", "content": "Текст.", "external_id": "https://kb.example/x"},
    )
    trace = [a for a in env.audits if a["action"] == "import"][0]["trace_id"]
    html = client.get("/console/audit", params={"ns": "bank"}).text
    assert trace in html
    other = client.get("/console/audit", params={"ns": "mos-card"}).text
    assert trace not in other  # аудит-контур другой KB пуст
    assert "Событий аудита в «mos-card» пока нет" in other


# ---------------- безопасность ----------------


def test_api_key_never_in_console_html(monkeypatch):
    env = _Env()
    env.nodes[("bank", ARTICLE["natural_key"])] = dict(ARTICLE)
    client = _install(monkeypatch, env, api_key=SECRET)
    pages = [
        client.get("/console"),
        client.get("/console/articles", params={"ns": "bank"}),
        client.get("/console/articles/view", params={"ns": "bank", "key": ARTICLE["natural_key"]}),
        client.get("/console/import"),
        client.post("/console/ask", data={"ns": "bank", "q": "карта"}),
        client.get("/console/search", params={"ns": "bank", "q": "карта"}),
        client.get("/console/audit"),
    ]
    for resp in pages:
        assert resp.status_code == 200
        assert SECRET not in resp.text
        assert "Bearer" not in resp.text


def test_console_pages_do_not_require_bearer(monkeypatch):
    """Console работает in-process: браузеру не нужен (и не выдаётся) API-ключ."""
    client = _install(monkeypatch, _Env(), api_key=SECRET)
    assert client.get("/console").status_code == 200  # без Authorization
    # а публичный API при этом ключ требует
    assert client.post("/api/brain/recall", json={"query": "q"}).status_code == 401


def test_cross_origin_post_rejected(monkeypatch):
    env = _Env()
    env.nodes[("bank", "kb-1")] = {**ARTICLE, "natural_key": "kb-1"}
    client = _install(monkeypatch, env)
    resp = client.post(
        "/console/articles/delete",
        data={"ns": "bank", "key": "kb-1", "confirm": "yes"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert ("bank", "kb-1") in env.nodes


def test_public_api_unaffected_when_console_disabled(monkeypatch):
    """Регрессия: без Console и её переменных публичный API работает как раньше."""
    env = _Env()
    client = _install(monkeypatch, env, enabled=False)
    resp = client.post("/api/brain/recall", json={"query": "q"})
    assert resp.status_code == 200
