"""E2E-сценарий Definition of Done (ADR-016, ТЗ §50) на реальной БД.

Независимое приложение, без каких-либо product-зависимостей и без LLM
(fake/echo-провайдеры), проходит весь цикл: observations → documents →
temporal projection → lexical/semantic/graph/hybrid retrieval →
build_context с ephemeral и бюджетом → provenance → trace. Плюс тот же
цикл через HTTP (/api/memory/*).
"""

from __future__ import annotations

import pytest

from platform_memory import (
    build_context,
    delete_observation,
    retain_observation,
    retain_observations,
    write_and_index_fact,
)

pytestmark = pytest.mark.integration

NS = "e2e"
UUID_TOKEN = "8f14e45f-ceea-4b16-8d5a-9a0c8f1e2b3c"


@pytest.fixture()
def seeded(settings, stores):
    """Наполнить память: structured/unstructured observations + документ."""
    # 1. Structured observation: сущности + temporal-факт + text-assertion.
    r1 = retain_observation(
        settings,
        {
            "source": {"system": "issue-tracker", "stream": "events", "external_id": "ev-1"},
            "kind": "work.completed",
            "occurred_at": "2026-08-01T10:00:00Z",
            "actor": {"type": "principal", "id": "alice"},
            "scopes": ["project:alpha"],
            "content": f"Alice починила регрессию {UUID_TOKEN} в деплое",
            "provenance": {"uri": "https://tracker.local/ev-1"},
            "assertions": [
                {"assert": "entity", "entity": {"type": "person", "id": "alice", "title": "Alice"}},
                {
                    "assert": "entity",
                    "entity": {"type": "project", "id": "alpha", "title": "Проект Альфа"},
                },
                {
                    "assert": "fact",
                    "fact": {
                        "subject": "person:alice",
                        "predicate": "WORKS_ON",
                        "object": "project:alpha",
                        "valid_from": "2026-08-01T00:00:00Z",
                    },
                },
                {
                    "assert": "text",
                    "text": {
                        "content": "Регрессия в деплое исправлена перезапуском мигратора",
                        "title": "Отчёт о фиксе",
                    },
                },
            ],
        },
        namespace=NS,
    )
    assert r1 == {"observation_id": r1["observation_id"], "duplicate": False, "status": "processed"}

    # 2. Unstructured observation (без assertions) — просто свидетельство.
    r2 = retain_observation(
        settings,
        {
            "kind": "meeting.note",
            "occurred_at": "2026-08-10T09:00:00Z",
            "scopes": ["project:alpha"],
            "content": "Обсудили деплой: решили катить в пятницу после фикса",
        },
        namespace=NS,
    )
    assert r2["status"] == "processed"

    # 3. Документ (существующий write-путь) — vault-подобный источник.
    write_and_index_fact(
        settings,
        "doc:deploy-guide",
        "product_doc",
        "Инструкция по деплою",
        text="Деплой выполняется через мигратор; при регрессии перезапустить мигратор",
        namespace=NS,
    )
    return r1, r2


def test_dod_full_cycle_python_api(settings, stores, seeded):
    r1, r2 = seeded

    # (1-2) Идемпотентность повторной доставки батчем.
    batch = retain_observations(
        settings,
        [
            {
                "source": {"system": "issue-tracker", "stream": "events", "external_id": "ev-1"},
                "content": "повторная доставка того же события",
            },
        ],
        namespace=NS,
    )
    assert batch["duplicates"] == 1 and batch["accepted"] == 0

    # (3) Temporal graph projection.
    from platform_memory import FactStore, GraphStore
    from platform_memory.core import db as dbmod

    conn = dbmod.connect(settings, autocommit=True)
    try:
        facts = FactStore(GraphStore(conn, settings.graph_name, settings.default_namespace))
        current = facts.facts(subject="person:alice", namespaces=[NS])
        assert current and current[0]["object"] == "project:alpha"
        assert current[0]["observation_ids"] == [r1["observation_id"]]
    finally:
        conn.close()

    # (4-8) build_context: hybrid = lexical (точный UUID) + vector + graph + recent.
    pack = build_context(
        settings,
        {
            "query": f"Что случилось с деплоем {UUID_TOKEN}?",
            "scopes": ["project:alpha"],
            "anchors": ["person:alice"],
            "ephemeralContext": {"current_state": "deploy pending"},  # (9)
            "budget": {"tokens": 4000},  # (10)
            "namespaces": [NS],
        },
    )

    sections = {s.kind: s for s in pack.sections}
    # (9) ephemeral включён первым и не потерян.
    assert sections["current"].items[0].id == "current:ephemeral"
    assert "deploy pending" in sections["current"].items[0].text
    # (4) lexical нашёл точный идентификатор — наблюдение с UUID в выдаче.
    obs_ids = [i.id for i in sections["recent_observations"].items]
    assert r1["observation_id"] in obs_ids
    uuid_item = next(
        i for i in sections["recent_observations"].items if i.id == r1["observation_id"]
    )
    assert uuid_item.signals.get("identifier_match") is True
    # (5) semantic: документ про мигратор найден вектором/FTS.
    doc_texts = " ".join(i.text for i in sections["documents"].items)
    assert "мигратор" in doc_texts
    # (6) graph: сущности вокруг якоря person:alice.
    entity_ids = {i.id for i in sections["related_entities"].items}
    assert "node:project:alpha" in entity_ids
    # (7) факты в выдаче с temporal-полями.
    fact_items = sections["relevant_facts"].items
    assert fact_items and fact_items[0].signals.get("valid_open") is True
    # (10) бюджет соблюдён.
    assert pack.token_estimate <= 4000
    # (11) provenance каждого significant result.
    assert uuid_item.provenance["observation_id"] == r1["observation_id"]
    assert fact_items[0].provenance["observation_ids"] == [r1["observation_id"]]
    for item in sections["documents"].items:
        assert item.provenance.get("node_key")
    assert pack.sources  # цитаты-источники присутствуют

    # (12) trace восстановим.
    from platform_memory.context.trace import ContextTraceStore

    conn = dbmod.connect(settings, autocommit=True)
    try:
        store = ContextTraceStore(conn, settings.context_traces_table, settings.default_namespace)
        trace = store.get(pack.trace_id, namespaces=[NS])
    finally:
        conn.close()
    assert trace is not None
    assert trace["request"]["query"].startswith("Что случилось")
    assert trace["decisions"]["stats"]["candidates"] > 0
    assert trace["decisions"]["ranking"]["rrf_k"] == 60
    # ephemeral-содержимое в trace НЕ сохранено.
    assert "deploy pending" not in str(trace)


