"""Пакеты онтологии арендатора на реальной БД (K007; MEM-ADR-020, амендмент 2026-09-28).

Приёмка задачи: пакет арендатора A не виден и не включается у B; имя пакета, вида
или связи, совпавшее с общим пакетом, — отказ при регистрации; сверка и
типизированный обход работают по виду арендатора. Запуск — см. conftest
(CB_TEST_DATABASE_URL).
"""

from __future__ import annotations

import json

import pytest

from platform_memory.context.typed import compile_typed_context
from platform_memory.core.kinds import UnknownKindError
from platform_memory.domain.reconcile import SnapshotError, reconcile
from platform_memory.domain.registry import (
    PackConflictError,
    PackNotFoundError,
    PackScopeConflictError,
    attach_kind_guard,
    open_registry,
)

pytestmark = pytest.mark.integration

OWNER_A = "tenant:a"
NS_A = "tenant:a:ws:w1"
OWNER_B = "tenant:b"

# Общий пакет: регистрирует администратор платформы.
TRACKER = {
    "name": "tracker",
    "version": "1",
    "kinds": [{"kind": "issue", "kindAliases": ["ticket"]}, {"kind": "service"}],
    "relations": [{"relation": "blocks", "fromKinds": ["issue"], "toKinds": ["issue"]}],
}

# Пакет арендатора: собственный вид и связь на вид общего пакета.
FLEET = {
    "name": "fleet",
    "version": "1",
    "kinds": [
        {
            "kind": "vehicle",
            "kindAliases": ["car"],
            "attributes": {"type": "object", "properties": {"plate": {"type": "string"}}},
        }
    ],
    "relations": [{"relation": "serves", "fromKinds": ["vehicle"], "toKinds": ["service"]}],
}

T1 = "2026-09-01T00:00:00Z"


@pytest.fixture
def registry(stores, settings):
    reg = open_registry(stores[0], settings)
    reg.ensure_schema()
    reg.register(TRACKER)
    return reg


def _with(pack: dict, **update) -> dict:
    return {**pack, **update}


class TestVisibility:
    def test_owner_and_below_see_pack_other_tenant_does_not(self, registry):
        assert registry.register_tenant(FLEET, owner=OWNER_A)["status"] == "created"
        assert registry.register_tenant(FLEET, owner=OWNER_A)["status"] == "unchanged"

        for ns in (OWNER_A, NS_A):
            pack = registry.get_tenant("fleet", namespace=ns)
            assert (pack.owner, pack.ref) == (OWNER_A, "tenant:fleet@1")
            listed = [p for p in registry.list_packs(ns) if p["scope"] == "tenant"]
            assert listed == [
                {
                    "name": "fleet",
                    "versions": ["1"],
                    "latest": "1",
                    "builtin": False,
                    "scope": "tenant",
                    "namespace": OWNER_A,
                    "ref": "tenant:fleet",
                }
            ]

        # Чужой арендатор и namespace с тем же началом строки, но не под владельцем.
        for ns in (OWNER_B, "tenant:ab", "tenant"):
            with pytest.raises(PackNotFoundError):
                registry.get_tenant("fleet", namespace=ns)
            assert all(p["scope"] == "common" for p in registry.list_packs(ns))
            with pytest.raises(PackNotFoundError):
                registry.put_settings(ns, strict=False, packages=["tenant:fleet"])
        # Без namespace — только общие пакеты, как раньше.
        assert [p["name"] for p in registry.list_packs()] == ["default", "tracker"]

    def test_names_are_independent_per_owner(self, registry):
        registry.register_tenant(FLEET, owner=OWNER_A)
        other = _with(FLEET, kinds=[{"kind": "vehicle"}, {"kind": "trailer"}])
        assert registry.register_tenant(other, owner=OWNER_B)["status"] == "created"
        # Версия иммутабельна в пределах (владелец, имя).
        with pytest.raises(PackConflictError):
            registry.register_tenant(other, owner=OWNER_A)
        assert [k.kind for k in registry.get_tenant("fleet", namespace=OWNER_B).kinds] == [
            "vehicle",
            "trailer",
        ]
        assert [k.kind for k in registry.get_tenant("fleet", namespace=NS_A).kinds] == ["vehicle"]

    def test_nearest_owner_wins(self, registry):
        registry.register_tenant(FLEET, owner=OWNER_A)
        ws = _with(FLEET, version="7", kinds=[{"kind": "drone"}])
        registry.register_tenant(ws, owner=NS_A)
        assert registry.get_tenant("fleet", namespace=NS_A).ref == "tenant:fleet@7"
        assert registry.get_tenant("fleet", namespace=OWNER_A).ref == "tenant:fleet@1"
        # Версия ближайшего владельца: версии дальнего через него не видны.
        with pytest.raises(PackNotFoundError):
            registry.get_tenant("fleet", "1", namespace=NS_A)


