"""Тесты публичной демо-витрины (/demo, ADR-009): гейт, изоляция KB, read-only, утечки.

Без БД: движок подменяется фейками через monkeypatch модуля server.app — витрина ходит
в движок только через него (in-process), поэтому фейки действуют и на неё. Ключевые
контракты: выключена по умолчанию (404), namespace ЖЁСТКО = CB_DEMO_NAMESPACE (клиент
задать не может), только чтение (search + source), API-ключ в HTML не попадает,
публичный /api/brain/* цел, rate-limit срабатывает.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from platform_memory.index.store import SearchHit
from platform_memory.retrieval.query import Source
from platform_memory.server import app as app_mod
from platform_memory.server import demo as demo_mod

pytestmark = pytest.mark.unit

SECRET = "super-secret-api-key-42"

SOURCE_DOC = {
    "natural_key": "https://handbook.acme-internal.example/it/vpn-setup",
    "namespace": "demo",
    "type": "article",
    "title": "VPN setup on a new laptop",
    "content": "Full original article text about setting up the VPN.",
    "content_source": "original",
    "chunks": 3,
    "provenance": {
        "source": "IT Handbook / Networking",
        "source_path": "https://handbook.acme-internal.example/it/vpn-setup",
        "origin": "agent",
        "actor": "kb-import",
        "actor_id": "secret-actor-id-should-not-leak",
        "trace_id": "trace-should-not-leak",
        "issue_id": "issue-should-not-leak",
        "confidence": 0.9,
        "last_seen": "2026-07-20T00:00:00",
        "metadata": {"internal": "should-not-leak"},
    },
    "pii": False,
    "pii_categories": [],
}


def _install(
    monkeypatch,
    env: SimpleNamespace | None = None,
    *,
    enabled: bool = True,
    namespace: str = "demo",
    rate_limit: int = 1000,
    api_key: str = "",
    pii_protection: bool = False,
):
    env = env or SimpleNamespace(read_namespaces=[], source_calls=[])
    settings = app_mod.get_settings().model_copy(
        update={
            "demo_public_enabled": enabled,
            "demo_namespace": namespace,
            "demo_rate_limit": rate_limit,
            "demo_search_k": 5,
            "console_enabled": False,
            "server_api_key": api_key,
            "server_api_keys_pii": "",
            "pii_protection": pii_protection,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    def fake_retrieve(_settings, q, k=8, hops=1, namespaces=(), **kw):
        env.read_namespaces.append(list(namespaces))
        hit = SearchHit(
            chunk_id=1,
            node_key="https://handbook.acme-internal.example/it/vpn-setup",
            source_path="https://handbook.acme-internal.example/it/vpn-setup",
            title="VPN setup on a new laptop",
            heading="Networking",
            text="Connect to the corporate VPN using the client provisioned on your laptop.",
            score=0.8421,
        )
        source = Source(
            source_path="https://handbook.acme-internal.example/it/vpn-setup",
            node_key="https://handbook.acme-internal.example/it/vpn-setup",
            title="VPN setup on a new laptop",
        )
        return SimpleNamespace(hits=[hit], neighbors=[], sources=[source], context="")

    def fake_brain_source(natural_key, namespace="", principal=None, x_run_id=None):
        env.source_calls.append({"key": natural_key, "namespace": namespace})
        if namespace != namespace_of_doc:
            raise HTTPException(status_code=404, detail="Источник не найден")
        if natural_key != SOURCE_DOC["natural_key"]:
            raise HTTPException(status_code=404, detail="Источник не найден")
        return dict(SOURCE_DOC)

    namespace_of_doc = "demo"
    monkeypatch.setattr(app_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(app_mod, "brain_source", fake_brain_source)
    # Rate-limit хранит состояние в модуле — чистим между тестами (общий IP testclient).
    demo_mod._rate_hits.clear()
    return TestClient(app_mod.app), env


# ---------------- гейт (выключена по умолчанию) ----------------


def test_demo_disabled_by_default_in_settings():
    from platform_memory.core.config import Settings

    assert Settings.model_fields["demo_public_enabled"].default is False


def test_demo_disabled_404_everywhere(monkeypatch):
    client, _ = _install(monkeypatch, enabled=False)
    assert client.get("/").status_code == 404
    assert client.get("/demo").status_code == 404
    assert client.post("/demo/api/search", json={"query": "vpn"}).status_code == 404
    assert client.get("/demo/api/source", params={"key": "x"}).status_code == 404


def test_public_api_unaffected_when_demo_disabled(monkeypatch):
    """Регрессия: без витрины и её переменных публичный API работает как раньше."""
    client, _ = _install(monkeypatch, enabled=False)
    # ключ не задан -> локальный режим, recall доступен
    assert client.post("/api/brain/recall", json={"query": "vpn"}).status_code == 200


# ---------------- страница ----------------


def test_demo_page_renders_brand_and_mode(monkeypatch):
    client, _ = _install(monkeypatch)
    resp = client.get("/demo")
    assert resp.status_code == 200
    assert "Verifiable Memory" in resp.text  # нейтральный бренд по умолчанию
    assert '"mode": "live"' in resp.text or '"mode":"live"' in resp.text


def test_root_redirects_to_demo_when_enabled(monkeypatch):
    client, _ = _install(monkeypatch)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/demo"


# ---------------- изоляция KB: клиент не задаёт namespace ----------------


def test_search_forces_demo_namespace(monkeypatch):
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env, namespace="demo")
    resp = client.post("/demo/api/search", json={"query": "vpn setup"})
    assert resp.status_code == 200
    assert env.read_namespaces == [["demo"]]  # ровно демо-KB
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["score"] == 0.8421
    assert body["results"][0]["source_path"].startswith("https://handbook.acme-internal.example")


def test_search_ignores_client_supplied_namespace(monkeypatch):
    """Клиент не может выйти за демо-KB: лишние поля (namespace/scope) игнорируются."""
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env, namespace="demo")
    resp = client.post(
        "/demo/api/search",
        json={"query": "vpn", "namespace": "bank", "scope": {"namespace": "secret"}},
    )
    assert resp.status_code == 200
    assert env.read_namespaces == [["demo"]]  # чужой namespace не просочился


def test_search_respects_custom_demo_namespace(monkeypatch):
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env, namespace="sea-demo")
    client.post("/demo/api/search", json={"query": "vpn"})
    assert env.read_namespaces == [["sea-demo"]]


def test_search_empty_query_returns_empty(monkeypatch):
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env)
    resp = client.post("/demo/api/search", json={"query": "   "})
    assert resp.status_code == 200
    assert resp.json() == {"query": "", "count": 0, "results": [], "sources": []}
    assert env.read_namespaces == []  # движок не дёргали


def test_search_passes_rerank_and_surfaces_rerank_score(monkeypatch):
    """Витрина реранкует свой поиск (rerank=demo_rerank) и отдаёт настоящую 0..1 уверенность."""
    env = SimpleNamespace(read_namespaces=[], source_calls=[], rerank_flags=[])
    client, env = _install(monkeypatch, env)

    def reranked_retrieve(_settings, q, k=8, hops=1, namespaces=(), rerank=None, **kw):
        env.read_namespaces.append(list(namespaces))
        env.rerank_flags.append(rerank)
        hit = SearchHit(
            chunk_id=1,
            node_key="https://handbook.acme-internal.example/x",
            source_path="https://handbook.acme-internal.example/x",
            title="X",
            heading="",
            text="body",
            score=0.03,
        )
        hit.rerank_score = 0.92
        return SimpleNamespace(hits=[hit], neighbors=[], sources=[], context="")

    monkeypatch.setattr(app_mod, "retrieve", reranked_retrieve)
    body = client.post("/demo/api/search", json={"query": "vpn"}).json()
    assert env.rerank_flags == [True]  # demo_rerank по умолчанию True
    assert body["reranked"] is True
    assert body["results"][0]["rerank_score"] == 0.92


def test_search_includes_concept_subgraph(monkeypatch):
    """Витрина отдаёт подграф отбора (seed → концепты → связанные) — граф, не плоский RAG."""
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env)

    class FakeConn:
        def close(self):
            pass

    class FakeGraph:
        def __init__(self, *a, **k):
            pass

        def neighborhood_subgraph(self, seed_keys, namespaces=(), hops=1, per_seed=10):
            return {
                "nodes": [
                    {
                        "natural_key": seed_keys[0],
                        "type": "article",
                        "title": "VPN",
                        "role": "seed",
                        "hop": 0,
                    },
                    {
                        "natural_key": "person:elena",
                        "type": "person",
                        "title": "Elena",
                        "role": "neighbor",
                        "hop": 1,
                    },
                    {
                        "natural_key": "concept:authentication",
                        "type": "concept",
                        "title": "Authentication",
                        "role": "neighbor",
                        "hop": 1,
                    },
                ],
                "edges": [
                    {"src": seed_keys[0], "dst": "concept:authentication", "type": "MENTIONS"},
                    {"src": "person:elena", "dst": "concept:authentication", "type": "EXPERT_IN"},
                ],
            }

    monkeypatch.setattr(app_mod.dbmod, "connect", lambda s: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    body = client.post("/demo/api/search", json={"query": "vpn"}).json()
    g = body["graph"]
    assert any(n["type"] == "person" for n in g["nodes"])  # окрестность включает людей
    assert any(n["role"] == "seed" for n in g["nodes"])
    assert all("type" in e for e in g["edges"])  # рёбра типизированы


# ---------------- наглядность конвейера + время движка («почти realtime») ----------------


def test_search_returns_pipeline_stages_and_timing(monkeypatch):
    """Витрина отдаёт этапы конвейера с РЕАЛЬНЫМИ счётчиками (из r.stats) и время движка."""
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env)

    def retrieve_with_stats(_settings, q, k=8, hops=1, namespaces=(), rerank=None, **kw):
        env.read_namespaces.append(list(namespaces))
        hit = SearchHit(
            chunk_id=1,
            node_key="k1",
            source_path="p1",
            title="T1",
            heading="",
            text="body",
            score=0.02,
        )
        hit.rerank_score = 0.88
        return SimpleNamespace(
            hits=[hit],
            neighbors=[],
            sources=[],
            context="",
            stats={
                "vector_candidates": 12,
                "neighbors": 7,
                "pool": 18,
                "reranked": True,
                "final": 1,
            },
        )

    monkeypatch.setattr(app_mod, "retrieve", retrieve_with_stats)
    body = client.post("/demo/api/search", json={"query": "vpn"}).json()
    assert isinstance(body["took_ms"], int) and body["took_ms"] >= 0
    stages = body["pipeline"]
    assert [s["key"] for s in stages] == ["search", "graph", "rerank", "gate"]
    assert stages[0]["count"] == 12  # реальные кандидаты вектор+FTS из stats
    rerank = next(s for s in stages if s["key"] == "rerank")
    assert rerank["count"] == 18 and rerank["confidence"] == 0.88  # пул и настоящая уверенность
    gate = next(s for s in stages if s["key"] == "gate")
    assert gate["count"] == 1 and gate["dropped"] == 0  # прошло порог


def test_search_pipeline_degrades_without_stats(monkeypatch):
    """Ретрив без .stats (старый контракт): конвейер всё равно строится — мягкая деградация."""
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env)  # дефолтный fake_retrieve без stats и без rerank
    body = client.post("/demo/api/search", json={"query": "vpn"}).json()
    stages = body["pipeline"]
    assert stages and stages[0]["key"] == "search"
    assert stages[0]["count"] == 1  # fallback: длина hits, когда stats нет
    assert [s["key"] for s in stages][-1] == "rank"  # без реранка — стадия «Ranked»


def test_answer_forces_namespace_returns_text_and_timing(monkeypatch):
    """Финальный ответ: namespace форсирован, отдаётся текст (markdown), источники и время."""
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env)

    def fake_run_query(
        _settings, q, k=8, hops=1, namespaces=(), rerank=None, system=None, max_tokens=None, **kw
    ):
        env.read_namespaces.append(list(namespaces))
        return SimpleNamespace(
            text="- Point one\n- Point two",
            sources=[Source("p1", "k1", "T1")],
            context="",
            hits=1,
            neighbors=0,
        )

    monkeypatch.setattr(app_mod, "run_query", fake_run_query)
    body = client.post("/demo/api/answer", json={"query": "summarize"}).json()
    assert body["answer"].startswith("- Point one")  # markdown-текст доходит без искажения
    assert isinstance(body["took_ms"], int)
    assert env.read_namespaces == [["demo"]]  # namespace форсирован (клиент не задаёт)
    assert body["sources"][0]["node_key"] == "k1"


def test_answer_empty_query_returns_empty(monkeypatch):
    client, _ = _install(monkeypatch)
    body = client.post("/demo/api/answer", json={"query": "  "}).json()
    assert body == {"query": "", "answer": "", "sources": []}


# ---------------- source-view: forced namespace, curated, isolation ----------------


def test_source_forces_demo_namespace_and_curates_fields(monkeypatch):
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env, namespace="demo")
    resp = client.get("/demo/api/source", params={"key": SOURCE_DOC["natural_key"]})
    assert resp.status_code == 200
    assert env.source_calls[-1]["namespace"] == "demo"  # namespace форсирован
    body = resp.json()
    assert body["content"] == SOURCE_DOC["content"]  # полный оригинал
    assert body["source_path"].startswith("https://handbook.acme-internal.example")
    assert body["confidence"] == 0.9
    # служебные поля provenance наружу НЕ отдаются
    raw = resp.text
    assert "should-not-leak" not in raw
    assert "actor_id" not in body and "trace_id" not in body and "metadata" not in body


def test_source_not_found_is_404(monkeypatch):
    client, _ = _install(monkeypatch)
    resp = client.get(
        "/demo/api/source", params={"key": "https://handbook.acme-internal.example/nope"}
    )
    assert resp.status_code == 404


# ---------------- read-only: запись/чужие маршруты недоступны с витрины ----------------


def test_demo_exposes_no_write_routes(monkeypatch):
    """С витрины нет ни одного пути записи/удаления — только search и source."""
    client, _ = _install(monkeypatch)
    assert client.post("/demo/api/retain", json={"content": "x"}).status_code == 404
    assert client.delete("/demo/api/source", params={"key": "x"}).status_code in (404, 405)
    assert client.post("/demo/api/source", params={"key": "x"}).status_code in (404, 405)


# ---------------- безопасность: ключ не в HTML ----------------


def test_api_key_never_in_demo_html(monkeypatch):
    client, _ = _install(monkeypatch, api_key=SECRET)
    resp = client.get("/demo")
    assert resp.status_code == 200
    assert SECRET not in resp.text
    assert "Bearer" not in resp.text


def test_demo_pages_do_not_require_bearer_but_api_does(monkeypatch):
    """Витрина работает in-process: браузеру ключ не нужен; публичный API — требует."""
    client, _ = _install(monkeypatch, api_key=SECRET)
    assert client.get("/demo").status_code == 200  # без Authorization
    assert client.post("/demo/api/search", json={"query": "vpn"}).status_code == 200
    # а публичный API при этом ключ требует
    assert client.post("/api/brain/recall", json={"query": "q"}).status_code == 401


# ---------------- ошибки движка не текут на публичную поверхность ----------------


def test_search_engine_error_is_generic_503(monkeypatch):
    client, _ = _install(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("psql: connection refused at postgresql://brain:secret@db:5432")

    monkeypatch.setattr(app_mod, "retrieve", boom)
    resp = client.post("/demo/api/search", json={"query": "vpn"})
    assert resp.status_code == 503
    assert "connection refused" not in resp.text
    assert "postgresql" not in resp.text


# ---------------- PII: витрина маскирует ВСЕГДА (masked-принципал) ----------------


def _retrieve_with_pii(env):
    def with_pii(_settings, q, k=8, hops=1, namespaces=(), **kw):
        env.read_namespaces.append(list(namespaces))
        hit = SearchHit(
            chunk_id=1,
            node_key="https://handbook.acme-internal.example/x",
            source_path="https://handbook.acme-internal.example/x",
            title="Contact",
            heading="",
            text="Call the desk at +7 916 123-45-67 for help.",
            score=0.5,
        )
        return SimpleNamespace(hits=[hit], neighbors=[], sources=[], context="")

    return with_pii


def test_search_masks_pii_when_protection_on(monkeypatch):
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env, pii_protection=True)
    monkeypatch.setattr(app_mod, "retrieve", _retrieve_with_pii(env))
    resp = client.post("/demo/api/search", json={"query": "contact"})
    assert resp.status_code == 200
    assert resp.json().get("pii_masked") is True
    assert "916" not in resp.text  # телефон замаскирован


def test_search_masks_pii_even_when_protection_off(monkeypatch):
    """Витрина форсирует masked-принципал: ПДн маскируются даже при CB_PII_PROTECTION=false
    (иначе «всегда маскированная выдача» — пустое обещание на публичной поверхности)."""
    env = SimpleNamespace(read_namespaces=[], source_calls=[])
    client, env = _install(monkeypatch, env, pii_protection=False)  # дефолт
    monkeypatch.setattr(app_mod, "retrieve", _retrieve_with_pii(env))
    resp = client.post("/demo/api/search", json={"query": "contact"})
    assert resp.status_code == 200
    assert resp.json().get("pii_masked") is True
    assert "916" not in resp.text  # телефон замаскирован, флаг выключен


# ---------------- rate-limit ----------------


def test_rate_limit_blocks_after_threshold(monkeypatch):
    client, _ = _install(monkeypatch, rate_limit=2)
    assert client.post("/demo/api/search", json={"query": "a"}).status_code == 200
    assert client.post("/demo/api/search", json={"query": "b"}).status_code == 200
    resp = client.post("/demo/api/search", json={"query": "c"})
    assert resp.status_code == 429


def test_rate_limit_zero_means_unlimited(monkeypatch):
    client, _ = _install(monkeypatch, rate_limit=0)
    for _ in range(5):
        assert client.post("/demo/api/search", json={"query": "a"}).status_code == 200


def test_rate_limit_not_bypassed_by_spoofed_xff(monkeypatch):
    """Подделка левого хопа X-Forwarded-For НЕ обходит лимит: доверенный прокси дописывает
    реальный peer справа, ключуемся по нему."""
    client, _ = _install(monkeypatch, rate_limit=2)
    # Атакующий подделывает начало цепочки; Traefik дописывает реальный peer '9.9.9.9'.
    for i, expected in enumerate((200, 200, 429)):
        resp = client.post(
            "/demo/api/search",
            json={"query": "x"},
            headers={"X-Forwarded-For": f"{i}.{i}.{i}.{i}, 9.9.9.9"},
        )
        assert resp.status_code == expected


def test_rate_limit_is_per_real_client(monkeypatch):
    """Разные реальные клиенты (правый хоп) — независимые корзины."""
    client, _ = _install(monkeypatch, rate_limit=1)
    a1 = client.post(
        "/demo/api/search", json={"query": "x"}, headers={"X-Forwarded-For": "f, 1.1.1.1"}
    )
    a2 = client.post(
        "/demo/api/search", json={"query": "x"}, headers={"X-Forwarded-For": "g, 1.1.1.1"}
    )
    b1 = client.post(
        "/demo/api/search", json={"query": "x"}, headers={"X-Forwarded-For": "h, 2.2.2.2"}
    )
    assert (a1.status_code, a2.status_code, b1.status_code) == (200, 429, 200)
