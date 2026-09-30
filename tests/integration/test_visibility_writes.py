"""Видимость по principal (MEM-ADR-019) на маршрутах записи по существующему ключу.

Тот же namespace и те же principal, что в ``test_visibility_reads``: W1, W2 и без
воркспейсов (``[]``), все с правом записи в namespace. Запись по ключу узла или
чанка W1 от чужого — ``403`` без полей объекта, как при отсутствии права; узел W1,
его scopes и чанки целы, и чужой по-прежнему не видит и не удаляет его. Перезапись
видимого объекта scopes не стирает, а сливает (общая сущность — объединение).
"""

from __future__ import annotations

import pytest

from platform_memory.core.models import Chunk, Node
from tests.integration import test_visibility_reads as reads
from tests.integration.test_visibility_reads import DELETE_PARAMS, NS, W1, W2, _node, _observation

# Окружение видимости (policy W1/W2/[] + статический ключ) — то же, что у чтений.
env = reads.env

pytestmark = pytest.mark.integration

KEY = "secret-w1"
SECRET = "SECRET-W1"
W1_SCOPE = f"workspace:{W1}"
W2_SCOPE = f"workspace:{W2}"
FOREIGN = ("w2", "none")


def _seed(settings, graph, index, *, key: str = KEY, node: bool = True) -> None:
    """Узел W1 и его чанк W1 (или только чанк — ``node=False``)."""
    if node:
        graph.upsert_node(_node(key, [W1_SCOPE], SECRET))
    chunk = Chunk(
        node_key=key, text=SECRET, source_path=key, namespace=NS, meta={"scopes": [W1_SCOPE]}
    )
    index.add_chunks([chunk], [[0.1] * settings.embedding_dim])


def _entity(key: str, scopes: list[str]) -> Node:
    props = {"content": SECRET, "scopes": scopes}
    return Node(type="person", natural_key=key, title=key, properties=props, namespace=NS)


def _chunks(conn, settings, key: str = KEY) -> list[tuple[str, dict]]:
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT text, meta FROM "{settings.chunks_table}" '
            "WHERE node_key = %s AND namespace = %s ORDER BY chunk_order",
            (key, NS),
        )
        return list(cur.fetchall())


def _assert_w1_intact(client, headers, graph, conn, settings, key: str = KEY) -> None:
    node = graph.get_node(key, namespaces=[NS])
    assert node is not None
    assert node["props"]["scopes"] == [W1_SCOPE]
    assert node["props"]["content"] == SECRET
    assert _chunks(conn, settings, key) == [(SECRET, {"scopes": [W1_SCOPE]})]
    # Чужой по-прежнему не видит и не удаляет узел.
    for who in FOREIGN:
        hidden = client.get(
            f"/api/brain/nodes/{key}", params={"namespace": NS}, headers=headers[who]
        )
        assert hidden.status_code == 404, (who, hidden.text)
        deleted = client.delete(
            f"/api/brain/nodes/{key}", params=DELETE_PARAMS, headers=headers[who]
        )
        assert deleted.status_code == 404, (who, deleted.text)


def _assert_denied(resp, key: str = KEY) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"Нет прав на запись: {key}"
    assert SECRET not in resp.text and W1 not in resp.text


def _retain(key: str, content: str) -> dict:
    return {"content": content, "external_id": key, "scope": {"namespace": NS}}


def _document(key: str, text: str, *, replace: bool = True, **extra) -> dict:
    return {
        "natural_key": key,
        "title": key,
        "namespace": NS,
        "chunks": [{"text": text}],
        "replace": replace,
        **extra,
    }


def _fact(key: str, **extra) -> dict:
    return {"natural_key": key, "type": "note", "title": key, "scope": {"namespace": NS}, **extra}


