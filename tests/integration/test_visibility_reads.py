"""Видимость по principal (MEM-ADR-019) на маршрутах по ключу, удалениях и трейсах.

Один namespace, в нём память воркспейсов W1 и W2 и общая память без scope. Читают
и удаляют три principal через policy: W1, W2 и без воркспейсов (``[]``), все с
правом записи в namespace. Невидимое по ключу — ответ как у отсутствующего (чтение
и удаление), объект цел; в списках и трейсах — отбрасывается. Статический ключ
сервиса видимостью не ограничен.
"""

from __future__ import annotations

import uuid

import pytest

from platform_memory import retain_observation
from platform_memory.core.models import Chunk, Edge, Node
from platform_memory.core.namespaces import workspace_namespace

pytestmark = pytest.mark.integration

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")  # W1
BOB = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")  # W2
CAROL = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")  # без воркспейсов
W1 = "0b0b0b0b-0000-4000-8000-000000000001"
W2 = "0b0b0b0b-0000-4000-8000-000000000002"
NS = workspace_namespace(str(TENANT), W1)

ISSUER = "https://iam.test/iam"
JWKS_URL = "https://iam.test/iam/.well-known/jwks.json"
AUDIENCE = "memory-service"
SERVICE_KEY = "svc-test-key"
TRACE = "trace-visibility"

WORKSPACES = {str(ALICE): [W1], str(BOB): [W2], str(CAROL): []}


class _Policy:
    async def list_objects(self, ctx, action, resource_type, *, on_behalf_of=None, **kw):
        from platform_auth import ObjectPage

        if resource_type == "memory_namespace":
            objects = [f"ws-{W1}"]
        else:
            objects = WORKSPACES[str(on_behalf_of)]
        return ObjectPage(objects=objects, cursor=None, model_version="1")


@pytest.fixture()
def env(settings, stores, monkeypatch):
    """Policy-режим + статический ключ; возвращает (client, headers по имени)."""
    import platform_memory.server.app as app_mod
    from fastapi.testclient import TestClient
    from platform_auth.jwks import JwksCache
    from platform_auth.testing import SigningKey

    from platform_memory.server import iam as iam_mod
    from platform_memory.server import visibility as vis_mod

    policy_settings = settings.model_copy(
        update={
            "api_keys": "",
            "server_api_key": SERVICE_KEY,
            "server_api_keys_pii": "",
            "iam_enabled": True,
            "iam_issuer": ISSUER,
            "iam_jwks_url": JWKS_URL,
            "iam_audience": AUDIENCE,
            "policy_enabled": True,
            "pii_protection": False,
        }
    )
    key = SigningKey.generate()

    def seeded_keys(_settings):
        cache = JwksCache(JWKS_URL)
        cache.seed(key.jwks())
        return cache

    vis_mod.reset()
    iam_mod.reset()
    monkeypatch.setattr(app_mod, "get_settings", lambda: policy_settings)
    monkeypatch.setattr(iam_mod, "key_source_factory", seeded_keys)
    monkeypatch.setattr(vis_mod, "policy_client_factory", lambda _s: _Policy())

    def _headers(subject: uuid.UUID) -> dict[str, str]:
        token = key.issue(
            issuer=ISSUER,
            audience=AUDIENCE,
            subject=str(subject),
            tenant_id=str(TENANT),
            scopes=["memory:read", "memory:write"],
            principal_type="human",
        )
        return {"Authorization": f"Bearer {token}"}

    headers = {
        "w1": _headers(ALICE),
        "w2": _headers(BOB),
        "none": _headers(CAROL),
        "service": {"Authorization": f"Bearer {SERVICE_KEY}"},
    }
    yield TestClient(app_mod.app), headers
    vis_mod.reset()
    iam_mod.reset()


def _node(key: str, scopes: list[str] | None, content: str) -> Node:
    props: dict = {"content": content}
    if scopes is not None:
        props["scopes"] = scopes
    return Node(type="note", natural_key=key, title=key, properties=props, namespace=NS)


