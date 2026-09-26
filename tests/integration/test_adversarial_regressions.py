"""Регрессии adversarial review (ADR-016, ТЗ §45): confirmed defect -> fix -> test."""

from __future__ import annotations

import pytest

from platform_memory import build_context, delete_observation, retain_observation

pytestmark = pytest.mark.integration

NS = "nexus"


def _text_observation(settings, secret: str):
    return retain_observation(
        settings,
        {
            "kind": "note",
            "content": f"наблюдение с {secret}",
            "scopes": ["project:alpha"],
            "assertions": [
                {"assert": "text", "text": {"content": f"{secret} в отчёте", "title": "N"}}
            ],
        },
        namespace=NS,
    )


def test_redact_scrubs_derived_node_content(settings, stores):
    """Дефект №4: контент наблюдения не должен пережить redact в props узла."""
    from platform_memory.core import db as dbmod
    from platform_memory.graph.store import GraphStore

    secret = "СЕКРЕТ-XYZZY"
    r = _text_observation(settings, secret)
    assert r["status"] == "processed"
    result = delete_observation(settings, r["observation_id"], namespace=NS, mode="redact")
    assert result["nodes_scrubbed"] == 1

    conn = dbmod.connect(settings, autocommit=True)
    try:
        node = GraphStore(conn, settings.graph_name, NS).get_node(
            f"{r['observation_id']}:text:0", namespaces=[NS]
        )
    finally:
        conn.close()
    assert node is not None  # redact сохраняет узел (аудит-след)
    assert secret not in str(node)
    assert node["props"].get("redacted") is True

    # И через build_context утечки нет.
    pack = build_context(
        settings,
        {"query": "отчёт", "anchors": [f"{r['observation_id']}:text:0"], "namespaces": [NS]},
    )
    assert all(secret not in i.text for s in pack.sections for i in s.items)


def test_purge_removes_derived_node(settings, stores):
    from platform_memory.core import db as dbmod
    from platform_memory.graph.store import GraphStore

    r = _text_observation(settings, "СЕКРЕТ-PURGE")
    result = delete_observation(settings, r["observation_id"], namespace=NS, mode="purge")
    assert result["nodes_scrubbed"] == 1
    conn = dbmod.connect(settings, autocommit=True)
    try:
        node = GraphStore(conn, settings.graph_name, NS).get_node(
            f"{r['observation_id']}:text:0", namespaces=[NS]
        )
    finally:
        conn.close()
    assert node is None  # purge удаляет узел целиком


def test_retain_creates_graph_before_init_db(settings):
    """Дефект №5: первый вызов API до init-db не должен падать проекцией."""
    # НЕ используем фикстуру stores: граф и таблицы не созданы заранее.
    r = retain_observation(
        settings,
        {
            "kind": "work.completed",
            "content": "первое наблюдение на свежей БД",
            "assertions": [
                {"assert": "entity", "entity": {"type": "person", "id": "first"}},
            ],
        },
        namespace=NS,
    )
    assert r["status"] == "processed"
    # delete на свежесозданном графе тоже работает.
    assert delete_observation(settings, r["observation_id"], namespace=NS, mode="purge")
    # Зачистка артефактов (фикстура stores здесь не убирает).
    from platform_memory.core import db as dbmod

    conn = dbmod.connect(settings, autocommit=True)
    try:
        dbmod.drop_graph(conn, settings.graph_name)
        with conn.cursor() as cur:
            for table in (settings.chunks_table, settings.observations_table):
                cur.execute(f'DROP TABLE IF EXISTS public."{table}"')
    finally:
        conn.close()


def test_fact_scope_filter_in_context(settings, stores):
    """Дефект №2: факт чужого scope не попадает в ContextPack при scope-фильтре."""
    _conn, _graph, facts, _index, _obs = stores
    facts.ensure_entity("person:a", namespace=NS)
    facts.ensure_entity("project:sec", namespace=NS)
    facts.assert_fact(
        "person:a",
        "WORKS_ON",
        "project:sec",
        namespace=NS,
        valid_from="2026-01-01T00:00:00Z",
        scopes=["project:secret"],
    )
    # Запрос в чужом scope — факт скрыт.
    pack = build_context(
        settings,
        {"query": "q", "anchors": ["person:a"], "scopes": ["project:other"], "namespaces": [NS]},
    )
    sections = {s.kind: s for s in pack.sections}
    assert sections["relevant_facts"].items == []
    # Запрос в правильном scope — факт виден.
    pack2 = build_context(
        settings,
        {"query": "q", "anchors": ["person:a"], "scopes": ["project:secret"], "namespaces": [NS]},
    )
    sections2 = {s.kind: s for s in pack2.sections}
    assert len(sections2["relevant_facts"].items) == 1