class TestConflictsWithCommon:
    @pytest.mark.parametrize(
        ("pack", "code"),
        [
            (_with(FLEET, name="tracker"), "pack_name_conflict"),
            (_with(FLEET, name="default"), "pack_name_conflict"),
            (_with(FLEET, kinds=[{"kind": "issue"}]), "kind_conflict"),
            (_with(FLEET, kinds=[{"kind": "vehicle", "kindAliases": ["ticket"]}]), "kind_conflict"),
            (_with(FLEET, kinds=[{"kind": "ticket"}]), "kind_conflict"),
            (_with(FLEET, kinds=[{"kind": "contract"}]), "kind_conflict"),  # вид default
            (_with(FLEET, kinds=[{"kind": "document"}]), "kind_conflict"),  # базовый вид
            (_with(FLEET, relations=[{"relation": "blocks"}]), "relation_conflict"),
            (_with(FLEET, relations=[{"relation": "mentions"}]), "relation_conflict"),
        ],
    )
    def test_registration_refused(self, registry, pack, code):
        with pytest.raises(PackScopeConflictError) as exc:
            registry.register_tenant(pack, owner=OWNER_A)
        assert exc.value.code == code
        assert registry.list_packs(OWNER_A)[-1]["scope"] == "common"

    def test_any_common_version_counts(self, registry):
        registry.register(_with(TRACKER, version="2", kinds=[{"kind": "epic"}], relations=[]))
        with pytest.raises(PackScopeConflictError, match="issue"):
            registry.register_tenant(_with(FLEET, kinds=[{"kind": "issue"}]), owner=OWNER_A)
        with pytest.raises(PackScopeConflictError, match="epic"):
            registry.register_tenant(_with(FLEET, kinds=[{"kind": "epic"}]), owner=OWNER_A)

    def test_later_common_pack_not_blocked_but_not_enabled_together(self, registry):
        registry.register_tenant(FLEET, owner=OWNER_A)
        # Общий пакет, пришедший позже, арендатор заблокировать не может.
        late = {"name": "transport", "version": "1", "kinds": [{"kind": "vehicle"}]}
        assert registry.register(late)["status"] == "created"
        with pytest.raises(PackScopeConflictError) as exc:
            registry.put_settings(NS_A, strict=False, packages=["transport", "tenant:fleet"])
        assert exc.value.code == "kind_conflict"
        # Порознь оба включаются.
        registry.put_settings(NS_A, strict=False, packages=["tenant:fleet"])
        registry.put_settings(OWNER_B, strict=False, packages=["transport"])


class TestReconcileAndTraversal:
    @pytest.fixture
    def enabled(self, stores, settings, registry):
        registry.register_tenant(FLEET, owner=OWNER_A)
        conf = registry.put_settings(NS_A, strict=True, packages=["tracker", "tenant:fleet@1"])
        assert conf.packages == ["tracker", "tenant:fleet@1"]
        catalog = registry.catalog_for(NS_A)
        assert catalog.to_payload()["packages"] == ["tracker@1", "tenant:fleet@1"]
        assert catalog.canonical("car") == "vehicle"
        return stores[0]

    def _snapshot(self, sid: str, pack: str = "tenant:fleet@1") -> dict:
        return {
            "pack": pack,
            "source": "fleet:registry",
            "scope": "",
            "snapshotId": sid,
            "observedAt": T1,
            "entities": [
                {"kind": "car", "key": "A123BC", "attributes": {"plate": "A123BC"}},
                {"kind": "service", "key": "svc:delivery"},
            ],
            "relations": [
                {
                    "relation": "serves",
                    "from": {"kind": "vehicle", "key": "A123BC"},
                    "to": {"kind": "service", "key": "svc:delivery"},
                }
            ],
        }

    def test_reconcile_and_typed_by_tenant_kind(self, settings, enabled):
        result = reconcile(settings, snapshot=self._snapshot("s1"), namespace=NS_A, conn=enabled)
        assert result["opened"] == 3 and result["duplicate"] is False
        assert {"kind": "vehicle", "key": "A123BC"} in result["changes"]["opened"]

        pack = compile_typed_context(
            settings,
            {
                "namespaces": [NS_A],
                "anchors": [{"kind": "car", "value": "A123BC"}],
                "traverse": [{"relation": "serves", "direction": "out"}],
            },
            conn=enabled,
        )
        reached = [
            (section["kind"], item["natural_key"])
            for section in pack["sections"]
            for item in section["items"]
            if not item["anchor"]
        ]
        assert reached == [("service", "svc:delivery")]

    def test_snapshot_pack_ref_must_match_tenant_pack(self, settings, enabled):
        # Общего пакета «fleet» в namespace нет: ссылка без tenant: — другой пакет.
        with pytest.raises(SnapshotError, match="не включён"):
            reconcile(
                settings, snapshot=self._snapshot("s1", "fleet@1"), namespace=NS_A, conn=enabled
            )

    def test_tenant_kind_unknown_in_other_tenant(self, stores, settings, registry, enabled):
        registry.put_settings(OWNER_B, strict=True, packages=["tracker"])
        graph = stores[1]
        attach_kind_guard(graph, settings)
        with pytest.raises(UnknownKindError):
            graph.kind_guard(OWNER_B, "vehicle", "A123BC")
        assert graph.kind_guard(NS_A, "car", "A123BC") == "vehicle"