def test_source_by_key(env, stores):
    client, headers = env
    _conn, graph, *_ = stores
    graph.upsert_node(_node("src-w1", [f"workspace:{W1}"], "SECRET-W1"))
    graph.upsert_node(_node("src-public", None, "PUBLIC-TEXT"))
    params = {"namespace": NS}

    own = client.get("/api/brain/sources/src-w1", params=params, headers=headers["w1"])
    assert own.status_code == 200, own.text
    assert own.json()["content"] == "SECRET-W1"

    missing = client.get("/api/brain/sources/nope", params=params, headers=headers["w2"])
    assert missing.status_code == 404
    for who in ("w2", "none"):
        hidden = client.get("/api/brain/sources/src-w1", params=params, headers=headers[who])
        assert hidden.status_code == 404, (who, hidden.text)
        assert "SECRET-W1" not in hidden.text
        assert hidden.json()["detail"] == "Источник не найден: src-w1"
        public = client.get("/api/brain/sources/src-public", params=params, headers=headers[who])
        assert public.status_code == 200, (who, public.text)

    svc = client.get("/api/brain/sources/src-w1", params=params, headers=headers["service"])
    assert svc.status_code == 200 and svc.json()["content"] == "SECRET-W1"


def _observation(ext_id: str, scopes: list[str], content: str) -> dict:
    return {
        "source": {"system": "tracker", "stream": "events", "external_id": ext_id},
        "kind": "note",
        "occurred_at": "2026-09-01T10:00:00Z",
        "scopes": scopes,
        "content": content,
    }


def test_observations(env, settings):
    client, headers = env
    ids = {}
    for name, scopes, content in (
        ("w1", [f"workspace:{W1}"], "OBS-SECRET-W1"),
        ("public", ["task:t-1"], "OBS-PUBLIC"),
    ):
        result = retain_observation(settings, _observation(name, scopes, content), namespace=NS)
        ids[name] = result["observation_id"]
    params = {"namespace": NS}

    own = client.get(f"/api/memory/observations/{ids['w1']}", params=params, headers=headers["w1"])
    assert own.status_code == 200, own.text
    listed = client.get("/api/memory/observations", params=params, headers=headers["w1"])
    assert {o["observation_id"] for o in listed.json()["observations"]} == set(ids.values())
    assert sum(listed.json()["statuses"].values()) == 2

    for who in ("w2", "none"):
        hidden = client.get(
            f"/api/memory/observations/{ids['w1']}", params=params, headers=headers[who]
        )
        missing = client.get("/api/memory/observations/nope", params=params, headers=headers[who])
        assert hidden.status_code == missing.status_code == 404, (who, hidden.text)
        assert "OBS-SECRET-W1" not in hidden.text

        listed = client.get("/api/memory/observations", params=params, headers=headers[who])
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert [o["observation_id"] for o in body["observations"]] == [ids["public"]]
        assert body["count"] == 1
        assert sum(body["statuses"].values()) == 1  # свод статусов тоже без чужих
        assert "OBS-SECRET-W1" not in listed.text

    svc = client.get("/api/memory/observations", params=params, headers=headers["service"])
    assert svc.json()["count"] == 2


def test_brain_trace(env, stores):
    client, headers = env
    _conn, graph, *_ = stores
    anchor = graph.ensure_trace_anchor(TRACE, namespace=NS)
    for node in (
        _node("fact-w1", [f"workspace:{W1}"], "FACT-SECRET-W1"),
        _node("fact-public", None, "FACT-PUBLIC"),
        _node("doomed-w1", [f"workspace:{W1}"], "DOOMED-W1"),
    ):
        graph.upsert_node(node)
    for key in ("fact-w1", "fact-public"):
        graph.upsert_edge(Edge(type="IN_TRACE", src=key, dst=anchor, namespace=NS), "2026-09-01")
    graph.record_audit(TRACE, "agent", "note", payload={"text": "общее событие"}, namespace=NS)
    # Удаление узла W1 через API: снимок в событии аудита несёт scope узла.
    deleted = client.delete(
        "/api/brain/nodes/doomed-w1",
        params={"namespace": NS, "trace_id": TRACE},
        headers=headers["service"],
    )
    assert deleted.status_code == 200, deleted.text
    params = {"namespace": NS}

    own = client.get(f"/api/brain/trace/{TRACE}", params=params, headers=headers["w1"])
    assert own.status_code == 200, own.text
    body = own.json()
    assert {f["natural_key"] for f in body["facts"]} == {"fact-w1", "fact-public"}
    assert {e["props"]["action"] for e in body["events"]} == {"note", "delete"}
    assert "doomed-w1" in own.text

    for who in ("w2", "none"):
        resp = client.get(f"/api/brain/trace/{TRACE}", params=params, headers=headers[who])
        assert resp.status_code == 200, (who, resp.text)
        body = resp.json()
        assert [f["natural_key"] for f in body["facts"]] == ["fact-public"]
        assert [e["props"]["action"] for e in body["events"]] == ["note"]
        assert body["fact_count"] == 1 and body["event_count"] == 1
        assert "SECRET-W1" not in resp.text and "doomed-w1" not in resp.text

    svc = client.get(f"/api/brain/trace/{TRACE}", params=params, headers=headers["service"])
    assert svc.json()["fact_count"] == 2 and svc.json()["event_count"] == 2


