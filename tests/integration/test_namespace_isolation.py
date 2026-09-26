"""Интеграционные тесты изоляции памяти по namespaces (ADR-003/017).

Проверяют контракт мультитенантности: retain в namespace A не виден чтению,
скоупленному в B; общая память (`shared`) читается поверх собственной; natural_key
не коллизируют между namespaces; ре-ingest vault не подметает тенантские данные;
backfill схемы идемпотентен; вызовы без scope работают в default namespace
(бит-совместимость с существующим потребителем Nexus).
"""

from __future__ import annotations

import pytest

from platform_memory.core import db as dbmod
from platform_memory.graph import GraphStore
from platform_memory.index import VectorIndex
from platform_memory.ingest import ingest_vault

pytestmark = pytest.mark.integration

NS_A = "tp:tenant:aaaa:team:t1"
NS_B = "tp:tenant:bbbb:team:t2"
SHARED = "shared"


@pytest.fixture
def appmod(test_settings, clean_db, monkeypatch):
    """Модуль сервера с настройками теста (вызов хендлеров напрямую, без сети)."""
    import platform_memory.server.app as mod

    monkeypatch.setattr(mod, "get_settings", lambda: test_settings)
    return mod


def _retain(appmod, content: str, namespace: str, external_id: str | None = None):
    return appmod.brain_retain(
        appmod.RetainRequest(
            content=content,
            type="note",
            external_id=external_id,
            scope={"namespace": namespace} if namespace else None,
        )
    )


def _recall(appmod, query: str, namespaces: list[str]):
    return appmod.brain_recall(appmod.RecallRequest(query=query, scope={"namespaces": namespaces}))


def test_retain_in_a_invisible_in_b(appmod):
    _retain(appmod, "Секретный факт тенанта А про лицензии", NS_A)
    in_b = _recall(appmod, "лицензии", [NS_B])
    assert in_b["count"] == 0, "данные тенанта A не должны быть видны в scope тенанта B"
    in_a = _recall(appmod, "лицензии", [NS_A])
    assert in_a["count"] >= 1
    assert all(m["namespace"] == NS_A for m in in_a["memories"])


def test_shared_namespace_visible_on_top_of_own(appmod):
    _retain(appmod, "Общий регламент платформы по заявкам", SHARED)
    _retain(appmod, "Внутренняя заметка тенанта А о заявках", NS_A)
    combined = _recall(appmod, "заявки", [NS_A, SHARED])
    namespaces = {m["namespace"] for m in combined["memories"]}
    assert namespaces == {NS_A, SHARED}
    only_b = _recall(appmod, "заявки", [NS_B, SHARED])
    assert {m["namespace"] for m in only_b["memories"]} == {SHARED}


def test_natural_key_no_collision_across_namespaces(appmod, test_settings):
    _retain(appmod, "Проект Альфа тенанта А", NS_A, external_id="project:alpha")
    _retain(appmod, "Проект Альфа тенанта Б — другой", NS_B, external_id="project:alpha")
    conn = dbmod.connect(test_settings, autocommit=True)
    try:
        graph = GraphStore(conn, test_settings.graph_name, test_settings.default_namespace)
        node_a = graph.get_node("project:alpha", namespaces=[NS_A])
        node_b = graph.get_node("project:alpha", namespaces=[NS_B])
    finally:
        conn.close()
    assert node_a is not None and node_b is not None
    assert node_a["title"] != node_b["title"], "узлы с одним natural_key живут независимо"


def test_default_namespace_backward_compatible(appmod):
    """Запросы без scope работают в default namespace (существующий потребитель Nexus)."""
    res = appmod.brain_retain(appmod.RetainRequest(content="Факт без scope"))
    assert res["namespace"] == appmod.get_settings().default_namespace
    recall = appmod.brain_recall(appmod.RecallRequest(query="факт без scope"))
    assert recall["count"] >= 1
    assert {m["namespace"] for m in recall["memories"]} == {appmod.get_settings().default_namespace}


def test_vault_reingest_does_not_sweep_tenant_data(appmod, test_settings):
    """Mark-and-sweep vault-ingest скоуплен default namespace: тенантские чанки живут."""
    ingest_vault(test_settings, test_settings.vault_path, reset=True)
    _retain(appmod, "Тенантский факт, который обязан пережить ре-ingest", NS_A)
    ingest_vault(test_settings, test_settings.vault_path)  # повторный прогон с prune
    survived = _recall(appmod, "пережить ре-ingest", [NS_A])
    assert survived["count"] >= 1


def test_ensure_schema_backfill_idempotent(test_settings, clean_db):
    """Повторный ensure_schema на существующих данных не падает и не меняет namespace."""
    conn = dbmod.connect(test_settings, autocommit=True)
    try:
        graph = GraphStore(conn, test_settings.graph_name, test_settings.default_namespace)
        index = VectorIndex(
            conn,
            test_settings.chunks_table,
            test_settings.embedding_dim,
            test_settings.default_namespace,
        )
        graph.ensure_schema()
        index.ensure_schema()
        graph.write_fact("fact:backfill", "note", "Факт до повторного ensure_schema")
        graph.ensure_schema()  # повторный запуск (backfill идемпотентен)
        index.ensure_schema()
        node = graph.get_node("fact:backfill")
        assert node is not None
        assert node["namespace"] == test_settings.default_namespace
    finally:
        conn.close()


def test_community_marking_and_members_scoped_to_namespace(appmod, test_settings):
    """Разметка сообществ и community_members не пересекают namespaces (ADR-003/017)."""
    _retain(appmod, "Факт про биллинг в default namespace", "")  # default ns
    _retain(appmod, "Факт тенанта А про биллинг", NS_A, external_id="fact:billing")
    conn = dbmod.connect(test_settings, autocommit=True)
    try:
        graph = GraphStore(conn, test_settings.graph_name, test_settings.default_namespace)
        default_ns = test_settings.default_namespace
        # Ключи узлов default-ns: возьмём их из графа.
        default_keys = [
            str(n["natural_key"]) for n in graph.list_nodes(limit=50) if n.get("natural_key")
        ]
        assert default_keys, "в default namespace должен быть хотя бы один узел"
        # Разметка (default ns) + попытка задеть узел тенанта по его natural_key.
        graph.set_node_communities(dict.fromkeys([*default_keys, "fact:billing"], 7))
        members = graph.community_members(7)  # default ns
        member_ns = {str(m.get("namespace")) for m in members}
        assert member_ns == {default_ns}, "community_members не должен отдавать чужие namespaces"
        tenant_node = graph.get_node("fact:billing", namespaces=[NS_A])
        assert tenant_node is not None
        assert tenant_node.get("community") is None, (
            "разметка сообществ default namespace не должна задевать узлы тенанта"
        )
        assert graph.community_members(7, namespaces=[NS_A]) == []
    finally:
        conn.close()


def test_audit_trace_scoped_to_namespace(appmod, test_settings):
    # В сервисе схему создаёт lifespan; при прямом вызове хендлера — создаём явно.
    conn = dbmod.connect(test_settings, autocommit=True)
    try:
        GraphStore(conn, test_settings.graph_name, test_settings.default_namespace).ensure_schema()
    finally:
        conn.close()
    appmod.brain_audit(
        appmod.AuditRequest(
            trace_id="tr-1",
            action="checkout",
            actor="agent-a",
            scope={"namespace": NS_A},
        )
    )
    in_a = appmod.brain_trace("tr-1", namespace=NS_A)
    assert in_a["event_count"] == 1
    in_b = appmod.brain_trace("tr-1", namespace=NS_B)
    assert in_b["event_count"] == 0
