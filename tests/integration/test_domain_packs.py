"""Доменные пакеты, reconcile снимков и типизированный обход на реальной БД (MEM-ADR-020).

Сценарии задачи TAI-ADR-0042 P1a: регистрация пакета и отказ неизвестного вида
в строгом namespace; reconcile закрывает пропавшее, сохраняет историю, повтор
снимка ничего не меняет; as_of видит прошлое состояние; обход не проходит по
закрытым фактам и соблюдает direction/depth/limit; разрешение якоря по
псевдониму и по idPattern. Запуск — см. conftest (CB_TEST_DATABASE_URL).
"""

from __future__ import annotations

import pytest

from platform_memory.context.typed import compile_typed_context
from platform_memory.core.kinds import UnknownKindError
from platform_memory.domain.reconcile import (
    SnapshotError,
    SnapshotLedger,
    StaleSnapshotError,
    reconcile,
)
from platform_memory.domain.registry import (
    PackConflictError,
    attach_kind_guard,
    open_registry,
)

pytestmark = pytest.mark.integration

NS = "tenant:t1:ws:w1"

TRACKER = {
    "name": "tracker",
    "version": "1.0.0",
    "kinds": [
        {
            "kind": "issue",
            "naturalKey": {"type": "string", "pattern": "^[A-Z]+-\\d+$"},
            "kindAliases": ["ticket"],
            "idPatterns": ["\\b(?P<id>[A-Z]{2,10}-\\d+)\\b"],
            "attributes": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
            },
        },
        {"kind": "service"},
        {"kind": "person"},
    ],
    "relations": [
        {"relation": "blocks", "fromKinds": ["issue"], "toKinds": ["issue"], "temporal": False},
        {
            "relation": "assigned_to",
            "fromKinds": ["issue"],
            "toKinds": ["person"],
            "cardinality": "one",
        },
        {"relation": "touches", "fromKinds": ["issue"], "toKinds": ["service"]},
    ],
}

T1 = "2026-09-01T00:00:00Z"
T2 = "2026-09-10T00:00:00Z"
BETWEEN = "2026-09-05T00:00:00Z"


@pytest.fixture
def registry(stores, settings):
    conn = stores[0]
    reg = open_registry(conn, settings)
    reg.ensure_schema()
    reg.register(TRACKER)
    # Пакет действует только в namespace, где явно включён.
    reg.put_settings(NS, strict=False, packages=["tracker"])
    return reg


def _issue(key: str, status: str = "open", **extra) -> dict:
    return {"kind": "issue", "key": key, "attributes": {"status": status}, **extra}


def _snapshot(sid: str, observed_at: str, entities: list[dict], relations: list[dict]) -> dict:
    """Документ снимка в формате SNAPSHOT.md."""
    return {
        "source": "tracker:main",
        "scope": "",
        "snapshotId": sid,
        "observedAt": observed_at,
        "entities": entities,
        "relations": relations,
    }


def _kind(key: str) -> str:
    if key in ("alice", "bob"):
        return "person"
    return "service" if key.startswith("svc:") else "issue"


def _fact(s: str, rel: str, o: str) -> dict:
    return {
        "relation": rel,
        "from": {"kind": _kind(s), "key": s},
        "to": {"kind": _kind(o), "key": o},
    }


def _run(settings, conn, snap: dict, *, source: str = "") -> dict:
    if source:
        snap = {**snap, "source": source}
    return reconcile(settings, snapshot=snap, namespace=NS, conn=conn)


def _typed(settings, conn, **payload) -> dict:
    payload.setdefault("namespaces", [NS])
    return compile_typed_context(settings, payload, conn=conn)


def _keys(pack: dict, kind: str | None = None) -> list[str]:
    return sorted(
        item["natural_key"]
        for section in pack["sections"]
        if kind is None or section["kind"] == kind
        for item in section["items"]
        if not item["anchor"]
    )