def test_context_trace(env, settings):
    client, headers = env
    retain_observation(
        settings, _observation("ctx-w1", [f"workspace:{W1}"], "Регламент CTX-W1"), namespace=NS
    )
    body = {"query": "Регламент", "strategy": "hybrid", "scope": {"namespace": NS}}

    def _compile(who: str) -> str:
        resp = client.post("/api/memory/context", json=body, headers=headers[who])
        assert resp.status_code == 200, resp.text
        return resp.json()["trace_id"]

    def _read(trace_id: str, who: str):
        return client.get(
            f"/api/memory/context/trace/{trace_id}", params={"namespace": NS}, headers=headers[who]
        )

    w1_trace = _compile("w1")
    assert _read(w1_trace, "w1").status_code == 200
    assert _read(w1_trace, "service").status_code == 200
    missing = _read("no-such-trace", "w2")
    for who in ("w2", "none"):
        hidden = _read(w1_trace, who)
        assert hidden.status_code == missing.status_code == 404, (who, hidden.text)
        assert "Регламент" not in hidden.text

    # Трейс автора без ограничения видимости читает только вызывающий без ограничения.
    svc_trace = _compile("service")
    assert _read(svc_trace, "service").status_code == 200
    for who in ("w1", "w2", "none"):
        assert _read(svc_trace, who).status_code == 404

    # Видимость автора включает его principal-scope: чужому principal трейс не виден.
    none_trace = _compile("none")
    assert _read(none_trace, "none").status_code == 200
    assert _read(none_trace, "w1").status_code == 404


# --- удаление (TASK-001050) ---

DELETE_PARAMS = {"namespace": NS, "trace_id": TRACE}


def _add_chunk(settings, index, key: str) -> None:
    chunk = Chunk(node_key=key, text=f"текст {key}", source_path=key, title=key, namespace=NS)
    index.add_chunks([chunk], [[0.1] * settings.embedding_dim])


@pytest.mark.parametrize("route", ["nodes", "documents"])
def test_delete_foreign_node(env, stores, settings, route):
    client, headers = env
    _conn, graph, _facts, index, _obs = stores
    graph.upsert_node(_node("secret-w1", [f"workspace:{W1}"], "SECRET-W1"))
    _add_chunk(settings, index, "secret-w1")
    url = f"/api/brain/{route}/secret-w1"

    for who in ("w2", "none"):
        missing = client.delete(
            f"/api/brain/{route}/nope", params=DELETE_PARAMS, headers=headers[who]
        )
        hidden = client.delete(url, params=DELETE_PARAMS, headers=headers[who])
        assert hidden.status_code == missing.status_code, (who, hidden.text)
        assert "SECRET-W1" not in hidden.text and "note" not in hidden.text
        if route == "nodes":
            assert hidden.status_code == 404
            assert hidden.json()["detail"] == "Узел не найден: secret-w1"
        else:  # документы идемпотентны: отсутствующий — 200 deleted:false
            assert hidden.json()["deleted"] is False and hidden.json()["chunks_deleted"] == 0
        assert graph.get_node("secret-w1", namespaces=[NS]) is not None
        assert index.count_for_node("secret-w1", namespace=NS) == 1

    # Попытки чужих не оставили событий удаления в трейсе.
    trace = client.get(
        f"/api/brain/trace/{TRACE}", params={"namespace": NS}, headers=headers["service"]
    )
    assert trace.json()["event_count"] == 0

    own = client.delete(url, params=DELETE_PARAMS, headers=headers["w1"])
    assert own.status_code == 200, own.text
    assert own.json()["deleted"] is True and own.json()["chunks_deleted"] == 1
    assert graph.get_node("secret-w1", namespaces=[NS]) is None


def test_delete_public_node_by_anyone(env, stores):
    client, headers = env
    _conn, graph, *_ = stores
    graph.upsert_node(_node("public", None, "PUBLIC"))
    resp = client.delete("/api/brain/nodes/public", params=DELETE_PARAMS, headers=headers["none"])
    assert resp.status_code == 200, resp.text
    assert graph.get_node("public", namespaces=[NS]) is None


