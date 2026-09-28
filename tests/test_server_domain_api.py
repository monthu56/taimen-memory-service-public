"""HTTP-контракт доменных пакетов, reconcile и типизированного обхода (MEM-ADR-020).

Без БД: движок подменяется через модульные имена ``server.memory_api`` (как в
test_server_memory_api). Проверяются service scope регистрации пакетов, гранты
по namespace, коды ответов и серверная видимость (allowed_scopes не от клиента).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from platform_memory.context.typed import TypedContextRequest
from platform_memory.core import timing
from platform_memory.core.kinds import PackError, UnknownKindError, UnknownRelationError
from platform_memory.domain.reconcile import (
    SnapshotError,
    SnapshotStateMismatch,
    StaleSnapshotError,
)
from platform_memory.domain.registry import (
    PackConflictError,
    PackNotFoundError,
    PackScopeConflictError,
)
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
    conn = _FakeConn()

    def __init__(self, env):
        self.env = env

    def register(self, payload, *, registered_by=""):
        if self.error:
            raise self.error
        self.env["registered"].append((payload, registered_by))
        return {"status": self.status, "pack": payload}

    def register_tenant(self, payload, *, owner, registered_by=""):
        if self.error:
            raise self.error
        self.env["registered"].append((payload, registered_by, owner))
        return {"status": self.status, "pack": payload}

    def list_packs(self, namespace=None):
        self.env["listed"].append(namespace)
        return [{"name": "default", "versions": ["1"], "latest": "1", "builtin": True}]

    def get(self, name, version=""):
        raise PackNotFoundError(f"Пакет {name} не найден")

    def get_tenant(self, name, version="", *, namespace):
        self.env["tenant_get"].append((name, version, namespace))
        raise PackNotFoundError(f"Пакет tenant:{name} не найден")

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
    state: dict = {
        "registered": [],
        "reconciled": [],
        "typed": [],
        "entities": [],
        "reindexed": [],
        "listed": [],
        "tenant_get": [],
    }
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

    def fake_reconcile(settings, *, snapshot, namespace, scopes, actor, **state_kw):
        if state.get("reconcile_error"):
            raise state["reconcile_error"]
        state["reconciled"].append((namespace, snapshot, scopes, actor))
        state.setdefault("reconcile_state", []).append(state_kw)
        return {"opened": 1, "closed": 0, "unchanged": 0, "duplicate": False}

    def fake_typed(settings, payload):
        state["typed"].append(payload)
        return {"sections": [], "facts": [], "used": {}, "trace_id": "ctx-1"}

    def fake_reindex(settings, conn, namespace, catalog, embedder):
        if state.get("reindex_error"):
            raise state["reindex_error"]
        state["reindexed"].append(namespace)
        return {"indexed": 2, "updated": 0, "removed": 1, "unchanged": 0}

    monkeypatch.setattr(api_mod, "reconcile_snapshot", fake_reconcile)

    def fake_entities(settings, payload):
        state["entities"].append(payload)
        return {"items": [], "nextCursor": None, "namespaces": payload["namespaces"]}

    monkeypatch.setattr(api_mod, "compile_typed_context", fake_typed)
    monkeypatch.setattr(api_mod, "query_entities", fake_entities)
    monkeypatch.setattr(api_mod, "reindex_namespace", fake_reindex)
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


class TestEntitiesQueryRoute:
    """K030: перечень сущностей — namespaces и scopes видимости задаёт сервер."""

    def _post(self, env, body, key="tenant-secret"):
        return env["client"].post("/api/memory/entities:query", json=body, headers=_h(key))

    def test_namespaces_and_server_visibility(self, env):
        body = {
            "namespaces": [NS],
            "kinds": ["license"],
            "where": [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}],
            "asOf": "2026-09-01T00:00:00Z",
            "limit": 50,
            "cursor": "abc",
            "allowedScopes": ["workspace:w1"],
        }
        resp = self._post(env, body)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"items": [], "nextCursor": None, "namespaces": [NS]}
        (payload,) = env["entities"]
        assert payload["namespaces"] == [NS]
        assert payload["allowed_scopes"] == ["workspace:w1"]
        assert "allowedScopes" not in payload and "scope" not in payload
        assert payload["where"] == body["where"]
        assert (payload["as_of"], payload["limit"], payload["cursor"]) == (
            "2026-09-01T00:00:00Z",
            50,
            "abc",
        )

    def test_scope_like_typed(self, env):
        resp = self._post(env, {"kinds": ["license"], "scope": {"namespaces": [NS]}})
        assert resp.status_code == 200
        assert env["entities"][0]["namespaces"] == [NS]

    def test_namespaces_and_scope_together_rejected(self, env):
        body = {"kinds": ["license"], "namespaces": [NS], "scope": {"namespace": NS}}
        assert self._post(env, body).status_code == 400

    def test_read_grant_required(self, env):
        assert self._post(env, {"kinds": ["x"], "namespaces": ["other"]}).status_code == 403

    @pytest.mark.parametrize(
        "extra",
        [
            {"kinds": []},
            {"kinds": [f"k{i}" for i in range(21)]},
            {"kinds": ["bad kind"]},
            {"limit": 0},
            {"limit": 501},
            {"where": [{"attr": "a", "op": "like", "value": "x"}]},
        ],
    )
    def test_schema_limits(self, env, extra):
        body = {"kinds": ["license"], "namespaces": [NS], **extra}
        assert self._post(env, body).status_code == 422
        assert env["entities"] == []

    def test_engine_value_error_is_400(self, env, monkeypatch):
        def boom(settings, payload):
            raise ValueError("cursor: не курсор перечня")

        monkeypatch.setattr(api_mod, "query_entities", boom)
        assert self._post(env, {"kinds": ["license"], "namespaces": [NS]}).status_code == 400


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


class TestContractBeforeImplementation:
    """Амендмент MEM-ADR-020 2026-09-28: поля объявлены в OpenAPI и исполняются —
    сверка по состоянию (K004), ``searchable`` (K005), ``where`` (K006), пакеты
    арендатора (K007); прежний вход проходит как раньше."""

    def test_reconcile_state_fields_reach_engine(self, env):
        """K004 снял 501: dryRun и expectedState уходят в движок, а не в документ снимка."""
        body = {**SNAP, "namespace": NS, "dryRun": True, "expectedState": "st1-abc"}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 200
        assert env["reconciled"][0][1] == SNAP
        assert env["reconcile_state"] == [{"dry_run": True, "expected_state": "st1-abc"}]

    def test_reconcile_state_defaults(self, env):
        body = {**SNAP, "namespace": NS}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 200
        assert env["reconcile_state"] == [{"dry_run": False, "expected_state": None}]

    def test_reconcile_state_needs_write_grant_first(self, env):
        body = {**SNAP, "namespace": "other", "dryRun": True}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 403
        assert env["reconciled"] == []

    def test_snapshot_stale_409_detail(self, env):
        env["reconcile_error"] = SnapshotStateMismatch("st1-old", "st1-new")
        body = {**SNAP, "namespace": NS, "expectedState": "st1-old"}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "snapshot_stale" and detail["message"]
        assert (detail["expectedState"], detail["stateToken"]) == ("st1-old", "st1-new")

    def test_expected_state_too_long_422(self, env):
        body = {**SNAP, "namespace": NS, "expectedState": "x" * 129}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 422
        assert env["reconciled"] == []

    def test_reconcile_dry_run_false_passes_snapshot_as_is(self, env):
        body = {**SNAP, "namespace": NS, "dryRun": False}
        resp = env["client"].post("/api/memory/reconcile", json=body, headers=_h("tenant-secret"))
        assert resp.status_code == 200
        assert env["reconciled"][0][1] == SNAP  # служебные поля в документ не попадают

    def test_searchable_kind_registered_as_is(self, env):
        """K005 снял 501: searchable уходит в реестр как есть (валидирует core.kinds)."""
        pack = {**PACK, "kinds": [{"kind": "issue", "searchable": {"fields": ["title"]}}]}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("svc-secret"))
        assert resp.status_code == 201
        assert env["registered"][0][0] == pack

    def test_invalid_searchable_is_400(self, env):
        env["registry"].error = PackError("kinds[0].searchable.fields: имена повторяются")
        pack = {**PACK, "kinds": [{"kind": "issue", "searchable": {"fields": ["a", "a"]}}]}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("svc-secret"))
        assert resp.status_code == 400

    def test_pack_payload_unchanged(self, env):
        """Объявленные в схеме поля не дописываются в пакет: хэш версии прежний."""
        pack = {**PACK, "relations": [{"relation": "blocks", "extraKey": 1}], "x-note": "n"}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("svc-secret"))
        assert resp.status_code == 201
        assert env["registered"][0][0] == pack
        resp = env["client"].post(
            "/api/memory/packages", json={**PACK, "scope": "common"}, headers=_h("svc-secret")
        )
        assert resp.status_code == 201

    def test_pack_old_error_codes_kept(self, env):
        """Прежние поля валидирует движок: мусор в kinds — не 422 pydantic."""
        env["registry"].error = PackError("kinds[0]: ожидается объект")
        pack = {**PACK, "kinds": [1]}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("svc-secret"))
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            (
                {"where": [{"attr": "okpd2", "op": "prefix", "value": "62.01"}]},
                {"where": [{"attr": "okpd2", "op": "prefix", "value": "62.01"}]},
            ),
            (
                {"where": [{"attr": "ktru", "op": "exists"}]},
                {"where": [{"attr": "ktru", "op": "exists"}]},
            ),
            (
                {"traverse": [{"relation": "performs", "where": [{"attr": "a", "op": "exists"}]}]},
                {"traverse": [{"relation": "performs", "where": [{"attr": "a", "op": "exists"}]}]},
            ),
        ],
    )
    def test_typed_where_reaches_engine(self, env, extra, expected):
        """K006: where исполняется движком — 501 снят, условия доходят как прислано."""
        body = {"anchors": [{"value": "PROJ-1"}], "scope": {"namespace": NS}, **extra}
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 200
        (payload,) = env["typed"]
        assert {k: payload[k] for k in expected} == expected

    @pytest.mark.parametrize(
        "extra",
        [
            {"where": [{"attr": "okpd2", "op": "prefix", "value": "62."}]},
            {"where": [{"attr": "validUntil", "op": "lte", "value": "вчера"}]},
            {"traverse": [{"relation": "holds", "where": [{"attr": "a", "op": "like"}]}]},
        ],
    )
    def test_typed_invalid_where_value_is_400(self, env, monkeypatch, extra):
        """Значение условия (и форма условия шага) проверяет движок: 400."""
        real = TypedContextRequest.from_payload
        monkeypatch.setattr(
            api_mod, "compile_typed_context", lambda settings, payload: real(payload)
        )
        body = {"anchors": [{"value": "PROJ-1"}], "scope": {"namespace": NS}, **extra}
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 400
        assert "where" in resp.json()["detail"]

    def test_typed_invalid_where_is_422(self, env):
        body = {
            "anchors": [{"value": "PROJ-1"}],
            "scope": {"namespace": NS},
            "where": [{"attr": "okpd2", "op": "like", "value": "62"}],
        }
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 422

    def test_typed_payload_unchanged(self, env):
        """Объявленные поля без значения не подменяют camelCase-варианты движка."""
        body = {
            "anchors": ["PROJ-1"],
            "traverse": [{"relation": "blocks", "from": "previous"}],
            "asOf": "2026-09-01T00:00:00Z",
            "allowSemantic": True,
            "scope": {"namespace": NS},
        }
        resp = env["client"].post(
            "/api/memory/context/typed", json=body, headers=_h("tenant-secret")
        )
        assert resp.status_code == 200
        payload = dict(env["typed"][0])
        payload.pop("namespaces")
        payload.pop("allowed_scopes")
        assert payload == {
            "anchors": ["PROJ-1"],
            "traverse": [{"relation": "blocks", "from": "previous"}],
            "asOf": "2026-09-01T00:00:00Z",
            "allowSemantic": True,
        }

    def test_searchable_still_needs_service_scope(self, env):
        pack = {**PACK, "kinds": [{"kind": "issue", "searchable": {"fields": ["title"]}}]}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("tenant-secret"))
        assert resp.status_code == 403


class TestNamespaceKindsReindex:
    """PUT …/kinds переиндексирует сущности для поиска по смыслу (K005)."""

    def test_put_reports_reindex(self, env):
        resp = env["client"].put(
            f"/api/memory/namespaces/{NS}/kinds",
            json={"strict": False, "packages": ["tracker"]},
            headers=_h("tenant-secret"),
        )
        assert resp.status_code == 200
        assert resp.json()["reindex"] == {"indexed": 2, "updated": 0, "removed": 1, "unchanged": 0}
        assert env["reindexed"] == [NS]

    def test_reindex_error_keeps_settings(self, env):
        env["reindex_error"] = RuntimeError("gateway недоступен")
        resp = env["client"].put(
            f"/api/memory/namespaces/{NS}/kinds",
            json={"strict": False, "packages": ["tracker"]},
            headers=_h("tenant-secret"),
        )
        assert resp.status_code == 200
        assert resp.json()["reindex"] == {"error": "gateway недоступен"}


class TestTenantPackages:
    """K007: пакет арендатора регистрирует владелец namespace, а не service scope."""

    TENANT_PACK = {**PACK, "scope": "tenant", "namespace": NS}

    def test_write_grant_registers_without_service_scope(self, env):
        resp = env["client"].post(
            "/api/memory/packages", json=self.TENANT_PACK, headers=_h("tenant-secret")
        )
        assert resp.status_code == 201, resp.text
        payload, label, owner = env["registered"][0]
        assert owner == NS and label == "tenant-key"
        # scope/namespace — адрес хранения, в спецификацию пакета (и хэш) не входят.
        assert payload == PACK

    def test_foreign_namespace_403(self, env):
        pack = {**self.TENANT_PACK, "namespace": "other:tenant"}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("tenant-secret"))
        assert resp.status_code == 403
        # Service scope без записи в namespace — тоже нет: право — у владельца базы.
        resp = env["client"].post(
            "/api/memory/packages", json=self.TENANT_PACK, headers=_h("svc-secret")
        )
        assert resp.status_code == 403
        assert env["registered"] == []

    def test_namespace_required_and_only_for_tenant(self, env):
        pack = {**PACK, "scope": "tenant"}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("tenant-secret"))
        assert resp.status_code == 400
        pack = {**PACK, "namespace": NS}
        resp = env["client"].post("/api/memory/packages", json=pack, headers=_h("svc-secret"))
        assert resp.status_code == 400
        assert env["registered"] == []

    @pytest.mark.parametrize("code", ["pack_name_conflict", "kind_conflict", "relation_conflict"])
    def test_conflict_with_common_is_409_with_code(self, env, code):
        env["registry"].error = PackScopeConflictError(code, "совпадает с общим")
        resp = env["client"].post(
            "/api/memory/packages", json=self.TENANT_PACK, headers=_h("tenant-secret")
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == {"code": code, "message": "совпадает с общим"}

    def test_immutability_conflict_keeps_string_detail(self, env):
        env["registry"].error = PackConflictError("иммутабельна")
        resp = env["client"].post(
            "/api/memory/packages", json=self.TENANT_PACK, headers=_h("tenant-secret")
        )
        assert resp.status_code == 409 and resp.json()["detail"] == "иммутабельна"

    def test_list_with_namespace_needs_read_grant(self, env):
        resp = env["client"].get(
            "/api/memory/packages", params={"namespace": NS}, headers=_h("tenant-secret")
        )
        assert resp.status_code == 200 and env["listed"] == [NS]
        resp = env["client"].get(
            "/api/memory/packages", params={"namespace": "other"}, headers=_h("tenant-secret")
        )
        assert resp.status_code == 403
        resp = env["client"].get("/api/memory/packages", headers=_h("tenant-secret"))
        assert resp.status_code == 200 and env["listed"] == [NS, None]

    def test_get_tenant_pack(self, env):
        resp = env["client"].get(
            "/api/memory/packages/tenant:fleet",
            params={"namespace": NS, "version": "2"},
            headers=_h("tenant-secret"),
        )
        assert resp.status_code == 404
        assert env["tenant_get"] == [("fleet", "2", NS)]
        # Без namespace пакет арендатора не ищется: 404, а не общий пакет «tenant:…».
        resp = env["client"].get("/api/memory/packages/tenant:fleet", headers=_h("tenant-secret"))
        assert resp.status_code == 404 and len(env["tenant_get"]) == 1
        resp = env["client"].get(
            "/api/memory/packages/tenant:fleet",
            params={"namespace": "other"},
            headers=_h("tenant-secret"),
        )
        assert resp.status_code == 403