class TestPackRegistry:
    def test_register_list_get_and_immutable_version(self, registry):
        again = registry.register(TRACKER)
        assert again["status"] == "unchanged"
        listed = {p["name"]: p for p in registry.list_packs()}
        assert listed["default"]["builtin"] is True
        assert listed["tracker"]["versions"] == ["1.0.0"]
        assert registry.get("tracker").kinds[0].kind == "issue"

        changed = {**TRACKER, "kinds": [{"kind": "issue"}]}
        with pytest.raises(PackConflictError, match="иммутабельна"):
            registry.register(changed)
        registry.register({**changed, "version": "1.1.0"})
        assert registry.get("tracker").version == "1.1.0"
        assert registry.get("tracker", "1.0.0").kinds[0].kind_aliases == ("ticket",)

    def test_strict_namespace_rejects_unknown_kind(self, stores, settings, registry):
        _, graph, facts, _, _ = stores
        registry.put_settings(NS, strict=True, packages=["tracker@1.0.0"])
        attach_kind_guard(graph, settings)

        with pytest.raises(UnknownKindError, match="spaceship"):
            graph.write_fact("x-1", "spaceship", "Корабль", namespace=NS)
        with pytest.raises(UnknownKindError):
            facts.ensure_entity("x-2", entity_type="spaceship", namespace=NS)
        assert graph.get_node("x-1", namespaces=[NS]) is None

        # Известный вид, его псевдоним и базовые виды проходят; псевдоним — канонизируется.
        graph.write_fact("PROJ-1", "ticket", "Тикет", properties={"status": "open"}, namespace=NS)
        assert graph.get_node("PROJ-1", namespaces=[NS])["type"] == "issue"
        facts.ensure_entity("doc-1", entity_type="document", namespace=NS)

        # reconcile в строгом namespace отвергает весь снимок — ничего не пишется.
        with pytest.raises(UnknownKindError):
            _run(
                settings,
                stores[0],
                _snapshot("s1", T1, [_issue("PROJ-2"), {"kind": "spaceship", "key": "x"}], []),
            )
        assert graph.get_node("PROJ-2", namespaces=[NS]) is None

    def test_non_strict_namespace_keeps_old_behaviour(self, stores, settings, registry):
        _, graph, _, _, _ = stores
        attach_kind_guard(graph, settings)
        graph.write_fact("x-1", "spaceship", "Корабль", namespace="other")
        assert graph.get_node("x-1", namespaces=["other"])["type"] == "spaceship"