def test_delete_foreign_observation(env, settings, stores):
    client, headers = env
    oid = retain_observation(
        settings, _observation("w1", [f"workspace:{W1}"], "OBS-SECRET-W1"), namespace=NS
    )["observation_id"]
    url = f"/api/memory/observations/{oid}"

    for who in ("w2", "none"):
        missing = client.delete(
            "/api/memory/observations/nope", params=DELETE_PARAMS, headers=headers[who]
        )
        hidden = client.delete(url, params=DELETE_PARAMS, headers=headers[who])
        assert hidden.status_code == missing.status_code == 404, (who, hidden.text)
        own = client.get(url, params={"namespace": NS}, headers=headers["w1"])
        assert own.status_code == 200 and own.json()["content"] == "OBS-SECRET-W1"

    deleted = client.delete(url, params=DELETE_PARAMS, headers=headers["w1"])
    assert deleted.status_code == 200, deleted.text

    # Событие удаления несёт scopes наблюдения: чужим трейс его не показывает.
    def _actions(who: str) -> list[str]:
        resp = client.get(
            f"/api/brain/trace/{TRACE}", params={"namespace": NS}, headers=headers[who]
        )
        assert resp.status_code == 200, resp.text
        return [e["props"]["action"] for e in resp.json()["events"]]

    assert _actions("w1") == ["observation_delete"]
    assert _actions("w2") == _actions("none") == []


def test_trace_limit_after_visibility(env, stores):
    """Фильтр видимости — до LIMIT: чужие факты не вытесняют видимые из трейса."""
    client, headers = env
    _conn, graph, *_ = stores
    anchor = graph.ensure_trace_anchor(TRACE, namespace=NS)
    for i in range(5):
        graph.upsert_node(_node(f"w1-{i}", [f"workspace:{W1}"], "SECRET"))
        graph.upsert_edge(Edge(type="IN_TRACE", src=f"w1-{i}", dst=anchor, namespace=NS), "t")
    graph.upsert_node(_node("public", None, "PUBLIC"))
    graph.upsert_edge(Edge(type="IN_TRACE", src="public", dst=anchor, namespace=NS), "t")

    assert graph.trace_subgraph(TRACE, limit=3, namespace=NS, allowed_scopes=[])["fact_count"] == 1
    sub = graph.trace_subgraph(TRACE, limit=3, namespace=NS, allowed_scopes=["workspace:nope"])
    assert [f["natural_key"] for f in sub["facts"]] == ["public"]
    assert graph.trace_subgraph(TRACE, limit=3, namespace=NS)["fact_count"] == 3


def test_typed_trace_of_own_author(env):
    client, headers = env
    body = {"anchors": [{"value": "никто"}], "scope": {"namespaces": [NS]}}

    def _read(trace_id: str, who: str):
        return client.get(
            f"/api/memory/context/trace/{trace_id}", params={"namespace": NS}, headers=headers[who]
        )

    for author in ("w1", "none"):
        pack = client.post("/api/memory/context/typed", json=body, headers=headers[author])
        assert pack.status_code == 200, pack.text
        trace_id = pack.json()["trace_id"]
        trace = _read(trace_id, author)
        assert trace.status_code == 200, (author, trace.text)
        assert trace.json()["request"]["mode"] == "typed"
        assert isinstance(trace.json()["request"]["allowed_scopes"], list)
        assert _read(trace_id, "service").status_code == 200
        other = "w2" if author == "w1" else "w1"
        assert _read(trace_id, other).status_code == 404


def test_pii_access_event_carries_object_scopes(env, stores, settings, monkeypatch):
    """Журнал выдачи ПДн несёт scopes выданного объекта: чужой трейс его не покажет."""
    import platform_memory.server.app as app_mod

    client, headers = env
    _conn, graph, *_ = stores
    graph.upsert_node(_node("pii-w1", [f"workspace:{W1}"], "Телефон +7 912 345-67-89"))
    pii_settings = app_mod.get_settings().model_copy(
        update={"pii_protection": True, "server_api_keys_pii": SERVICE_KEY}
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: pii_settings)

    resp = client.get(
        "/api/brain/sources/pii-w1",
        params={"namespace": NS},
        headers={**headers["service"], "X-Run-Id": TRACE},
    )
    assert resp.status_code == 200, resp.text
    svc = client.get(
        f"/api/brain/trace/{TRACE}", params={"namespace": NS}, headers=headers["service"]
    )
    events = [e for e in svc.json()["events"] if e["props"]["action"] == "pii_access"]
    assert events and events[0]["props"]["payload"]["scopes"] == [f"workspace:{W1}"]
    for who in ("w2", "none"):
        hidden = client.get(
            f"/api/brain/trace/{TRACE}", params={"namespace": NS}, headers=headers[who]
        )
        assert hidden.json()["event_count"] == 0, who


