"""HTTP-контракт доменных пакетов, reconcile и типизированного обхода (MEM-ADR-020).

Без БД: движок подменяется через модульные имена ``server.memory_api`` (как в
test_server_memory_api). Проверяются service scope регистрации пакетов, гранты
по namespace, коды ответов и серверная видимость (allowed_scopes не от клиента).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from platform_memory.core import timing
from platform_memory.core.kinds import PackError, UnknownKindError, UnknownRelationError
from platform_memory.domain.reconcile import SnapshotError, StaleSnapshotError
from platform_memory.domain.registry import PackConflictError, PackNotFoundError
from platform_memory.server import app as app_mod
from platform_memory.server import iam
from platform_memory.server import memory_api as api_mod
from platform_memory.server.auth import CallerGrants, NamespaceGrant

pytestmark = pytest.mark.unit

NS = "tp:tenant:aaaa"

_KEYS = [
    {
        "name": "tenant-key",
        "key": "tenant-secret",
        "grants": [{"prefix": "tp:", "read": True, "write": True}],
    },
    {
        "name": "svc-key",
        "key": "svc-secret",
        "service": True,
        "grants": [{"prefix": "tp:", "read": True, "write": False}],
    },
    {
        "name": "admin-all",
        "key": "admin-secret",
        "grants": [{"prefix": "", "read": True, "write": True}],
    },
]

PACK = {"name": "tracker", "version": "1.0.0", "kinds": [{"kind": "issue"}]}


class _FakeConn:
    def close(self):
        pass


class _FakeRegistry:
    status = "created"
    error: Exception | None = None

    def __init__(self, env):
        self.env = env

    def register(self, payload, *, registered_by=""):
        if self.error:
            raise self.error
        self.env["registered"].append((payload, registered_by))
        return {"status": self.status, "pack": payload}

    def list_packs(self):
        return [{"name": "default", "versions": ["1"], "latest": "1", "builtin": True}]

    def get(self, name, version=""):
        raise PackNotFoundError(f"Пакет {name} не найден")

    class _Payload:
        def to_payload(self):
            return {}

    def get_settings(self, ns):
        return self._Payload()

    def put_settings(self, ns, **_kw):
        return self._Payload()

    def catalog_for(self, ns):
        return self._Payload()


@pytest.fixture
def env(monkeypatch):
    state: dict = {"registered": [], "reconciled": [], "typed": []}
    settings = app_mod.get_settings().model_copy(
        update={
            "api_keys": json.dumps(_KEYS),
            "server_api_key": "",
            "server_api_keys_pii": "",
            "pii_protection": False,
            "iam_enabled": False,
            "policy_enabled": False,
        }
    )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(api_mod.dbmod, "connect", lambda *_a, **_k: _FakeConn())
    registry = _FakeRegistry(state)
    monkeypatch.setattr(api_mod, "open_registry", lambda conn, s: registry)
    state["registry"] = registry

    def fake_reconcile(settings, *, snapshot, namespace, scopes, actor):
        if state.get("reconcile_error"):
            raise state["reconcile_error"]
        state["reconciled"].append((namespace, snapshot, scopes, actor))
        return {"opened": 1, "closed": 0, "unchanged": 0, "duplicate": False}

    def fake_typed(settings, payload):
        state["typed"].append(payload)
        return {"sections": [], "facts": [], "used": {}, "trace_id": "ctx-1"}

    monkeypatch.setattr(api_mod, "reconcile_snapshot", fake_reconcile)
    monkeypatch.setattr(api_mod, "compile_typed_context", fake_typed)
    state["client"] = TestClient(app_mod.app)
    return state


def _h(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestPackagesServiceScope:
    def test_tenant_key_cannot_register(self, env):
        resp = env["client"].post("/api/memory/packages", json=PACK, headers=_h("tenant-secret"))
        assert resp.status_code == 403
        assert env["registered"] == []

    def test_numeric_version_accepted(self, env):
        resp = env["client"].post(
            "/api/memory/packages", json={**PACK, "version": 1}, headers=_h("svc-secret")
        )
        assert resp.status_code == 201

    @pytest.mark.parametrize("key", ["svc-secret", "admin-secret"])
    def test_service_and_full_access_register(self, env, key):
        resp = env["client"].post("/api/memory/packages", json=PACK, headers=_h(key))
        assert resp.status_code == 201
        assert env["registered"][0][0]["name"] == "tracker"

    def test_same_version_again_is_200_and_conflict_409(self, env):
        env["registry"].status = "unchanged"
        resp = env["client"].post("/api/memory/packages", json=PACK, headers=_h("svc-secret"))
        assert resp.status_code == 200
        env["registry"].error = PackConflictError("иммутабельна")
        resp = env["client"].post("/api/memory/packages", json=PACK, headers=_h("svc-secret"))
        assert resp.status_code == 409
        env["registry"].error = PackError("плохой пакет")
        resp = env["client"].post("/api/memory/packages", json=PACK, headers=_h("svc-secret"))
        assert resp.status_code == 400

    def test_read_is_open_to_any_caller(self, env):
        resp = env["client"].get("/api/memory/packages", headers=_h("tenant-secret"))
        assert resp.status_code == 200 and resp.json()["packages"][0]["name"] == "default"
        resp = env["client"].get("/api/memory/packages/nope", headers=_h("tenant-secret"))
        assert resp.status_code == 404

    def test_iam_scope_maps_to_service(self):
        grants = CallerGrants(name="x", grants=(NamespaceGrant(prefix="tenant:t:"),), service=True)
        app_mod._authorize_service(app_mod.Principal(grants=grants))
        assert iam.SCOPE_SERVICE == "memory:service"


SNAP = {
    "pack": "software-delivery@1",
    "source": "git:control-plane",
    "scope": "control-plane",
    "snapshotId": "control-plane@abc",
    "observedAt": "2026-09-23T08:45:00+00:00",
    "entities": [],
    "relations": [],
}


class TestReconcileRoute:
    def test_write_grant_required(self, env):
        resp = env["client"].post(
            "/api/memory/reconcile",
            params={"namespace": "other"},
            json=SNAP,
            headers=_h("tenant-secret"),
        )
        assert resp.status_code == 403
        resp = env["client"].post(
            "/api/memory/reconcile",
            json={**SNAP, "namespace": "other"},
            headers=_h("tenant-secret"),
        )
        assert resp.status_code == 403

    def test_flat_document_passed_as_is(self, env):
        body = {**SNAP, "namespace": NS, "scopes": ["workspace:w1"]}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 200 and resp.json()["opened"] == 1
        namespace, snapshot, scopes, _ = env["reconciled"][0]
        assert namespace == NS and scopes == ["workspace:w1"]
        assert snapshot == SNAP  # документ без namespace/scopes, scope — строка снимка

    def test_namespace_query_and_body_must_agree(self, env):
        resp = env["client"].post(
            "/api/memory/reconcile",
            params={"namespace": NS},
            json={**SNAP, "namespace": "tp:other"},
            headers=_h("tenant-secret"),
        )
        assert resp.status_code == 400

    def test_ok_and_error_mapping(self, env):
        body = {**SNAP, "namespace": NS}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 200 and resp.json()["opened"] == 1
        assert env["reconciled"][0][0] == NS
        for exc, code in (
            (StaleSnapshotError("старше"), 409),
            (SnapshotError("плохой"), 400),
            (UnknownKindError("вид"), 422),
            (UnknownRelationError("связь"), 422),
        ):
            env["reconcile_error"] = exc
            resp = env["client"].post(
                "/api/memory/reconcile", json=body, headers=_h("tenant-secret")
            )
            assert resp.status_code == code, exc


class TestTypedContextRoute:
    def test_client_allowed_scopes_narrow(self, env):
        body = {
            "anchors": [{"value": "PROJ-1"}],
            "traverse": [{"relation": "blocks"}],
            "allowedScopes": ["principal:evil"],
            "scope": {"namespace": NS},
        }
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 200 and resp.json()["trace_id"] == "ctx-1"
        payload = env["typed"][0]
        assert payload["namespaces"] == [NS]
        # Статический ключ без ограничения: клиентское сужение и есть видимость
        # (MEM-ADR-021) — пересечение с «всё», а не игнор.
        assert payload["allowed_scopes"] == ["principal:evil"]
        assert "allowedScopes" not in payload

    def test_read_grant_required(self, env):
        body = {"anchors": [{"value": "x"}], "scope": {"namespace": "other"}}
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 403

    def test_server_timing_off_by_default(self, env):
        body = {"anchors": [{"value": "PROJ-1"}], "scope": {"namespace": NS}}
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 200 and "server-timing" not in resp.headers

    def test_server_timing_phases_of_whole_request(self, env, monkeypatch):
        """CB_SERVER_TIMING: фазы HTTP-слоя и движка (typed.*) в Server-Timing и в логе."""
        settings = app_mod.get_settings().model_copy(update={"server_timing": True})
        monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

        def fake_typed(settings, payload):
            engine = timing.Phases()
            engine.add("anchors", 0.002)
            timing.merge(engine, "typed.")
            return {"sections": [], "facts": [], "used": {}, "trace_id": "ctx-1"}

        monkeypatch.setattr(api_mod, "compile_typed_context", fake_typed)
        body = {"anchors": [{"value": "PROJ-1"}], "scope": {"namespace": NS}}
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 200
        names = [part.split(";")[0].strip() for part in resp.headers["server-timing"].split(",")]
        assert set(names) == {
            "auth",
            "visibility",
            "typed",
            "typed.anchors",
            "protect",
            "total",
        }, names
        assert "typed.anchors;dur=2.0" in resp.headers["server-timing"]


class TestCoreOnlyRoutes:
    """Амендмент MEM-ADR-020: reconcile, пакеты и виды namespace — только identity ядра."""

    ROUTES = (
        ("post", "/api/memory/reconcile", {"json": {**SNAP, "namespace": NS}}),
        ("post", "/api/memory/packages", {"json": PACK}),
        ("get", "/api/memory/packages", {}),
        ("get", "/api/memory/packages/nope", {}),
        ("get", f"/api/memory/namespaces/{NS}/kinds", {}),
        ("put", f"/api/memory/namespaces/{NS}/kinds", {"json": {"strict": True}}),
    )

    @staticmethod
    def _restrict(monkeypatch, **update):
        settings = app_mod.get_settings().model_copy(update=update)
        monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    @pytest.mark.parametrize(("method", "path", "kw"), ROUTES)
    def test_grant_is_not_enough(self, env, monkeypatch, method, path, kw):
        self._restrict(monkeypatch, core_identities="other")
        # admin-all: запись во все базы и неявное право регистрации пакетов, но без
        # явного "service": true — не ядро.
        for key in ("tenant-secret", "admin-secret"):
            resp = getattr(env["client"], method)(path, headers=_h(key), **kw)
            assert resp.status_code == 403, (key, resp.text)
            assert "identity ядра" in resp.json()["detail"]
        assert env["registered"] == [] and env["reconciled"] == []

    @pytest.mark.parametrize(("method", "path", "kw"), ROUTES)
    def test_listed_identity_passes(self, env, monkeypatch, method, path, kw):
        self._restrict(monkeypatch, core_identities=" admin-all , other ")
        resp = getattr(env["client"], method)(path, headers=_h("admin-secret"), **kw)
        assert resp.status_code != 403, resp.text

    @pytest.mark.parametrize(("method", "path", "kw"), ROUTES)
    def test_service_key_is_core_without_list(self, env, monkeypatch, method, path, kw):
        # Ключ реестра с "service": true — identity ядра и при CB_CORE_ONLY без списка.
        self._restrict(monkeypatch, core_only=True)
        resp = getattr(env["client"], method)(path, headers=_h("svc-secret"), **kw)
        assert "identity ядра" not in resp.text

    def test_core_identity_still_needs_namespace_grant(self, env, monkeypatch):
        # Ядро — дополнительное условие, а не замена грантов: у svc-key нет записи.
        self._restrict(monkeypatch, core_only=True)
        resp = env["client"].post(
            "/api/memory/reconcile", json={**SNAP, "namespace": NS}, headers=_h("svc-secret")
        )
        assert resp.status_code == 403 and "namespace" in resp.json()["detail"]
        resp = env["client"].post("/api/memory/packages", json=PACK, headers=_h("svc-secret"))
        assert resp.status_code == 201

    def test_core_only_without_list_admits_only_service_scope(self, env, monkeypatch):
        self._restrict(monkeypatch, core_only=True)
        resp = env["client"].get("/api/memory/packages", headers=_h("admin-secret"))
        assert resp.status_code == 403
        core = CallerGrants(
            name="iam:service:cp", grants=(NamespaceGrant(prefix=""),), service=True
        )
        app_mod._authorize_core(app_mod.Principal(grants=core))
        tenant = CallerGrants(name="iam:service:cp", grants=(NamespaceGrant(prefix=""),))
        with pytest.raises(app_mod.HTTPException) as exc:
            app_mod._authorize_core(app_mod.Principal(grants=tenant))
        assert exc.value.status_code == 403
        with pytest.raises(app_mod.HTTPException) as exc:
            app_mod._authorize_core(app_mod.Principal())  # локальный режим — fail closed
        assert exc.value.status_code == 403
        assert not hasattr(iam, "SCOPE_CORE")

    def test_other_memory_routes_unaffected(self, env, monkeypatch):
        self._restrict(monkeypatch, core_identities="svc-key")
        body = {"anchors": [{"value": "PROJ-1"}], "scope": {"namespace": NS}}
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 200

    def test_restriction_off_by_default(self, env):
        assert app_mod.core_identities(app_mod.get_settings()) is None
        resp = env["client"].get("/api/memory/packages", headers=_h("tenant-secret"))
        assert resp.status_code == 200