class TestReconcile:
    def test_closes_vanished_keeps_history_and_is_idempotent(self, stores, settings, registry):
        conn, graph, facts, _, _ = stores
        s1 = _snapshot(
            "s1",
            T1,
            [_issue("PROJ-1"), _issue("PROJ-2"), _issue("PROJ-3")],
            [_fact("PROJ-1", "blocks", "PROJ-2"), _fact("PROJ-2", "blocks", "PROJ-3")],
        )
        r1 = _run(settings, conn, s1)
        assert (r1["opened"], r1["closed"], r1["unchanged"]) == (5, 0, 0)
        assert r1["duplicate"] is False

        # s2: PROJ-3 пропал (и его факт), PROJ-2 сменил статус, PROJ-1 -> PROJ-2 на месте.
        s2 = _snapshot(
            "s2",
            T2,
            [_issue("PROJ-1"), _issue("PROJ-2", "closed")],
            [_fact("PROJ-1", "blocks", "PROJ-2")],
        )
        r2 = _run(settings, conn, s2)
        assert r2["entities"] == {"opened": 1, "closed": 2, "unchanged": 1, "superseded": 1}
        assert r2["relations"] == {
            "opened": 0,
            "closed": 1,
            "unchanged": 1,
            "superseded": 0,
            "pending": 0,
            "resolved": 0,
            "retried": 0,
        }
        assert (r2["opened"], r2["closed"], r2["unchanged"]) == (1, 3, 2)

        # Ничего не удалено: узел PROJ-3 закрыт, факт PROJ-2 -> PROJ-3 закрыт, но есть.
        node3 = graph.get_node("PROJ-3", namespaces=[NS])
        assert node3 is not None and node3["valid_to"] == T2
        assert graph.get_node("PROJ-2", namespaces=[NS])["props"]["status"] == "closed"
        history = facts.facts(subject="PROJ-2", namespaces=[NS], include_closed=True)
        assert len(history) == 1 and history[0]["valid_to"] == T2
        assert facts.facts(subject="PROJ-2", namespaces=[NS]) == []

        # Журнал версий: у PROJ-2 две версии, старая закрыта ссылкой на новую.
        versions = SnapshotLedger(conn, settings.snapshots_table).entity_versions([NS], ["PROJ-2"])[
            (NS, "issue", "PROJ-2")
        ]
        assert [v["payload"]["attributes"]["status"] for v in versions] == ["open", "closed"]
        assert versions[0]["valid_to"] == T2 and versions[1]["valid_to"] is None

        # Повтор снимка — те же счётчики, duplicate, граф не меняется.
        before = graph.count_edges_by_type([NS])
        r2_again = _run(settings, conn, s2)
        assert r2_again["duplicate"] is True
        assert {k: r2_again[k] for k in ("opened", "closed", "unchanged")} == {
            k: r2[k] for k in ("opened", "closed", "unchanged")
        }
        assert graph.count_edges_by_type([NS]) == before
        assert len(facts.facts(subject="PROJ-2", namespaces=[NS], include_closed=True)) == 1

        # Снимок старше последнего принятого — отказ (история не переписывается).
        with pytest.raises(StaleSnapshotError):
            _run(settings, conn, _snapshot("s0", "2026-08-01T00:00:00Z", [], []))

    def test_temporal_relation_change_supersedes(self, stores, settings, registry):
        conn, _, facts, _, _ = stores
        people = [{"kind": "person", "key": "alice"}, {"kind": "person", "key": "bob"}]
        _run(
            settings,
            conn,
            _snapshot(
                "s1", T1, [_issue("PROJ-1"), *people], [_fact("PROJ-1", "assigned_to", "alice")]
            ),
        )
        r2 = _run(
            settings,
            conn,
            _snapshot(
                "s2", T2, [_issue("PROJ-1"), *people], [_fact("PROJ-1", "assigned_to", "bob")]
            ),
        )
        assert r2["relations"]["superseded"] == 1
        history = {
            f["object"]: f
            for f in facts.facts(subject="PROJ-1", namespaces=[NS], include_closed=True)
        }
        assert history["alice"]["valid_to"] == T2
        assert history["alice"]["superseded_by"] == history["bob"]["fact_id"]
        assert history["bob"]["supersedes"] == history["alice"]["fact_id"]
        assert history["bob"]["snapshot_id"] == "s2"

    def test_many_valued_relation_closes_only_vanished_pairs(self, stores, settings, registry):
        conn, _, facts, _, _ = stores
        services = [{"kind": "service", "key": "svc:a"}, {"kind": "service", "key": "svc:b"}]
        _run(
            settings,
            conn,
            _snapshot(
                "s1",
                T1,
                [_issue("PROJ-1"), *services],
                [_fact("PROJ-1", "touches", "svc:a"), _fact("PROJ-1", "touches", "svc:b")],
            ),
        )
        # touches — набор (cardinality many): пропала пара с svc:a, пара с svc:b на месте.
        r2 = _run(
            settings,
            conn,
            _snapshot(
                "s2", T2, [_issue("PROJ-1"), *services], [_fact("PROJ-1", "touches", "svc:b")]
            ),
        )
        assert (r2["relations"]["closed"], r2["relations"]["superseded"]) == (1, 0)
        assert r2["relations"]["unchanged"] == 1
        live = facts.facts(subject="PROJ-1", namespaces=[NS])
        assert [f["object"] for f in live] == ["svc:b"]
        closed = facts.facts(subject="PROJ-1", namespaces=[NS], include_closed=True)
        gone = next(f for f in closed if f["object"] == "svc:a")
        assert gone["valid_to"] == T2 and gone["superseded_by"] is None

    def test_other_source_keeps_entity_open(self, stores, settings, registry):
        conn, graph, _, _, _ = stores
        _run(settings, conn, _snapshot("a1", T1, [_issue("PROJ-1")], []), source="a")
        _run(settings, conn, _snapshot("b1", T1, [_issue("PROJ-1")], []), source="b")
        _run(settings, conn, _snapshot("a2", T2, [], []), source="a")
        assert graph.get_node("PROJ-1", namespaces=[NS]).get("valid_to") is None

    def test_invalid_snapshot(self, stores, settings, registry):
        with pytest.raises(SnapshotError, match="дважды"):
            _run(settings, stores[0], _snapshot("s1", T1, [_issue("A-1"), _issue("A-1")], []))


