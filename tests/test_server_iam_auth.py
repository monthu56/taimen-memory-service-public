"""IAM access token на HTTP-слое (CB_IAM_ENABLED, ADR-018) рядом со статическими ключами.

Без БД и без сети: retrieve/сторы подменяются фейками, токены выпускаются локальной парой
ключей (``platform_auth.testing.SigningKey``), JWKS-кэш засевается напрямую. Главные
регрессии: при включённом IAM статические ключи работают как прежде; IAM-токен видит только
``tenant:<tenant_id>`` и его поддерево (плюс claim ``memory_namespaces``); чужой namespace,
чужой audience/issuer, отсутствие scope и недоступный JWKS дают 403/401/403/503 — и
никогда не allow.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from platform_auth.jwks import JwksCache
from platform_auth.testing import SigningKey

from platform_memory.index.store import SearchHit
from platform_memory.server import app as app_mod
from platform_memory.server import iam as iam_mod
from platform_memory.server.auth import CallerGrants

pytestmark = pytest.mark.unit

ISSUER = "https://iam.test/iam"
JWKS_URL = "https://iam.test/iam/.well-known/jwks.json"
AUDIENCE = "memory-service"
TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = uuid.UUID("22222222-2222-4222-8222-222222222222")
NS_TENANT = f"tenant:{TENANT}"
NS_TENANT_KB = f"tenant:{TENANT}:kb"
NS_OTHER = f"tenant:{OTHER_TENANT}"
PHONE_TEXT = "Клиент Иванов, тел +7 999 123-45-67, вопрос по карте."

_KEYS = [
    {
        "name": "tp-api",
        "key": "tp-secret",
        "grants": [{"prefix": "tp:", "read": True, "write": True}],
    }
]


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture(autouse=True)
def _reset_verifiers():
    iam_mod.reset()
    yield
    iam_mod.reset()


class _Env:
    def __init__(self):
        self.read_namespaces: list[list[str]] = []
        self.write_namespaces: list[str] = []


def _install(monkeypatch, env: _Env, signing_key: SigningKey | None, **overrides) -> TestClient:
    settings = app_mod.get_settings().model_copy(
        update={
            "api_keys": json.dumps(_KEYS),
            "server_api_key": "legacy-secret",
            "server_api_keys_pii": "",
            "pii_protection": False,
            "default_namespace": "nexus",
            "iam_enabled": True,
            "iam_issuer": ISSUER,
            "iam_jwks_url": JWKS_URL,
            "iam_audience": AUDIENCE,
            **overrides,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    if signing_key is not None:

        def seeded_keys(_settings):
            cache = JwksCache(JWKS_URL)
            cache.seed(signing_key.jwks())
            return cache

        monkeypatch.setattr(iam_mod, "key_source_factory", seeded_keys)

    def fake_retrieve(_settings, _q, k=8, hops=1, namespaces=(), meta_filter=None, **kw):
        env.read_namespaces.append(list(namespaces))
        hit = SearchHit(
            chunk_id=1,
            node_key="kb-1",
            source_path="/kb.md",
            title="Запись",
            heading="H",
            text=PHONE_TEXT,
            score=0.9,
            namespace=namespaces[0] if namespaces else "nexus",
        )
        return SimpleNamespace(hits=[hit], neighbors={}, sources=[], context=PHONE_TEXT)

    def fake_write(_settings, natural_key, type_, title, **kw):
        env.write_namespaces.append(kw.get("namespace", ""))
        return {"natural_key": natural_key, "namespace": kw.get("namespace", "")}

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeGraph:
        def __init__(self, conn, graph_name, default_namespace=""):
            pass

        def record_audit(self, trace_id, actor, action, **kw):
            return {"trace_id": trace_id}

        def total_nodes(self):
            return 0

        def count_nodes_by_type(self, namespaces=None):
            return {}

        def count_edges_by_type(self, namespaces=None):
            return {}

    class FakeIndex:
        def __init__(self, conn, table, dim, default_namespace=""):
            pass

        def table_exists(self):
            return False

    monkeypatch.setattr(app_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(app_mod, "write_and_index_fact", fake_write)
    monkeypatch.setattr(app_mod.dbmod, "connect", lambda s: FakeConn())
    monkeypatch.setattr(app_mod, "GraphStore", FakeGraph)
    monkeypatch.setattr(app_mod, "VectorIndex", FakeIndex)
    return TestClient(app_mod.app)


def _token(signing_key: SigningKey, **kw) -> str:
    params = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "tenant_id": TENANT,
        "scopes": [iam_mod.SCOPE_READ, iam_mod.SCOPE_WRITE],
        "principal_type": "agent",
    }
    params.update(kw)
    return signing_key.issue(**params)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _recall(namespaces: list[str] | None = None) -> dict:
    body: dict = {"query": "тест"}
    if namespaces is not None:
        body["scope"] = {"namespaces": namespaces}
    return body


def _retain(namespace: str) -> dict:
    return {"content": "факт", "scope": {"namespace": namespace}}


# ---------------- статические ключи не меняются при включённом IAM ----------------


def test_static_keys_still_work_with_iam_enabled(monkeypatch, signing_key):
    env = _Env()
    client = _install(monkeypatch, env, signing_key)
    legacy = _bearer("legacy-secret")
    assert (
        client.post("/api/brain/recall", json=_recall([NS_OTHER]), headers=legacy).status_code
        == 200
    )
    assert client.get("/api/brain/stats", headers=legacy).status_code == 200
    tp = _bearer("tp-secret")
    assert client.post("/api/brain/recall", json=_recall(["tp:x"]), headers=tp).status_code == 200
    assert (
        client.post("/api/brain/recall", json=_recall([NS_TENANT]), headers=tp).status_code == 403
    )
    # Неверный ключ и не-JWT — прежний 401 с прежним текстом.
    wrong = client.post("/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer("wrong"))
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == app_mod.AUTH_REQUIRED_DETAIL
    assert client.post("/api/brain/recall", json=_recall([NS_TENANT])).status_code == 401


def test_iam_enabled_without_static_keys_is_not_local_mode(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key, api_keys="", server_api_key="")
    assert client.post("/api/brain/recall", json=_recall([NS_TENANT])).status_code == 401
    ok = _bearer(_token(signing_key))
    assert (
        client.post("/api/brain/recall", json=_recall([NS_TENANT]), headers=ok).status_code == 200
    )


def test_iam_disabled_rejects_jwt(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key, iam_enabled=False)
    res = client.post(
        "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(_token(signing_key))
    )
    assert res.status_code == 401


# ---------------- валидный токен: свой tenant, чужой — 403 ----------------


def test_valid_token_reads_and_writes_own_tenant_namespaces(monkeypatch, signing_key):
    env = _Env()
    client = _install(monkeypatch, env, signing_key)
    headers = _bearer(_token(signing_key))
    assert (
        client.post("/api/brain/recall", json=_recall([NS_TENANT]), headers=headers).status_code
        == 200
    )
    assert env.read_namespaces[-1] == [NS_TENANT]
    assert (
        client.post(
            "/api/brain/recall", json=_recall([NS_TENANT, NS_TENANT_KB]), headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post("/api/brain/retain", json=_retain(NS_TENANT), headers=headers).status_code
        == 201
    )
    assert (
        client.post("/api/brain/retain", json=_retain(NS_TENANT_KB), headers=headers).status_code
        == 201
    )
    assert env.write_namespaces[-2:] == [NS_TENANT, NS_TENANT_KB]
    assert client.get("/api/brain/health", headers=headers).status_code == 200


def test_foreign_namespace_is_403(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key)
    headers = _bearer(_token(signing_key))
    res = client.post("/api/brain/recall", json=_recall([NS_OTHER]), headers=headers)
    assert res.status_code == 403
    assert NS_OTHER in res.json()["detail"]
    # Смешанный scope: одна чужая база — весь запрос отклонён.
    assert (
        client.post(
            "/api/brain/recall", json=_recall([NS_TENANT, NS_OTHER]), headers=headers
        ).status_code
        == 403
    )
    # Без scope — default namespace сервиса, IAM-токену он не выдан.
    assert client.post("/api/brain/recall", json=_recall(), headers=headers).status_code == 403
    # Префиксная ловушка: tenant:<uuid>0 — не поддерево tenant:<uuid>.
    assert (
        client.post(
            "/api/brain/recall", json=_recall([f"{NS_TENANT}0"]), headers=headers
        ).status_code
        == 403
    )
    # Глобальная статистика — только грантам без ограничений.
    assert client.get("/api/brain/stats", headers=headers).status_code == 403
    assert (
        client.post("/api/brain/retain", json=_retain(NS_OTHER), headers=headers).status_code == 403
    )


def test_tenants_scope_covers_every_tenant_namespace(monkeypatch, signing_key):
    """``memory:tenants`` — service account платформы (context-adapter ядра) видит
    поддерево ``tenant:*`` целиком; без него чужой tenant по-прежнему 403."""
    env = _Env()
    client = _install(monkeypatch, env, signing_key)
    platform = _bearer(
        _token(
            signing_key,
            scopes=[iam_mod.SCOPE_READ, iam_mod.SCOPE_WRITE, iam_mod.SCOPE_TENANTS],
            principal_type="service_account",
        )
    )
    assert (
        client.post("/api/brain/recall", json=_recall([NS_OTHER]), headers=platform).status_code
        == 200
    )
    assert (
        client.post("/api/brain/retain", json=_retain(NS_OTHER), headers=platform).status_code
        == 201
    )
    assert env.write_namespaces[-1] == NS_OTHER
    # Не «всё»: default namespace сервиса и чужие префиксы остаются закрытыми.
    assert (
        client.post("/api/brain/recall", json=_recall(["tp:x"]), headers=platform).status_code
        == 403
    )
    assert client.post("/api/brain/recall", json=_recall(), headers=platform).status_code == 403
    assert client.get("/api/brain/stats", headers=platform).status_code == 403
    # Только чтение: scope tenants не добавляет write сам по себе.
    reader = _bearer(_token(signing_key, scopes=[iam_mod.SCOPE_READ, iam_mod.SCOPE_TENANTS]))
    assert (
        client.post("/api/brain/retain", json=_retain(NS_OTHER), headers=reader).status_code == 403
    )


def test_memory_namespaces_claim_adds_grants(monkeypatch, signing_key):
    env = _Env()
    client = _install(monkeypatch, env, signing_key)
    token = _token(signing_key, extra_claims={"memory_namespaces": ["shared", "Bad Name", ""]})
    headers = _bearer(token)
    assert (
        client.post(
            "/api/brain/recall", json=_recall(["shared", "shared:faq"]), headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post("/api/brain/retain", json=_retain("shared"), headers=headers).status_code == 201
    )
    assert (
        client.post("/api/brain/recall", json=_recall(["sharedx"]), headers=headers).status_code
        == 403
    )
    # /api/memory/* авторизуется теми же грантами через тот же async authenticate.
    denied = client.get("/api/memory/observations", params={"namespace": NS_OTHER}, headers=headers)
    assert denied.status_code == 403


# ---------------- scopes ----------------


def test_without_write_scope_write_is_403(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key)
    headers = _bearer(_token(signing_key, scopes=[iam_mod.SCOPE_READ]))
    assert (
        client.post("/api/brain/recall", json=_recall([NS_TENANT]), headers=headers).status_code
        == 200
    )
    assert (
        client.post("/api/brain/retain", json=_retain(NS_TENANT), headers=headers).status_code
        == 403
    )
    assert (
        client.post(
            "/api/brain/facts",
            json={
                "natural_key": "f",
                "type": "fact",
                "title": "t",
                "scope": {"namespace": NS_TENANT},
            },
            headers=headers,
        ).status_code
        == 403
    )


def test_scope_ceiling_narrows_token_scopes(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key)
    headers = _bearer(_token(signing_key, scope_ceiling=[iam_mod.SCOPE_READ]))
    assert (
        client.post("/api/brain/recall", json=_recall([NS_TENANT]), headers=headers).status_code
        == 200
    )
    assert (
        client.post("/api/brain/retain", json=_retain(NS_TENANT), headers=headers).status_code
        == 403
    )


def test_token_without_memory_scopes_covers_nothing(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key)
    headers = _bearer(_token(signing_key, scopes=["control-plane:read"]))
    assert (
        client.post("/api/brain/recall", json=_recall([NS_TENANT]), headers=headers).status_code
        == 403
    )
    assert client.get("/api/brain/health", headers=headers).status_code == 200  # токен валиден


# ---------------- чужой audience / issuer, дефекты токена — 401 ----------------


@pytest.mark.parametrize(
    "kw",
    [
        {"audience": "control-plane"},
        {"issuer": "https://other.test/iam"},
        {"ttl_seconds": -600},
        {"drop_claims": ("tenant_id",)},
        {"algorithm": "HS256"},
    ],
)
def test_foreign_or_defective_token_is_401(monkeypatch, signing_key, kw):
    client = _install(monkeypatch, _Env(), signing_key)
    res = client.post(
        "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(_token(signing_key, **kw))
    )
    assert res.status_code == 401
    assert res.json()["detail"] == app_mod.AUTH_REQUIRED_DETAIL


def test_token_signed_by_unknown_key_is_401(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key)
    stranger = SigningKey.generate(key_id="stranger")
    res = client.post(
        "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(_token(stranger))
    )
    assert res.status_code == 401


# ---------------- JWKS недоступен / IAM не сконфигурирован — 503, не allow ----------------


def test_jwks_unreachable_is_503(monkeypatch, signing_key):
    def failing_keys(_settings):
        def handler(request):
            raise httpx.ConnectError("iam down", request=request)

        return JwksCache(JWKS_URL, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    client = _install(monkeypatch, _Env(), None)
    monkeypatch.setattr(iam_mod, "key_source_factory", failing_keys)
    res = client.post(
        "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(_token(signing_key))
    )
    assert res.status_code == 503
    assert res.json()["detail"] == app_mod.IAM_UNAVAILABLE_DETAIL
    # Статический ключ при этом продолжает работать.
    assert (
        client.post(
            "/api/brain/recall", json=_recall([NS_OTHER]), headers=_bearer("legacy-secret")
        ).status_code
        == 200
    )


def test_iam_enabled_without_issuer_or_jwks_is_503(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), None, iam_jwks_url="")
    assert (
        client.post(
            "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(_token(signing_key))
        ).status_code
        == 503
    )
    client = _install(monkeypatch, _Env(), signing_key, iam_issuer="")
    assert (
        client.post(
            "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(_token(signing_key))
        ).status_code
        == 503
    )


# ---------------- ПДн: маска без memory:pii ----------------


def test_pii_masked_without_pii_scope_and_full_with_it(monkeypatch, signing_key):
    client = _install(monkeypatch, _Env(), signing_key, pii_protection=True)
    masked = client.post(
        "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(_token(signing_key))
    ).json()
    assert masked["pii_masked"] is True
    assert "+7 999 123-45-67" not in masked["context"]
    full_token = _token(
        signing_key, scopes=[iam_mod.SCOPE_READ, iam_mod.SCOPE_WRITE, iam_mod.SCOPE_PII]
    )
    full = client.post(
        "/api/brain/recall", json=_recall([NS_TENANT]), headers=_bearer(full_token)
    ).json()
    assert "pii_masked" not in full
    assert "+7 999 123-45-67" in full["context"]


# ---------------- чистая проекция контекста в гранты ----------------


def test_grants_from_context_projection(signing_key):
    from platform_auth.context import TrustedAuthContext

    import jwt as pyjwt

    token = _token(
        signing_key, scopes=[iam_mod.SCOPE_READ], extra_claims={"memory_namespaces": ["shared"]}
    )
    claims = pyjwt.decode(token, options={"verify_signature": False}, audience=AUDIENCE)
    ctx = TrustedAuthContext.from_claims(claims, audience=AUDIENCE)
    grants = iam_mod.grants_from_context(ctx)
    assert isinstance(grants, CallerGrants)
    assert grants.name.startswith("iam:agent:")
    assert grants.pii is False
    assert grants.allows(NS_TENANT, write=False) and not grants.allows(NS_TENANT, write=True)
    assert grants.allows(NS_TENANT_KB, write=False)
    assert grants.allows("shared", write=False) and grants.allows("shared:x", write=False)
    assert not grants.allows(f"{NS_TENANT}0", write=False)
    assert not grants.allows("sharedx", write=False)
    assert not grants.allows(NS_OTHER, write=False)


# ---------------- маршруты ядра (амендмент MEM-ADR-020) ----------------


def _core_client(monkeypatch, signing_key, **overrides) -> tuple[TestClient, list]:
    from platform_memory.server import memory_api as api_mod

    calls: list = []

    def fake_reconcile(_settings, *, snapshot, namespace, scopes, actor):
        calls.append((namespace, actor))
        return {"opened": 0, "duplicate": False}

    monkeypatch.setattr(api_mod, "reconcile_snapshot", fake_reconcile)
    return _install(monkeypatch, _Env(), signing_key, **overrides), calls


_SNAPSHOT = {
    "source": "git:repo",
    "scope": "repo",
    "snapshotId": "repo@1",
    "observedAt": "2026-09-23T08:45:00+00:00",
    "entities": [],
    "relations": [],
    "namespace": NS_TENANT,
}


_KERNEL_SCOPES = [iam_mod.SCOPE_READ, iam_mod.SCOPE_WRITE, iam_mod.SCOPE_TENANTS]

_CORE_ROUTES = (
    ("post", "/api/memory/reconcile", {"json": _SNAPSHOT}),
    ("post", "/api/memory/packages", {"json": {"name": "p", "version": "1", "kinds": []}}),
    ("get", "/api/memory/packages", {}),
    ("get", "/api/memory/packages/p", {}),
    ("get", f"/api/memory/namespaces/{NS_TENANT}/kinds", {}),
    ("put", f"/api/memory/namespaces/{NS_TENANT}/kinds", {"json": {"strict": True}}),
)


def test_service_scope_admits_reconcile_and_tenant_token_is_403(monkeypatch, signing_key):
    client, calls = _core_client(monkeypatch, signing_key, core_only=True)
    plain = _bearer(_token(signing_key))  # read+write своего tenant, но не ядро
    resp = client.post("/api/memory/reconcile", json=_SNAPSHOT, headers=plain)
    assert resp.status_code == 403 and calls == []

    core = _bearer(
        _token(
            signing_key,
            scopes=[*_KERNEL_SCOPES, iam_mod.SCOPE_SERVICE],
            principal_type="service",
        )
    )
    resp = client.post("/api/memory/reconcile", json=_SNAPSHOT, headers=core)
    assert resp.status_code == 200, resp.text
    assert calls[0][0] == NS_TENANT and calls[0][1].startswith("iam:service:")


@pytest.mark.parametrize(("method", "path", "kw"), _CORE_ROUTES)
def test_write_tenants_without_service_is_403_on_core_routes(
    monkeypatch, signing_key, method, path, kw
):
    """Токен ядра до TASK-000317 (read+write+tenants без memory:service) — 403."""
    client, calls = _core_client(monkeypatch, signing_key, core_only=True)
    headers = _bearer(_token(signing_key, scopes=_KERNEL_SCOPES, principal_type="service"))
    resp = getattr(client, method)(path, headers=headers, **kw)
    assert resp.status_code == 403 and "identity ядра" in resp.json()["detail"]
    assert calls == []


@pytest.mark.parametrize(("method", "path", "kw"), _CORE_ROUTES)
def test_service_scope_passes_core_gate_on_every_core_route(
    monkeypatch, signing_key, method, path, kw
):
    from fastapi import HTTPException

    from platform_memory.server import memory_api as api_mod

    def registry_reached(_fn):
        raise HTTPException(status_code=418, detail="registry reached")

    monkeypatch.setattr(api_mod, "_with_registry", registry_reached)
    client, calls = _core_client(monkeypatch, signing_key, core_only=True)
    headers = _bearer(
        _token(
            signing_key,
            scopes=[*_KERNEL_SCOPES, iam_mod.SCOPE_SERVICE],
            principal_type="service",
        )
    )
    resp = getattr(client, method)(path, headers=headers, **kw)
    # Ворота ядра пройдены: reconcile выполнен, прочие маршруты дошли до реестра.
    assert resp.status_code in (200, 418), resp.text
    assert resp.status_code == 418 or calls


def test_core_routes_unrestricted_by_default(monkeypatch, signing_key):
    """Без CB_CORE_ONLY/CB_CORE_IDENTITIES поведение прежнее: решают гранты."""
    client, calls = _core_client(monkeypatch, signing_key)
    headers = _bearer(_token(signing_key, scopes=_KERNEL_SCOPES, principal_type="service"))
    assert client.post("/api/memory/reconcile", json=_SNAPSHOT, headers=headers).status_code == 200
    assert len(calls) == 1


def test_service_scope_outside_ceiling_is_ignored(monkeypatch, signing_key):
    client, calls = _core_client(monkeypatch, signing_key, core_only=True)
    headers = _bearer(
        _token(
            signing_key,
            scopes=[iam_mod.SCOPE_READ, iam_mod.SCOPE_WRITE, iam_mod.SCOPE_SERVICE],
            scope_ceiling=[iam_mod.SCOPE_READ, iam_mod.SCOPE_WRITE],
        )
    )
    assert client.post("/api/memory/reconcile", json=_SNAPSHOT, headers=headers).status_code == 403
    assert calls == []


def test_iam_principal_listed_in_core_identities(monkeypatch, signing_key):
    subject = uuid.uuid4()
    client, calls = _core_client(monkeypatch, signing_key, core_identities=f"iam:service:{subject}")
    listed = _bearer(_token(signing_key, subject=subject, principal_type="service"))
    assert client.post("/api/memory/reconcile", json=_SNAPSHOT, headers=listed).status_code == 200
    other = _bearer(_token(signing_key, principal_type="service"))
    assert client.post("/api/memory/reconcile", json=_SNAPSHOT, headers=other).status_code == 403
    assert len(calls) == 1