ROUTES = {
    "retain": ("/api/brain/retain", lambda key: _retain(key, "OVERWRITE")),
    "documents-replace": ("/api/brain/documents", lambda key: _document(key, "OVERWRITE")),
    "documents-append": (
        "/api/brain/documents",
        lambda key: _document(key, "OVERWRITE", replace=False),
    ),
    "facts": ("/api/brain/facts", lambda key: _fact(key, properties={"content": "OVERWRITE"})),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_write_foreign_node_is_denied(env, stores, settings, route):
    client, headers = env
    conn, graph, _facts, index, _obs = stores
    _seed(settings, graph, index)
    url, body = ROUTES[route]

    for who in FOREIGN:
        _assert_denied(client.post(url, json=body(KEY), headers=headers[who]))
        _assert_w1_intact(client, headers, graph, conn, settings)
        # Свободный ключ тот же вызывающий пишет как раньше.
        fresh = client.post(url, json=body(f"fresh-{who}"), headers=headers[who])
        assert fresh.status_code == 201, (who, fresh.text)


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_write_foreign_chunk_without_node_is_denied(env, stores, settings, route):
    """Чанк W1 без узла: запись по ключу перезаписала бы его по chunk_order."""
    client, headers = env
    conn, graph, _facts, index, _obs = stores
    _seed(settings, graph, index, node=False)
    url, body = ROUTES[route]

    for who in FOREIGN:
        _assert_denied(client.post(url, json=body(KEY), headers=headers[who]))
        assert graph.get_node(KEY, namespaces=[NS]) is None
        assert _chunks(conn, settings) == [(SECRET, {"scopes": [W1_SCOPE]})]


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_own_rewrite_keeps_scopes(env, stores, settings, route):
    """Владелец перезаписывает свой узел: содержимое новое, scopes узла и чанка — те же."""
    client, headers = env
    conn, graph, _facts, index, _obs = stores
    _seed(settings, graph, index)
    url, body = ROUTES[route]

    resp = client.post(url, json=body(KEY), headers=headers["w1"])
    assert resp.status_code == 201, resp.text
    node = graph.get_node(KEY, namespaces=[NS])
    assert node["props"]["scopes"] == [W1_SCOPE]
    for _text, meta in _chunks(conn, settings):
        assert meta["scopes"] == [W1_SCOPE]
    for who in FOREIGN:
        hidden = client.get(
            f"/api/brain/nodes/{KEY}", params={"namespace": NS}, headers=headers[who]
        )
        assert hidden.status_code == 404, (who, hidden.text)


def test_documents_replace_by_owner_replaces_chunks(env, stores, settings):
    client, headers = env
    conn, graph, _facts, index, _obs = stores
    _seed(settings, graph, index)
    resp = client.post("/api/brain/documents", json=_document(KEY, "NEW"), headers=headers["w1"])
    assert resp.status_code == 201, resp.text
    assert _chunks(conn, settings) == [("NEW", {"scopes": [W1_SCOPE]})]


def test_service_rewrite_merges_scopes(env, stores, settings):
    """Без ограничения видимости перезапись не стирает scopes, а объединяет их."""
    client, headers = env
    conn, graph, _facts, index, _obs = stores
    _seed(settings, graph, index)

    retained = client.post(
        "/api/brain/retain", json=_retain(KEY, "SERVICE"), headers=headers["service"]
    )
    assert retained.status_code == 201, retained.text
    assert graph.get_node(KEY, namespaces=[NS])["props"]["scopes"] == [W1_SCOPE]

    fact = _fact(KEY, properties={"content": "SERVICE", "scopes": [W2_SCOPE]})
    written = client.post("/api/brain/facts", json=fact, headers=headers["service"])
    assert written.status_code == 201, written.text
    assert graph.get_node(KEY, namespaces=[NS])["props"]["scopes"] == [W1_SCOPE, W2_SCOPE]
    assert [meta["scopes"] for _t, meta in _chunks(conn, settings)] == [[W1_SCOPE, W2_SCOPE]]
    none = client.get(f"/api/brain/nodes/{KEY}", params={"namespace": NS}, headers=headers["none"])
    assert none.status_code == 404


def test_write_into_public_node_keeps_it_shared(env, stores):
    """Scope видимости записи не приватизирует общий узел: остальные его видят."""
    client, headers = env
    _conn, graph, *_ = stores
    graph.upsert_node(_node("public", None, "PUBLIC"))
    fact = _fact("public", properties={"content": "EDIT", "scopes": [W2_SCOPE]})
    resp = client.post("/api/brain/facts", json=fact, headers=headers["w2"])
    assert resp.status_code == 201, resp.text
    assert "scopes" not in graph.get_node("public", namespaces=[NS])["props"]
    seen = client.get("/api/brain/nodes/public", params={"namespace": NS}, headers=headers["none"])
    assert seen.status_code == 200, seen.text


def _assertions_observation(ext_id: str, scopes: list[str]) -> dict:
    return {
        **_observation(ext_id, scopes, "OBS"),
        "assertions": [
            {"assert": "entity", "entity": {"type": "person", "id": "w1"}},
            {"assert": "text", "text": {"key": KEY, "content": "OVERWRITE"}},
        ],
    }


@pytest.mark.parametrize("batch", [False, True])
def test_observation_assertions_do_not_rewrite_foreign_nodes(env, stores, settings, batch):
    client, headers = env
    conn, graph, _facts, index, _obs = stores
    _seed(settings, graph, index)
    graph.upsert_node(_entity("person:w1", [W1_SCOPE]))

    for who in FOREIGN:
        obs = _assertions_observation(f"{who}-{batch}", [W2_SCOPE] if who == "w2" else [])
        if batch:
            resp = client.post(
                "/api/memory/observations:batch",
                json={"observations": [obs], "scope": {"namespace": NS}},
                headers=headers[who],
            )
            assert resp.status_code == 207, resp.text
            result = resp.json()["results"][0]
        else:
            resp = client.post(
                "/api/memory/observations",
                json={**obs, "scope": {"namespace": NS}},
                headers=headers[who],
            )
            assert resp.status_code == 201, resp.text
            result = resp.json()
        assert result["status"] == "failed", (who, result)
        assert SECRET not in resp.text
        entity = graph.get_node("person:w1", namespaces=[NS])
        assert entity["props"]["scopes"] == [W1_SCOPE]
        _assert_w1_intact(client, headers, graph, conn, settings)


def test_observation_shared_entity_unions_scopes(env, stores, settings):
    """Сущность из наблюдений двух воркспейсов (ингест ядра) видна обоим."""
    client, headers = env
    _conn, graph, *_ = stores
    graph.upsert_node(_entity("person:shared", [W1_SCOPE]))
    obs = {
        **_observation("shared", [W2_SCOPE], "OBS"),
        "assertions": [{"assert": "entity", "entity": {"type": "person", "id": "shared"}}],
    }
    resp = client.post(
        "/api/memory/observations",
        json={**obs, "scope": {"namespace": NS}},
        headers=headers["service"],
    )
    assert resp.json()["status"] == "processed", resp.text
    entity = graph.get_node("person:shared", namespaces=[NS])
    assert entity["props"]["scopes"] == [W1_SCOPE, W2_SCOPE]
    for who in ("w1", "w2"):
        seen = client.get(
            "/api/brain/nodes/person:shared", params={"namespace": NS}, headers=headers[who]
        )
        assert seen.status_code == 200, (who, seen.text)


def test_redrive_does_not_rewrite_node_invisible_to_observation(env, stores, settings):
    """Redrive пишет с видимостью пишущего из записи: чужое ему не перезаписывается."""
    from platform_memory.observations.ingest import process_observations

    from platform_memory.core.observations import Observation

    _conn, graph, _facts, _index, obs_store = stores

    graph.upsert_node(_entity("person:w1", [W1_SCOPE]))
    payload = {
        **_observation("redrive", [W2_SCOPE], "OBS"),
        "assertions": [{"assert": "entity", "entity": {"type": "person", "id": "w1"}}],
    }
    obs = Observation.from_payload(payload, namespace=NS)
    obs_store.insert(obs, writer_scopes=[W2_SCOPE])  # received, без проекции
    result = process_observations(settings, namespace=NS)
    assert result["statuses"] == {"failed": 1}
    assert graph.get_node("person:w1", namespaces=[NS])["props"]["scopes"] == [W1_SCOPE]


# --- scopes создаваемого объекта, факты и supersedes (ревью TASK-001052) --------


def _observe(client, headers, obs: dict) -> object:
    return client.post(
        "/api/memory/observations", json={**obs, "scope": {"namespace": NS}}, headers=headers
    )


def _entity_obs(ext_id: str, scopes: list[str], entity_id: str, title: str) -> dict:
    return {
        **_observation(ext_id, scopes, "OBS"),
        "assertions": [
            {"assert": "entity", "entity": {"type": "person", "id": entity_id, "title": title}}
        ],
    }


def _fact_obs(ext_id: str, scopes: list[str], subject: str, obj: str, **fact) -> dict:
    return {
        **_observation(ext_id, scopes, "OBS"),
        "assertions": [
            {
                "assert": "fact",
                "fact": {
                    "subject": {"type": "person", "id": subject},
                    "predicate": "works_on",
                    "object": {"type": "person", "id": obj},
                    **fact,
                },
            }
        ],
    }


def test_observation_with_foreign_scope_is_denied_and_redrive_keeps_node(env, stores, settings):
    """Scopes наблюдения — только из видимости пишущего: redrive не пишет от чужого имени."""
    client, headers = env
    _conn, graph, _facts, _index, obs_store = stores
    graph.upsert_node(_entity("person:victim", [W1_SCOPE]))
    obs = _entity_obs("hijack", [W1_SCOPE], "victim", "HIJACKED")

    for who in FOREIGN:
        single = _observe(
            client, headers[who], {**obs, "source": {**obs["source"], "external_id": who}}
        )
        assert single.status_code == 403, (who, single.text)
        assert single.json()["detail"] == f"Нет прав на запись: {W1_SCOPE}"
        batch = client.post(
            "/api/memory/observations:batch",
            json={"observations": [obs], "scope": {"namespace": NS}},
            headers=headers[who],
        )
        assert batch.status_code == 207, batch.text
        assert batch.json()["failed"] == 1
        assert batch.json()["results"][0]["error"] == f"Нет прав на запись: {W1_SCOPE}"
    # Наблюдение не сохранено — redrive нечего переигрывать от имени W1.
    assert obs_store.count([NS]) == 0

    consolidated = client.post(
        "/api/memory/consolidate", json={"namespace": NS}, headers=headers["w2"]
    )
    assert consolidated.status_code == 200, consolidated.text
    node = graph.get_node("person:victim", namespaces=[NS])
    assert node["title"] != "HIJACKED"
    assert node["props"]["scopes"] == [W1_SCOPE]


def test_write_with_foreign_scope_is_denied(env, stores, settings):
    """``properties.scopes``/``meta.scopes`` чужого воркспейса не создают объект."""
    client, headers = env
    conn, graph, *_ = stores
    fact = _fact("planted", properties={"content": "X", "scopes": [W1_SCOPE]})
    doc = _document("planted", "X", meta={"scopes": [W1_SCOPE]})
    for url, body in (("/api/brain/facts", fact), ("/api/brain/documents", doc)):
        for who in FOREIGN:
            resp = client.post(url, json=body, headers=headers[who])
            assert resp.status_code == 403, (url, who, resp.text)
            assert resp.json()["detail"] == f"Нет прав на запись: {W1_SCOPE}"
    assert graph.get_node("planted", namespaces=[NS]) is None
    assert _chunks(conn, settings, "planted") == []
    # Свой scope — можно.
    own = _fact("planted", properties={"content": "X", "scopes": [W1_SCOPE]})
    assert client.post("/api/brain/facts", json=own, headers=headers["w1"]).status_code == 201


def _seed_fact(graph, facts, scopes: list[str]) -> str:
    for key in ("person:a", "person:b", "person:c"):
        graph.upsert_node(_entity(key, []))  # общие концы уровня namespace
    return facts.assert_fact("person:a", "works_on", "person:b", namespace=NS, scopes=scopes)[
        "fact_id"
    ]


def test_repeated_fact_keeps_owner_scopes(env, stores, settings):
    """Повтор факта W1 от чужого — отказ; факт W1 цел и остаётся его."""
    client, headers = env
    _conn, graph, facts, _index, _obs = stores
    fid = _seed_fact(graph, facts, [W1_SCOPE])

    for who, scopes in (("w2", [W2_SCOPE]), ("none", [])):
        resp = _observe(client, headers[who], _fact_obs(f"rep-{who}", scopes, "a", "b"))
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "failed", (who, resp.json())
        fact = facts.get_fact(fid, namespaces=[NS])
        assert fact["scopes"] == [W1_SCOPE]
        assert fact["observation_ids"] == []

    # Без ограничения (ингест ядра) повтор сливает scopes: факт виден обоим.
    svc = _observe(client, headers["service"], _fact_obs("rep-svc", [W2_SCOPE], "a", "b"))
    assert svc.json()["status"] == "processed", svc.text
    assert facts.get_fact(fid, namespaces=[NS])["scopes"] == [W1_SCOPE, W2_SCOPE]


def test_supersedes_does_not_close_foreign_fact(env, stores, settings):
    client, headers = env
    _conn, graph, facts, _index, _obs = stores
    fid = _seed_fact(graph, facts, [W1_SCOPE])

    resp = _observe(client, headers["w2"], _fact_obs("sup", [W2_SCOPE], "a", "c", supersedes=fid))
    assert resp.json()["status"] == "processed", resp.text
    old = facts.get_fact(fid, namespaces=[NS])
    assert old["valid_to"] is None and old["superseded_by"] is None

    # Свой факт владелец закрывает, как прежде.
    own = _observe(
        client, headers["w1"], _fact_obs("sup-own", [W1_SCOPE], "a", "d", supersedes=fid)
    )
    assert own.json()["status"] == "processed", own.text
    assert facts.get_fact(fid, namespaces=[NS])["valid_to"] is not None


def test_fact_to_foreign_end_is_denied(env, stores, settings):
    """Конец факта — существующий узел W1: чужой факт к нему не подвешивает."""
    client, headers = env
    _conn, graph, facts, _index, _obs = stores
    graph.upsert_node(_entity("person:w1", [W1_SCOPE]))
    resp = _observe(client, headers["w2"], _fact_obs("end", [W2_SCOPE], "x", "w1"))
    assert resp.json()["status"] == "failed", resp.text
    assert facts.facts(obj="person:w1", namespaces=[NS]) == []


def test_links_to_foreign_node_look_like_missing(env, stores, settings):
    client, headers = env
    _conn, graph, _facts, index, _obs = stores
    _seed(settings, graph, index)
    for dst in (KEY, "missing"):
        resp = client.post(
            "/api/brain/facts", json=_fact(f"link-{dst}", links=[dst]), headers=headers["w2"]
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["edges"] == 0
    own = client.post(
        "/api/brain/facts", json=_fact("link-own", links=[KEY]), headers=headers["w1"]
    )
    assert own.json()["edges"] == 1


def test_private_observation_into_shared_node_warns(env, stores, settings):
    """Содержимое приватного наблюдения, легшее в общий узел, — предупреждение."""
    client, headers = env
    _conn, graph, *_ = stores
    graph.upsert_node(_entity("person:public", []))
    shared = _observe(client, headers["w1"], _entity_obs("shared", [W1_SCOPE], "public", "T"))
    assert shared.json()["status"] == "processed", shared.text
    assert shared.json()["warnings"] == [
        "assertion[0]: узел person:public общий для namespace — "
        "его содержимое видно без scopes наблюдения"
    ]
    private = _observe(client, headers["w1"], _entity_obs("private", [W1_SCOPE], "new", "T"))
    assert "warnings" not in private.json(), private.text
