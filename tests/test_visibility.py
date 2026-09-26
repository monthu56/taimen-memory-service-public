"""Видимость по principal (MEM-ADR-019): предикаты, VisibleSet и HTTP-слой без БД и сети."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from platform_auth import AuthorizationUnavailable, ObjectPage
from platform_auth.jwks import JwksCache
from platform_auth.testing import SigningKey

import platform_memory.server.app as app_mod
import platform_memory.server.memory_api as api_mod
from platform_memory.context.models import ContextPack, ContextRequest
from platform_memory.core.namespaces import (
    namespace_from_policy_object,
    principal_namespace,
    workspace_namespace,
)
from platform_memory.core.scopes import (
    chunk_visibility_sql,
    is_visible,
    observation_visibility_sql,
)
from platform_memory.index.store import SearchHit
from platform_memory.server import iam as iam_mod
from platform_memory.server import visibility as vis_mod

pytestmark = pytest.mark.unit

ISSUER = "https://iam.test/iam"
JWKS_URL = "https://iam.test/iam/.well-known/jwks.json"
AUDIENCE = "memory-service"
TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
WS_A = "33333333-3333-4333-8333-333333333333"
WS_B = "44444444-4444-4444-8444-444444444444"
NS_A = workspace_namespace(str(TENANT), WS_A)
NS_B = workspace_namespace(str(TENANT), WS_B)


# --- чистые предикаты ----------------------------------------------------------


def test_is_visible_private_by_default():
    allowed = ["workspace:w1", "principal:p1"]
    assert is_visible([], allowed)  # без scopes — уровень namespace
    assert is_visible(["task:t1", "run:r1"], allowed)  # только релевантность
    assert is_visible(["workspace:w1", "task:t1"], allowed)
    assert not is_visible(["workspace:w2"], allowed)
    assert not is_visible(["principal:p2"], allowed)
    assert is_visible(["workspace:w2"], None)  # без ограничения


def test_visibility_sql_shapes():
    obs = observation_visibility_sql("scopes")
    assert (
        "scopes ?| %s" in obs
        and "LIKE 'workspace:%%'" in obs
        and "jsonb_array_elements_text" in obs
    )
    chunk = chunk_visibility_sql()
    assert chunk.startswith("(NOT meta ? 'scopes' OR ") and "(meta->'scopes') ?| %s" in chunk


def test_namespace_from_policy_object():
    t = str(TENANT)
    assert namespace_from_policy_object(t, f"ws-{WS_A}") == NS_A
    assert namespace_from_policy_object(t, f"principal-{ALICE}") == principal_namespace(
        t, str(ALICE)
    )
    assert namespace_from_policy_object(t, f"tenant-{t}") == f"tenant:{t}"
    assert namespace_from_policy_object(t, "bogus") is None
    assert namespace_from_policy_object(t, "x-1") is None


def test_context_request_reads_allowed_scopes():
    req = ContextRequest.from_payload({"query": "x", "allowedScopes": ["workspace:w1"]})
    assert req.allowed_scopes == ["workspace:w1"]
    assert ContextRequest.from_payload({"query": "x"}).allowed_scopes is None
    assert ContextRequest.from_payload({"strategy": "briefing"}).strategy == "briefing"


# --- VisibleSet ------------------------------------------------------------------


class _FakePolicy:
    def __init__(self, namespaces: list[str], workspaces: list[str], fail: bool = False):
        self.namespaces = namespaces
        self.workspaces = workspaces
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []

    async def list_objects(self, ctx, action, resource_type, *, on_behalf_of=None, **kw):
        self.calls.append((action, resource_type, on_behalf_of))
        if self.fail:
            raise AuthorizationUnavailable("policy_service_unavailable")
        objects = self.namespaces if resource_type == "memory_namespace" else self.workspaces
        return ObjectPage(objects=list(objects), cursor=None, model_version="1")


def _subject(scopes=("memory:read",), principal=ALICE):
    return SimpleNamespace(
        tenant_id=TENANT,
        principal_id=principal,
        has_scope=lambda s: s in scopes,
    )


@pytest.fixture(autouse=True)
def _reset():
    vis_mod.reset()
    iam_mod.reset()
    yield
    vis_mod.reset()
    iam_mod.reset()
    vis_mod.policy_client_factory = vis_mod._default_client


def _settings(**overrides):
    return app_mod.get_settings().model_copy(update={"policy_enabled": True, **overrides})


def test_visible_set_unrestricted_when_disabled_or_static_key():
    assert (
        asyncio.run(vis_mod.resolve_visibility(_settings(policy_enabled=False), _subject(), None))
        == vis_mod.UNRESTRICTED
    )
    assert asyncio.run(vis_mod.resolve_visibility(_settings(), None, None)) == vis_mod.UNRESTRICTED


def test_visible_set_from_policy_with_cache(monkeypatch):
    policy = _FakePolicy([f"ws-{WS_A}", f"tenant-{TENANT}"], [WS_A, "child-1"])
    monkeypatch.setattr(vis_mod, "policy_client_factory", lambda _s: policy)
    visible = asyncio.run(vis_mod.resolve_visibility(_settings(), _subject(), None))
    assert visible.namespaces == frozenset(
        {NS_A, f"tenant:{TENANT}", principal_namespace(str(TENANT), str(ALICE))}
    )
    assert visible.scopes == (f"workspace:{WS_A}", "workspace:child-1", f"principal:{ALICE}")
    assert visible.denied_namespace([NS_A]) is None
    assert visible.denied_namespace([NS_A, NS_B]) == NS_B
    # второй вызов — из кэша
    asyncio.run(vis_mod.resolve_visibility(_settings(), _subject(), None))
    assert len(policy.calls) == 2
    assert policy.calls[0] == ("memory.read", "memory_namespace", str(ALICE))


def test_visible_set_policy_unavailable_is_503(monkeypatch):
    monkeypatch.setattr(vis_mod, "policy_client_factory", lambda _s: _FakePolicy([], [], fail=True))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(vis_mod.resolve_visibility(_settings(), _subject(), None))
    assert excinfo.value.status_code == 503


def test_visible_set_on_behalf_requires_body_sets():
    subject = _subject(scopes=("memory:read", "memory:on-behalf"))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(vis_mod.resolve_visibility(_settings(), subject, {"query": "x"}))
    assert excinfo.value.status_code == 403
    visible = asyncio.run(
        vis_mod.resolve_visibility(
            _settings(), subject, {"allowedNamespaces": [NS_A], "allowedScopes": ["workspace:w1"]}
        )
    )
    assert visible.namespaces == frozenset({NS_A}) and visible.scopes == ("workspace:w1",)


# --- клиентское сужение (MEM-ADR-021) --------------------------------------------


def test_narrow_absent_fields_keep_server_visibility():
    server = vis_mod.VisibleSet(namespaces=frozenset({NS_A}), scopes=("workspace:w1",))
    assert vis_mod.narrow_visibility(server, None) == server
    assert vis_mod.narrow_visibility(server, {"query": "x"}) == server
    assert vis_mod.narrow_visibility(server, {"allowedScopes": None}) == server
    assert vis_mod.narrow_visibility(vis_mod.UNRESTRICTED, {"query": "x"}) == vis_mod.UNRESTRICTED


def test_narrow_is_intersection_never_expansion():
    server = vis_mod.VisibleSet(
        namespaces=frozenset({NS_A}), scopes=("workspace:w1", "workspace:w2", "principal:p")
    )
    got = vis_mod.narrow_visibility(
        server,
        {"allowedNamespaces": [NS_A, NS_B], "allowedScopes": ["workspace:w2", "workspace:evil"]},
    )
    assert got.namespaces == frozenset({NS_A})
    assert got.scopes == ("workspace:w2",)


def test_narrow_unrestricted_server_takes_client_sets():
    got = vis_mod.narrow_visibility(
        vis_mod.UNRESTRICTED,
        {"allowed_namespaces": [NS_A], "allowed_scopes": ["workspace:w1", "workspace:w1"]},
    )
    assert got.namespaces == frozenset({NS_A}) and got.scopes == ("workspace:w1",)


def test_narrow_empty_list_is_nothing_not_everything():
    got = vis_mod.narrow_visibility(
        vis_mod.UNRESTRICTED, {"allowedNamespaces": [], "allowedScopes": []}
    )
    assert got.namespaces == frozenset() and got.scopes == ()
    assert not is_visible(["workspace:w1"], got.scopes)
    with pytest.raises(HTTPException) as excinfo:
        vis_mod.check_visible(got, [NS_A])
    assert excinfo.value.status_code == 403
    # Запрос без scope (дефолт движка) тоже не обходит пустое множество namespaces.
    with pytest.raises(HTTPException):
        vis_mod.check_visible(got, [], "nexus")


@pytest.mark.parametrize(
    "body",
    [
        {"allowedScopes": "workspace:w1"},
        {"allowedScopes": [1]},
        {"allowedScopes": ["no-colon"]},
        {"allowedNamespaces": ["Bad NS"]},
    ],
)
def test_narrow_malformed_is_400(body):
    with pytest.raises(HTTPException) as excinfo:
        vis_mod.narrow_visibility(vis_mod.UNRESTRICTED, body)
    assert excinfo.value.status_code == 400


# --- HTTP: IAM-токен человека, policy подменён ----------------------------------


class _Env:
    def __init__(self):
        self.context_payloads: list[dict] = []
        self.retrieve_kwargs: list[dict] = []


def _install(monkeypatch, env: _Env, signing_key: SigningKey, policy: _FakePolicy) -> TestClient:
    settings = app_mod.get_settings().model_copy(
        update={
            "api_keys": "",
            "server_api_key": "legacy-secret",
            "server_api_keys_pii": "",
            "pii_protection": False,
            "default_namespace": "nexus",
            "iam_enabled": True,
            "iam_issuer": ISSUER,
            "iam_jwks_url": JWKS_URL,
            "iam_audience": AUDIENCE,
            "policy_enabled": True,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    def seeded_keys(_settings):
        cache = JwksCache(JWKS_URL)
        cache.seed(signing_key.jwks())
        return cache

    monkeypatch.setattr(iam_mod, "key_source_factory", seeded_keys)
    monkeypatch.setattr(vis_mod, "policy_client_factory", lambda _s: policy)

    def fake_retrieve(_settings, _q, k=8, hops=1, namespaces=(), meta_filter=None, **kw):
        env.retrieve_kwargs.append({"namespaces": list(namespaces), **kw})
        hit = SearchHit(
            chunk_id=1,
            node_key="kb-1",
            source_path="/kb.md",
            title="Запись",
            heading="H",
            text="текст",
            score=0.9,
            namespace=namespaces[0] if namespaces else "nexus",
        )
        return SimpleNamespace(hits=[hit], neighbors={}, sources=[], context="текст")

    monkeypatch.setattr(app_mod, "retrieve", fake_retrieve)

    def fake_build_context(_settings, payload):
        env.context_payloads.append(dict(payload))
        return ContextPack(query=str(payload.get("query", "")), sections=[], trace_id="ctx-1")

    monkeypatch.setattr(api_mod, "build_context", fake_build_context)
    return TestClient(app_mod.app)


def _token(signing_key: SigningKey, *, scopes: tuple[str, ...], sub=ALICE) -> str:
    return signing_key.issue(
        issuer=ISSUER,
        audience=AUDIENCE,
        subject=str(sub),
        tenant_id=str(TENANT),
        scopes=list(scopes),
        principal_type="human",
    )


def test_http_query_and_context_apply_visibility(monkeypatch):
    env = _Env()
    key = SigningKey.generate()
    policy = _FakePolicy([f"ws-{WS_A}"], [WS_A])
    client = _install(monkeypatch, env, key, policy)
    headers = {"Authorization": f"Bearer {_token(key, scopes=('memory:read',))}"}

    ok = client.post(
        "/api/brain/query",
        json={"question": "q", "scope": {"namespaces": [NS_A]}},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert env.retrieve_kwargs[-1]["allowed_scopes"] == (f"workspace:{WS_A}", f"principal:{ALICE}")

    denied = client.post(
        "/api/brain/query",
        json={"question": "q", "scope": {"namespaces": [NS_B]}},
        headers=headers,
    )
    assert denied.status_code == 403
    assert "вне видимости" in denied.json()["detail"]

    ctx = client.post(
        "/api/memory/context",
        json={"query": "q", "scope": {"namespaces": [NS_A]}, "allowedScopes": ["workspace:hacked"]},
        headers=headers,
    )
    assert ctx.status_code == 200, ctx.text
    # Клиент не расширяет серверную видимость: пересечение с чужим scope пусто (MEM-ADR-021).
    assert env.context_payloads[-1]["allowed_scopes"] == []

    narrowed = client.post(
        "/api/memory/context",
        json={
            "query": "q",
            "scope": {"namespaces": [NS_A]},
            "allowedScopes": [f"workspace:{WS_A}", "workspace:hacked"],
        },
        headers=headers,
    )
    assert narrowed.status_code == 200, narrowed.text
    assert env.context_payloads[-1]["allowed_scopes"] == [f"workspace:{WS_A}"]

    plain = client.post(
        "/api/memory/context", json={"query": "q", "scope": {"namespaces": [NS_A]}}, headers=headers
    )
    assert plain.status_code == 200, plain.text
    assert env.context_payloads[-1]["allowed_scopes"] == [f"workspace:{WS_A}", f"principal:{ALICE}"]

    # allowedNamespaces тоже только сужает: NS_B вне серверной видимости остаётся 403.
    widened = client.post(
        "/api/memory/context",
        json={"query": "q", "scope": {"namespaces": [NS_B]}, "allowedNamespaces": [NS_A, NS_B]},
        headers=headers,
    )
    assert widened.status_code == 403

    # статический ключ — service-режим без ограничений
    legacy = client.post(
        "/api/brain/query",
        json={"question": "q", "scope": {"namespaces": [NS_B]}},
        headers={"Authorization": "Bearer legacy-secret"},
    )
    assert legacy.status_code == 200
    assert env.retrieve_kwargs[-1]["allowed_scopes"] is None


def test_http_on_behalf_service_account(monkeypatch):
    env = _Env()
    key = SigningKey.generate()
    client = _install(monkeypatch, env, key, _FakePolicy([], []))
    headers = {
        "Authorization": f"Bearer {_token(key, scopes=('memory:read', 'memory:on-behalf'), sub=uuid.uuid4())}"
    }
    missing = client.post(
        "/api/memory/context", json={"query": "q", "scope": {"namespaces": [NS_A]}}, headers=headers
    )
    assert missing.status_code == 403
    ok = client.post(
        "/api/memory/context",
        json={
            "query": "q",
            "scope": {"namespaces": [NS_A]},
            "allowedNamespaces": [NS_A],
            "allowedScopes": [f"workspace:{WS_A}"],
        },
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert env.context_payloads[-1]["allowed_scopes"] == [f"workspace:{WS_A}"]
    assert "allowedNamespaces" not in env.context_payloads[-1]