class TestTypedTraversal:
    @pytest.fixture
    def chain(self, stores, settings, registry):
        """PROJ-1 -> PROJ-2 -> PROJ-3 -> PROJ-4 (blocks); PROJ-1 touches pay-api."""
        conn = stores[0]
        entities = [
            _issue("PROJ-1"),
            _issue("PROJ-2"),
            _issue("PROJ-3"),
            _issue("PROJ-4"),
            {"kind": "service", "key": "svc:payments", "aliases": ["payments-api"]},
        ]
        facts = [
            _fact("PROJ-1", "blocks", "PROJ-2"),
            _fact("PROJ-2", "blocks", "PROJ-3"),
            _fact("PROJ-3", "blocks", "PROJ-4"),
            _fact("PROJ-1", "touches", "svc:payments"),
        ]
        _run(settings, conn, _snapshot("s1", T1, entities, facts))
        return conn

    def test_direction_depth_limit(self, settings, chain):
        anchor = [{"kind": "issue", "value": "PROJ-2"}]

        def reach(**step) -> list[str]:
            pack = _typed(
                settings, chain, anchors=anchor, traverse=[{"relation": "blocks", **step}]
            )
            return _keys(pack)

        assert reach(direction="out") == ["PROJ-3"]
        assert reach(direction="out", depth=2) == ["PROJ-3", "PROJ-4"]
        assert reach(direction="in", depth=3) == ["PROJ-1"]
        assert reach(direction="both") == ["PROJ-1", "PROJ-3"]
        assert len(reach(direction="both", limit=1)) == 1
        # Другая связь не смешивается с blocks.
        assert reach(direction="out", depth=5, limit=10) == ["PROJ-3", "PROJ-4"]

    def test_chained_steps_and_pack_shape(self, settings, chain):
        pack = _typed(
            settings,
            chain,
            anchors=[{"value": "PROJ-2"}],
            traverse=[
                {"relation": "blocks", "direction": "in"},
                {"relation": "touches", "direction": "out", "from": "previous"},
            ],
        )
        assert _keys(pack, "service") == ["svc:payments"]
        assert {s["kind"] for s in pack["sections"]} == {"issue", "service"}
        assert pack["used"]["snapshots"] == [
            {"source": "tracker:main", "scope": "", "snapshot_id": "s1"}
        ]
        assert len(pack["used"]["facts"]) == 2
        assert {e["natural_key"] for e in pack["used"]["entities"]} == {
            "PROJ-1",
            "PROJ-2",
            "svc:payments",
        }
        assert all(f["snapshot"]["snapshot_id"] == "s1" for f in pack["facts"])
        assert pack["trace_id"].startswith("ctx-")

    def test_does_not_traverse_closed_facts_and_as_of_sees_past(self, settings, chain):
        # s2: PROJ-3 пропал вместе с фактами; PROJ-2 закрыт статусом.
        _run(
            settings,
            chain,
            _snapshot(
                "s2",
                T2,
                [_issue("PROJ-1"), _issue("PROJ-2", "closed"), _issue("PROJ-4")],
                [_fact("PROJ-1", "blocks", "PROJ-2")],
            ),
        )
        step = [{"relation": "blocks", "direction": "out", "depth": 3}]
        now = _typed(settings, chain, anchors=[{"value": "PROJ-1"}], traverse=step)
        assert _keys(now) == ["PROJ-2"]  # PROJ-2 -> PROJ-3 закрыт: дальше не идём

        past = _typed(settings, chain, anchors=[{"value": "PROJ-1"}], traverse=step, as_of=BETWEEN)
        assert _keys(past) == ["PROJ-2", "PROJ-3", "PROJ-4"]
        proj2 = next(
            i for s in past["sections"] for i in s["items"] if i["natural_key"] == "PROJ-2"
        )
        assert proj2["attributes"] == {"status": "open"}  # атрибуты версии на as_of

        current = next(
            i for s in now["sections"] for i in s["items"] if i["natural_key"] == "PROJ-2"
        )
        assert current["attributes"] == {"status": "closed"}
        assert current["snapshot"] == {"source": "tracker:main", "scope": "", "snapshot_id": "s2"}

        # До первого снимка сущностей не было — якорь не разрешается.
        before = _typed(settings, chain, anchors=[{"value": "PROJ-1"}], as_of="2026-01-01")
        assert before["unresolved"] == [{"kind": "", "value": "PROJ-1"}]

    def test_anchor_by_alias_and_id_pattern(self, settings, chain):
        by_alias = _typed(settings, chain, anchors=[{"kind": "service", "value": "payments-api"}])
        (resolved,) = by_alias["anchors"][0]["resolved"]
        assert (resolved["natural_key"], resolved["method"]) == ("svc:payments", "alias")
        assert resolved["evidence"] == "asserted"

        by_id = _typed(
            settings, chain, anchors=[{"kind": "ticket", "value": "см. задачу PROJ-3, срочно"}]
        )
        (resolved,) = by_id["anchors"][0]["resolved"]
        assert (resolved["natural_key"], resolved["method"]) == ("PROJ-3", "id_pattern")
        assert resolved["evidence"] == "extracted"

        # Вид якоря фильтрует: сервиса с таким ключом нет.
        wrong_kind = _typed(settings, chain, anchors=[{"kind": "service", "value": "PROJ-3"}])
        assert wrong_kind["unresolved"]

    def test_semantic_only_when_allowed_and_marked_inferred(self, stores, settings, chain):
        from platform_memory.retrieval import write_and_index_fact

        write_and_index_fact(
            settings,
            "note-payments",
            "document",
            "Платёжный шлюз",
            text="Платёжный шлюз обрабатывает оплату картой",
            namespace=NS,
        )
        anchor = [{"value": "Платёжный шлюз обрабатывает оплату картой"}]
        closed = _typed(settings, chain, anchors=anchor)
        assert closed["unresolved"] and not closed["anchors"][0]["resolved"]

        opened = _typed(settings, chain, anchors=anchor, allow_semantic=True, semantic_k=1)
        (resolved,) = opened["anchors"][0]["resolved"]
        assert resolved["method"] == "semantic" and resolved["evidence"] == "inferred"
        item = opened["sections"][0]["items"][0]
        assert item["evidence"] == "inferred"

    def test_visibility_scopes_hide_private_entities(self, stores, settings, chain):
        _, graph, facts, _, _ = stores
        facts.ensure_entity(
            "PROJ-9", entity_type="issue", namespace=NS, scopes=["principal:someone"]
        )
        facts.assert_fact("PROJ-2", "blocks", "PROJ-9", namespace=NS)
        pack = _typed(
            settings,
            chain,
            anchors=[{"value": "PROJ-2"}],
            traverse=[{"relation": "blocks"}],
            allowed_scopes=["principal:me"],
        )
        assert _keys(pack) == ["PROJ-3"]