class TestHttpTenantPackages:
    """Маршруты /api/memory/packages с грантами двух арендаторов (ADR-017)."""

    KEYS = [
        {"name": "a-key", "key": "a-secret", "grants": [{"prefix": "tenant:a", "write": True}]},
        {"name": "b-key", "key": "b-secret", "grants": [{"prefix": "tenant:b", "write": True}]},
        {
            "name": "admin",
            "key": "admin-secret",
            "service": True,
            "grants": [{"prefix": "tenant:", "read": True, "write": False}],
        },
    ]

    @pytest.fixture
    def client(self, test_settings, clean_db, monkeypatch):
        from fastapi.testclient import TestClient

        import platform_memory.server.app as mod

        settings = test_settings.model_copy_with(
            server_api_key="",
            server_api_keys_pii="",
            api_keys=json.dumps(self.KEYS),
            iam_enabled=False,
            policy_enabled=False,
            pii_protection=False,
        )
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
        return TestClient(mod.app)

    @staticmethod
    def _h(key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    def test_tenant_a_pack_invisible_to_b(self, client):
        assert (
            client.post("/api/memory/packages", json=TRACKER, headers=self._h("admin-secret"))
        ).status_code == 201
        # Общий пакет арендатор не регистрирует, свой — без service scope.
        assert (
            client.post("/api/memory/packages", json=FLEET, headers=self._h("a-secret"))
        ).status_code == 403
        body = {**FLEET, "scope": "tenant", "namespace": OWNER_A}
        resp = client.post("/api/memory/packages", json=body, headers=self._h("a-secret"))
        assert resp.status_code == 201, resp.text
        assert resp.json()["pack"]["ref"] == "tenant:fleet@1"
        assert resp.json()["pack"]["namespace"] == OWNER_A
        assert (
            client.post("/api/memory/packages", json=body, headers=self._h("b-secret"))
        ).status_code == 403

        conflict = {**body, "kinds": [{"kind": "issue"}]}
        resp = client.post("/api/memory/packages", json=conflict, headers=self._h("a-secret"))
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "kind_conflict"

        listed = client.get(
            "/api/memory/packages", params={"namespace": NS_A}, headers=self._h("a-secret")
        ).json()["packages"]
        assert [p["ref"] for p in listed] == ["default", "tracker", "tenant:fleet"]
        got = client.get(
            "/api/memory/packages/tenant:fleet",
            params={"namespace": NS_A},
            headers=self._h("a-secret"),
        )
        assert got.status_code == 200 and got.json()["kinds"][0]["kind"] == "vehicle"
        put = client.put(
            f"/api/memory/namespaces/{NS_A}/kinds",
            json={"strict": True, "packages": ["tenant:fleet"]},
            headers=self._h("a-secret"),
        )
        assert put.status_code == 200 and put.json()["catalog"]["kinds"] == ["vehicle"]

        # Арендатор B: пакета нет — 404, а не 403.
        listed = client.get(
            "/api/memory/packages", params={"namespace": OWNER_B}, headers=self._h("b-secret")
        ).json()["packages"]
        assert [p["ref"] for p in listed] == ["default", "tracker"]
        got = client.get(
            "/api/memory/packages/tenant:fleet",
            params={"namespace": OWNER_B},
            headers=self._h("b-secret"),
        )
        assert got.status_code == 404
        put = client.put(
            f"/api/memory/namespaces/{OWNER_B}/kinds",
            json={"strict": True, "packages": ["tenant:fleet"]},
            headers=self._h("b-secret"),
        )
        assert put.status_code == 404