# --- устойчивость к payload.scopes не того типа (ревью TASK-001050) ---


def _trace_actions(client, headers, who: str) -> list[str]:
    resp = client.get(f"/api/brain/trace/{TRACE}", params={"namespace": NS}, headers=headers[who])
    assert resp.status_code == 200, (who, resp.text)
    return sorted(e["props"]["action"] for e in resp.json()["events"])


def test_trace_tolerates_malformed_stored_event_scopes(env, stores):
    """События, записанные до валидации, с объектом/строкой вместо списка не роняют трейс.

    Не список — «без scope», как у Python ``is_visible``/``node_scopes``: такое событие
    видно всем, кто читает базу; событие со списком по-прежнему скрывается.
    """
    client, headers = env
    _conn, graph, *_ = stores
    graph.record_audit(TRACE, "a", "obj", payload={"scopes": {"a": 1}}, namespace=NS)
    graph.record_audit(TRACE, "a", "str", payload={"scopes": f"workspace:{W1}"}, namespace=NS)
    graph.record_audit(
        TRACE, "a", "mixed", payload={"scopes": [1, f"workspace:{W1}"]}, namespace=NS
    )
    graph.record_audit(TRACE, "a", "nodict", payload={"scopes": None}, namespace=NS)

    assert _trace_actions(client, headers, "w1") == ["mixed", "nodict", "obj", "str"]
    for who in ("w2", "none"):
        assert _trace_actions(client, headers, who) == ["nodict", "obj", "str"]


@pytest.mark.parametrize("scopes", [{"a": 1}, f"workspace:{W1}", ["без-двоеточия"], [1]])
def test_audit_rejects_malformed_payload_scopes(env, scopes):
    client, headers = env
    body = {
        "trace_id": TRACE,
        "action": "x",
        "payload": {"scopes": scopes},
        "scope": {"namespace": NS},
    }
    resp = client.post("/api/brain/audit", json=body, headers=headers["w2"])
    assert resp.status_code == 400, resp.text
    assert _trace_actions(client, headers, "service") == []


def test_audit_canonical_payload_scopes_hide_event(env):
    client, headers = env
    body = {
        "trace_id": TRACE,
        "action": "x",
        "payload": {"scopes": [{"type": "workspace", "id": W1}, f"workspace:{W1}"]},
        "scope": {"namespace": NS},
    }
    resp = client.post("/api/brain/audit", json=body, headers=headers["w2"])
    assert resp.status_code == 201, resp.text
    svc = client.get(
        f"/api/brain/trace/{TRACE}", params={"namespace": NS}, headers=headers["service"]
    )
    assert svc.json()["events"][0]["props"]["payload"]["scopes"] == [f"workspace:{W1}"]
    assert _trace_actions(client, headers, "w1") == ["x"]
    assert _trace_actions(client, headers, "w2") == _trace_actions(client, headers, "none") == []


def test_delete_document_skips_foreign_chunks_without_node(env, stores, settings):
    """Чанк W1 без узла: удаление документа от W2 его не трогает и не считает."""
    client, headers = env
    _conn, _graph, _facts, index, _obs = stores
    chunk = Chunk(
        node_key="doc-w1",
        text="текст",
        source_path="doc-w1",
        namespace=NS,
        meta={"scopes": [f"workspace:{W1}"]},
    )
    index.add_chunks([chunk], [[0.1] * settings.embedding_dim])
    url = "/api/brain/documents/doc-w1"

    for who in ("w2", "none"):
        resp = client.delete(url, params=DELETE_PARAMS, headers=headers[who])
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] is False and resp.json()["chunks_deleted"] == 0
        assert index.count_for_node("doc-w1", namespace=NS) == 1

    own = client.delete(url, params=DELETE_PARAMS, headers=headers["w1"])
    assert own.json()["chunks_deleted"] == 1
    assert index.count_for_node("doc-w1", namespace=NS) == 0