class TestHttpEndToEnd:
    """Маршруты /api/memory/* поверх настоящего движка (локальный режим без ключей)."""

    @pytest.fixture
    def client(self, test_settings, clean_db, monkeypatch):
        from fastapi.testclient import TestClient

        import platform_memory.server.app as mod

        settings = test_settings.model_copy_with(
            server_api_key="", server_api_keys_pii="", api_keys="", iam_enabled=False
        )
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
        return TestClient(mod.app)

    def test_pack_strict_reconcile_typed(self, client):
        assert client.post("/api/memory/packages", json=TRACKER).status_code == 201
        assert client.post("/api/memory/packages", json=TRACKER).status_code == 200
        put = client.put(
            f"/api/memory/namespaces/{NS}/kinds",
            json={"strict": True, "packages": ["tracker"]},
        )
        assert put.status_code == 200 and put.json()["catalog"]["kinds"] == [
            "issue",
            "person",
            "service",
        ]

        bad = client.post(
            "/api/brain/facts",
            json={
                "natural_key": "x-1",
                "type": "spaceship",
                "title": "Корабль",
                "scope": {"namespace": NS},
            },
        )
        assert bad.status_code == 422

        snap = _snapshot(
            "s1", T1, [_issue("PROJ-1"), _issue("PROJ-2")], [_fact("PROJ-1", "blocks", "PROJ-2")]
        )
        body = {**snap, "namespace": NS}
        first = client.post("/api/memory/reconcile", json=body).json()
        assert (first["opened"], first["duplicate"]) == (3, False)
        again = client.post("/api/memory/reconcile", json=body).json()
        assert (again["opened"], again["duplicate"]) == (3, True)

        pack = client.post(
            "/api/memory/context/typed",
            json={
                "anchors": [{"kind": "ticket", "value": "PROJ-1"}],
                "traverse": [{"relation": "blocks"}],
                "scope": {"namespaces": [NS]},
            },
        )
        assert pack.status_code == 200
        assert _keys(pack.json()) == ["PROJ-2"]
        trace = client.get(
            f"/api/memory/context/trace/{pack.json()['trace_id']}", params={"namespace": NS}
        )
        assert trace.status_code == 200