def test_scope_filtering_excludes_foreign_scope(settings, stores, seeded):
    # Наблюдение в чужом scope не попадает в выдачу с фильтром project:beta,
    # а элементы без scopes (документ) остаются видимыми.
    pack = build_context(
        settings,
        {"query": "деплой", "scopes": ["project:beta"], "namespaces": [NS]},
    )
    sections = {s.kind: s for s in pack.sections}
    assert sections["recent_observations"].items == []
    assert sections["documents"].items  # общая память KB видна


def test_namespace_isolation_e2e(settings, stores, seeded):
    pack = build_context(settings, {"query": "деплой регрессия", "namespaces": ["other"]})
    assert all(not s.items for s in pack.sections if s.kind != "current")


def test_deletion_cascade(settings, stores, seeded):
    r1, _r2 = seeded
    oid = r1["observation_id"]
    result = delete_observation(settings, oid, namespace=NS, mode="redact")
    assert result["chunks_deleted"] == 1  # text-assertion чанк удалён
    assert result["facts_evidence_lost"] == 1  # факт остался без свидетельств

    # Наблюдение redacted: содержимое зачищено, запись жива.
    from platform_memory.core import db as dbmod
    from platform_memory.observations.store import ObservationStore

    conn = dbmod.connect(settings, autocommit=True)
    try:
        store = ObservationStore(conn, settings.observations_table, settings.default_namespace)
        rec = store.get(oid, namespaces=[NS])
    finally:
        conn.close()
    assert rec is not None and rec.status == "redacted" and rec.content == ""

    # Derived-факт выпал из retrieval (dangling truth исключён).
    pack = build_context(
        settings, {"query": "деплой", "anchors": ["person:alice"], "namespaces": [NS]}
    )
    sections = {s.kind: s for s in pack.sections}
    assert sections["relevant_facts"].items == []


def test_dod_full_cycle_http(settings, stores, monkeypatch):
    """Тот же цикл через HTTP: observations → context → trace (реальный движок)."""
    import platform_memory.server.app as app_mod
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    client = TestClient(app_mod.app)

    resp = client.post(
        "/api/memory/observations",
        json={
            "source": {"system": "crm", "stream": "events", "external_id": "c-1"},
            "kind": "customer.refund_requested",
            "content": f"Клиент попросил возврат по заказу {UUID_TOKEN}",
            "scopes": ["customer:acme"],
            "scope": {"namespace": NS},
        },
    )
    assert resp.status_code == 201, resp.text
    oid = resp.json()["observation_id"]
    assert resp.json()["duplicate"] is False

    # Повтор — duplicate.
    again = client.post(
        "/api/memory/observations",
        json={
            "source": {"system": "crm", "stream": "events", "external_id": "c-1"},
            "content": "повтор",
            "scope": {"namespace": NS},
        },
    )
    assert again.json()["duplicate"] is True

    ctx = client.post(
        "/api/memory/context",
        json={
            "query": f"возврат {UUID_TOKEN}",
            "scopes": ["customer:acme"],
            "budget": {"tokens": 2000},
            "scope": {"namespace": NS},
        },
    )
    assert ctx.status_code == 200, ctx.text
    body = ctx.json()
    obs_section = next(s for s in body["sections"] if s["kind"] == "recent_observations")
    assert any(i["id"] == oid for i in obs_section["items"])
    assert body["token_estimate"] <= 2000

    trace = client.get(f"/api/memory/context/trace/{body['trace_id']}?namespace={NS}")
    assert trace.status_code == 200
    assert trace.json()["decisions"]["stats"]["candidates"] >= 1

    got = client.get(f"/api/memory/observations/{oid}?namespace={NS}")
    assert got.status_code == 200
    assert got.json()["kind"] == "customer.refund_requested"

    deleted = client.delete(f"/api/memory/observations/{oid}?namespace={NS}&mode=purge")
    assert deleted.status_code == 200
    assert client.get(f"/api/memory/observations/{oid}?namespace={NS}").status_code == 404
