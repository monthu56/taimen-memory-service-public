"""Приёмка reconcile на реальных снимках пакета software-delivery (MEM-ADR-020, амендмент).

Фикстуры ``tests/fixtures/software_delivery``: ``pack.json`` — пакет производителя
(``software-delivery.yaml`` суперпроекта, переведённый в JSON как есть, ``version: 1``
числом), ``control-plane.snapshot.json`` и ``platform-web.snapshot.json`` — снимки в
формате SNAPSHOT.md (``pack, source, scope, snapshotId, observedAt, entities[],
relations[]``). Вызовы UI из platform-web ведут на эндпоинты control-plane — связи
через границу снимка.

Сценарии: снимки принимаются как есть; связь на ещё не появившийся конец хранится
отложенной и разрешается, когда цель приходит (в любом порядке); повтор снимка ничего
не меняет; provenance входит в хэш содержимого; сверка закрывает только своё
``(source, scope)``; fact_id привязан к источнику; закрытое ребро не переоткрывается;
строгий режим проверяет связи; пакет действует только там, где включён; scopes
видимости пишутся; снимок ~1000 элементов сверяется за секунды.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from platform_memory.context.typed import compile_typed_context
from platform_memory.core.db import agtype
from platform_memory.core.kinds import UnknownRelationError
from platform_memory.domain.reconcile import (
    SnapshotError,
    SnapshotLedger,
    SnapshotStateMismatch,
    reconcile,
)
from platform_memory.domain.registry import open_registry
from platform_memory.graph import store as store_mod

pytestmark = pytest.mark.integration

NS = "tenant:t1:ws:w1"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "software_delivery"
CHECKPOINTS = "GET /api/v1/runs/{}/checkpoints"
UI_CALL = "platform-web:src/app/runs/[id]/page.tsx:38"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def pack() -> dict:
    return _load("pack.json")


@pytest.fixture
def cp() -> dict:
    return _load("control-plane.snapshot.json")


@pytest.fixture
def web() -> dict:
    return _load("platform-web.snapshot.json")


@pytest.fixture
def registry(stores, settings, pack):
    reg = open_registry(stores[0], settings)
    reg.ensure_schema()
    reg.register(pack)
    reg.put_settings(NS, strict=True, packages=["software-delivery"])
    return reg


def _run(settings, conn, snap: dict, **kw) -> dict:
    return reconcile(settings, snapshot=snap, namespace=NS, conn=conn, **kw)


def _next(snap: dict, sid: str, observed_at: str) -> dict:
    """Следующий снимок того же источника (копия с новым id и временем)."""
    out = copy.deepcopy(snap)
    out["snapshotId"], out["observedAt"] = sid, observed_at
    return out


def _typed(settings, conn, **payload) -> dict:
    payload.setdefault("namespaces", [NS])
    return compile_typed_context(settings, payload, conn=conn)


def _callers(settings, conn, value: str, **extra) -> dict:
    """Typed traversal «кто вызывает endpoint X»: calls, direction=in."""
    return _typed(
        settings,
        conn,
        anchors=[{"kind": "endpoint", "value": value}],
        traverse=[{"relation": "calls", "direction": "in", "limit": 50}],
        **extra,
    )


def _items(pack: dict) -> dict[str, dict]:
    return {i["natural_key"]: i for s in pack["sections"] for i in s["items"]}


def _non_anchor(pack: dict) -> list[str]:
    return sorted(k for k, i in _items(pack).items() if not i["anchor"])


def _graph_size(graph) -> tuple[dict, dict]:
    return graph.count_nodes_by_type([NS]), graph.count_edges_by_type([NS])


class TestRealSnapshots:
    def test_web_before_control_plane_calls_resolve(self, stores, settings, registry, cp, web):
        conn, _, facts, _, _ = stores
        r_web = _run(settings, conn, web)
        assert r_web["observed_at"] == "2026-09-23T08:47:12Z"  # время снимка, а не now
        assert (r_web["source"], r_web["scope"]) == ("git:platform-web", "platform-web")
        assert r_web["entities"]["opened"] == len(web["entities"])
        # Три вызова UI ведут на эндпоинты control-plane, которых ещё нет: связи отложены.
        assert r_web["relations"]["opened"] == len(web["relations"])
        assert r_web["relations"]["pending"] == 3
        assert facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS]) == []

        r_cp = _run(settings, conn, cp)
        assert r_cp["relations"]["pending"] == 0
        assert r_cp["relations"]["resolved"] == 3  # отложенные связи web стали рёбрами
        assert r_cp["relations"]["retried"] == 3  # перепланированы чужие отложенные
        (call,) = facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS])
        assert call["object"] == CHECKPOINTS
        assert call["attributes"] == {"queryParams": ["limit"]}
        assert call["valid_from"] == "2026-09-23T08:47:12Z"
        assert (call["snapshot_source"], call["snapshot_scope"]) == (
            "git:platform-web",
            "platform-web",
        )

        # «Кто вызывает endpoint X»: вызов UI и метод клиента.
        pack = _callers(settings, conn, CHECKPOINTS)
        assert _non_anchor(pack) == [
            "control-plane:client.ControlPlaneClient.list_checkpoints",
            UI_CALL,
        ]
        items = _items(pack)
        assert items[UI_CALL]["kind"] == "ui_call"
        assert items[UI_CALL]["provenance"]["line"] == 38
        endpoint = items[CHECKPOINTS]
        assert endpoint["source_path"] == (
            "control-plane@f6023272c0ffee00000000000000000000000001:"
            "src/control_plane/api/v1/runs.py:199"
        )
        assert endpoint["aliases"] == ["/api/v1/runs/{run_id}/checkpoints"]
        snaps = {s["source"] for s in pack["used"]["snapshots"]}
        assert snaps == {"git:control-plane", "git:platform-web"}

        # Якорь по псевдониму ключа (путь с исходными именами параметров).
        # Путь общий у GET и POST — якорь разрешается в оба эндпоинта.
        by_alias = _callers(settings, conn, "/api/v1/runs/{run_id}/checkpoints")
        resolved = by_alias["anchors"][0]["resolved"]
        assert {(r["natural_key"], r["method"]) for r in resolved} == {
            (CHECKPOINTS, "alias"),
            ("POST /api/v1/runs/{}/checkpoints", "alias"),
        }
        assert _non_anchor(by_alias) == [
            "control-plane:client.ControlPlaneClient.create_checkpoint",
            "control-plane:client.ControlPlaneClient.list_checkpoints",
            UI_CALL,
        ]

    def test_control_plane_first_links_immediately(self, stores, settings, registry, cp, web):
        conn = stores[0]
        _run(settings, conn, cp)
        r_web = _run(settings, conn, web)
        assert (r_web["relations"]["pending"], r_web["relations"]["resolved"]) == (0, 0)
        assert UI_CALL in _non_anchor(_callers(settings, conn, CHECKPOINTS))

    def test_anchor_in_inexact_form_resolves_like_resolve_channel(
        self, stores, settings, registry, cp, web
    ):
        """Якорь без метода / с другим плейсхолдером / без репозитория (амендмент typed)."""
        conn = stores[0]
        _run(settings, conn, cp)
        _run(settings, conn, web)

        exact = _callers(settings, conn, CHECKPOINTS)
        assert exact["anchors"][0]["matchedBy"] == "natural_key"

        # Другое имя параметра — нормализация к ключу, прежние вызывающие.
        renamed = _callers(settings, conn, "GET /api/v1/runs/{id}/checkpoints")
        assert renamed["anchors"][0]["matchedBy"] == "normalized"
        assert _non_anchor(renamed) == _non_anchor(exact)

        # Путь без метода и с другим параметром — все методы пути, без выбора.
        no_method = _callers(settings, conn, "/api/v1/runs/{id}/checkpoints")
        anchor = no_method["anchors"][0]
        assert anchor["matchedBy"] == "suffix" and anchor["ambiguous"] is True
        assert {c["natural_key"] for c in anchor["candidates"]} == {
            CHECKPOINTS,
            "POST /api/v1/runs/{}/checkpoints",
        }
        assert no_method["unresolved"] == []
        assert _non_anchor(no_method) == [
            "control-plane:client.ControlPlaneClient.create_checkpoint",
            "control-plane:client.ControlPlaneClient.list_checkpoints",
            UI_CALL,
        ]

        # Файл без префикса репозитория и таблица по имени.
        typed = _typed(
            settings,
            conn,
            anchors=[
                {"kind": "source_file", "value": "src/control_plane/api/v1/runs.py"},
                {"kind": "table", "value": "runs"},
            ],
        )
        got = [(r["natural_key"], r["method"]) for a in typed["anchors"] for r in a["resolved"]]
        assert got == [
            ("control-plane:src/control_plane/api/v1/runs.py", "suffix"),
            ("control-plane:runs", "suffix"),
        ]
        assert typed["unresolved"] == []

    def test_repeat_changes_nothing(self, stores, settings, registry, cp, web):
        conn, graph, _, _, _ = stores
        _run(settings, conn, web)
        _run(settings, conn, cp)
        before = _graph_size(graph)

        again = _run(settings, conn, cp)
        assert again["duplicate"] is True

        # Тот же контент новым снимком: всё unchanged, граф не меняется.
        same = _run(settings, conn, _next(cp, "control-plane@next", "2026-09-23T09:45:00Z"))
        assert same["duplicate"] is False
        assert (same["opened"], same["closed"], same["superseded"]) == (0, 0, 0)
        assert same["unchanged"] == len(cp["entities"]) + len(cp["relations"])
        assert _graph_size(graph) == before

    def test_provenance_change_supersedes(self, stores, settings, registry, cp):
        conn, graph, _, _, _ = stores
        _run(settings, conn, cp)
        moved = _next(cp, "control-plane@moved", "2026-09-24T08:00:00Z")
        entity = next(e for e in moved["entities"] if e["key"] == CHECKPOINTS)
        entity["provenance"]["line"] = 205  # код сдвинулся: атрибуты те же
        result = _run(settings, conn, moved)
        assert result["entities"] == {"opened": 1, "closed": 1, "unchanged": 15, "superseded": 1}

        ledger = SnapshotLedger(conn, settings.snapshots_table)
        versions = ledger.entity_versions([NS], [CHECKPOINTS])[(NS, "endpoint", CHECKPOINTS)]
        assert [v["payload"]["provenance"]["line"] for v in versions] == [199, 205]
        assert versions[0]["valid_to"] == "2026-09-24T08:00:00Z"
        node = graph.get_node(CHECKPOINTS, namespaces=[NS])
        assert node["props"]["provenance"]["line"] == 205
        assert node["source_path"].endswith("runs.py:205")

    def test_changes_carry_node_keys(self, stores, settings, registry, cp):
        """Амендмент MEM-ADR-020 «изменённые ключи»: новый, изменённый и пропавший узел —
        каждый в своём списке; повтор снимка — пустые списки."""
        conn = stores[0]
        first = _run(settings, conn, cp)
        everything = sorted(
            ({"kind": e["kind"], "key": e["key"]} for e in cp["entities"]),
            key=lambda r: (r["kind"], r["key"]),
        )
        assert first["changes"] == {
            "opened": everything,
            "changed": [],
            "closed": [],
            "limit": settings.reconcile_changes_limit,
            "truncated": False,
        }
        # Прежние поля ответа не меняются.
        assert first["entities"]["opened"] == len(cp["entities"])

        nxt = _next(cp, "control-plane@2", "2026-09-24T08:00:00Z")
        gone = next(e for e in nxt["entities"] if e["kind"] == "adr")
        nxt["entities"].remove(gone)
        nxt["relations"] = [
            r for r in nxt["relations"] if gone["key"] not in (r["from"]["key"], r["to"]["key"])
        ]
        moved = next(e for e in nxt["entities"] if e["key"] == CHECKPOINTS)
        moved["provenance"]["line"] = 205
        added = {**copy.deepcopy(moved), "key": CHECKPOINTS + "/latest", "title": "latest"}
        nxt["entities"].append(added)
        result = _run(settings, conn, nxt)
        assert result["changes"] == {
            "opened": [{"kind": "endpoint", "key": added["key"]}],
            "changed": [{"kind": "endpoint", "key": CHECKPOINTS}],
            "closed": [{"kind": "adr", "key": gone["key"]}],
            "limit": settings.reconcile_changes_limit,
            "truncated": False,
        }
        assert (result["entities"]["opened"], result["entities"]["closed"]) == (2, 2)
        assert result["entities"]["superseded"] == 1

        empty = {"opened": [], "changed": [], "closed": []}
        again = _run(settings, conn, nxt)
        assert again["duplicate"] is True
        assert {k: again["changes"][k] for k in empty} == empty
        assert again["entities"] == result["entities"]  # счётчики — сохранённые
        same = _run(settings, conn, _next(nxt, "control-plane@3", "2026-09-25T08:00:00Z"))
        assert {k: same["changes"][k] for k in empty} == empty
        assert same["changes"]["truncated"] is False

    def test_changes_truncated_by_limit(self, stores, settings, registry, cp):
        limited = settings.model_copy(update={"reconcile_changes_limit": 3})
        result = _run(limited, stores[0], cp)
        assert len(result["changes"]["opened"]) == 3
        assert result["changes"]["truncated"] is True
        assert result["entities"]["opened"] == len(cp["entities"])

    def test_source_scope_isolation(self, stores, settings, registry, cp, web):
        conn, graph, facts, _, _ = stores
        _run(settings, conn, cp)
        _run(settings, conn, web)
        # platform-web больше не вызывает checkpoints: закрывается только его пара.
        web2 = _next(web, "platform-web@2", "2026-09-24T08:00:00Z")
        web2["relations"] = [
            r
            for r in web2["relations"]
            if not (r["relation"] == "calls" and r["from"]["key"] == UI_CALL)
        ]
        result = _run(settings, conn, web2)
        assert (result["relations"]["closed"], result["entities"]["closed"]) == (1, 0)
        assert _non_anchor(_callers(settings, conn, CHECKPOINTS)) == [
            "control-plane:client.ControlPlaneClient.list_checkpoints"
        ]

        # Пустой снимок platform-web закрывает всё своё и не трогает control-plane.
        empty = _next(web, "platform-web@3", "2026-09-25T08:00:00Z")
        empty["entities"], empty["relations"] = [], []
        _run(settings, conn, empty)
        assert graph.get_node(UI_CALL, namespaces=[NS])["valid_to"] == "2026-09-25T08:00:00Z"
        endpoint = graph.get_node(CHECKPOINTS, namespaces=[NS])
        assert endpoint.get("valid_to") is None
        cp_calls = facts.facts(obj=CHECKPOINTS, predicate="calls", namespaces=[NS])
        assert [f["subject"] for f in cp_calls] == [
            "control-plane:client.ControlPlaneClient.list_checkpoints"
        ]

    def test_fact_id_bound_to_source(self, stores, settings, registry, cp, web):
        conn, graph, facts, _, _ = stores
        _run(settings, conn, cp)
        _run(settings, conn, web)
        # Тот же вызов из второго источника (например, зеркала репозитория).
        mirror = _next(web, "mirror@1", "2026-09-23T08:50:00Z")
        mirror["source"], mirror["scope"] = "git:platform-web-mirror", "platform-web"
        _run(settings, conn, mirror)
        calls = facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS])
        assert len(calls) == 2 and len({c["fact_id"] for c in calls}) == 2

        # Зеркало пропало: его факт закрыт, факт platform-web открыт, узел держится.
        gone = _next(mirror, "mirror@2", "2026-09-24T08:00:00Z")
        gone["entities"], gone["relations"] = [], []
        _run(settings, conn, gone)
        (live,) = facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS])
        assert live["snapshot_source"] == "git:platform-web"
        assert graph.get_node(UI_CALL, namespaces=[NS]).get("valid_to") is None

    def test_closed_edge_not_reopened_with_same_valid_from(
        self, stores, settings, registry, cp, web
    ):
        conn, _, facts, _, _ = stores
        _run(settings, conn, cp)
        at = web["observedAt"]
        _run(settings, conn, web)
        without = _next(web, "platform-web@b", at)
        without["relations"] = [r for r in without["relations"] if r["from"]["key"] != UI_CALL]
        _run(settings, conn, without)
        back = _next(web, "platform-web@c", at)  # тот же valid_from, связь вернулась
        result = _run(settings, conn, back)
        assert result["relations"]["opened"] == 2  # calls и defined_in — новыми версиями
        history = facts.facts(
            subject=UI_CALL, predicate="calls", namespaces=[NS], include_closed=True
        )
        assert len(history) == 2
        closed = [f for f in history if f["valid_to"]]
        assert len(closed) == 1 and closed[0]["valid_to"] == "2026-09-23T08:47:12Z"

    def test_pending_resolves_on_own_retry(self, stores, settings, registry, web):
        conn, graph, facts, _, _ = stores
        _run(settings, conn, web)
        # Цель появилась не через reconcile (например, записана агентом).
        graph.write_fact("GET /api/v1/tasks", "endpoint", "GET /api/v1/tasks", namespace=NS)
        result = _run(settings, conn, _next(web, "platform-web@2", "2026-09-24T08:00:00Z"))
        assert (result["relations"]["resolved"], result["relations"]["pending"]) == (1, 2)
        assert result["relations"]["retried"] == 3  # свои отложенные: 1 разрешилась
        assert facts.facts(obj="GET /api/v1/tasks", predicate="calls", namespaces=[NS])

    @pytest.mark.parametrize("how", ["confirmed", "superseded"])
    def test_pending_rechecked_on_entity_confirmed_by_snapshot(
        self, stores, settings, registry, cp, web, how
    ):
        """Конец отложенной связи — сущность, которую снимок уже держит открытой.

        control-plane открыл эндпоинт раньше, потом его узел пропал из графа (удаление
        через API); связь platform-web к нему отложена. Следующий снимок control-plane
        эндпоинт не открывает впервые, а подтверждает (тот же контент; узел вернул
        агент) или заменяет (новая версия пересоздаёт узел) — связь должна разрешиться
        этой сверкой, а не ждать следующего снимка platform-web.
        """
        conn, graph, facts, _, _ = stores
        _run(settings, conn, cp)
        assert graph.delete_node(CHECKPOINTS, namespace=NS) is not None
        r_web = _run(settings, conn, web)
        assert r_web["relations"]["pending"] == 1  # два других вызова нашли свои эндпоинты
        assert facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS]) == []

        again = _next(cp, "control-plane@2", "2026-09-24T08:00:00Z")
        if how == "confirmed":
            graph.write_fact(CHECKPOINTS, "endpoint", CHECKPOINTS, namespace=NS)
        else:
            entity = next(e for e in again["entities"] if e["key"] == CHECKPOINTS)
            entity["provenance"]["line"] += 1
        r_cp = _run(settings, conn, again)
        assert r_cp["entities"]["opened"] == (0 if how == "confirmed" else 1)
        assert r_cp["relations"]["resolved"] == 1
        (call,) = facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS])
        assert call["snapshot_source"] == "git:platform-web"
        assert call["valid_from"] == "2026-09-23T08:47:12Z"  # время снимка связи


class TestPackAndStrictMode:
    def test_pack_acts_only_where_enabled(self, stores, settings, pack, web):
        conn = stores[0]
        reg = open_registry(conn, settings)
        reg.ensure_schema()
        reg.register(pack)
        # Без настройки namespace — только пакет по умолчанию.
        assert "endpoint" not in reg.catalog_for(NS).kinds
        with pytest.raises(SnapshotError, match="не включён"):
            _run(settings, conn, web)

    def test_strict_mode_checks_relations(self, stores, settings, registry, web):
        conn, graph, _, _, _ = stores
        backwards = copy.deepcopy(web)
        call = next(r for r in backwards["relations"] if r["relation"] == "calls")
        call["from"], call["to"] = call["to"], call["from"]
        with pytest.raises(UnknownRelationError, match="fromKinds"):
            _run(settings, conn, backwards)

        unknown = copy.deepcopy(web)
        unknown["relations"][0]["relation"] = "imports"
        with pytest.raises(UnknownRelationError, match="imports"):
            _run(settings, conn, unknown)
        assert graph.get_node(UI_CALL, namespaces=[NS]) is None  # ничего не записано

    def test_visibility_scopes_written(self, stores, settings, registry, cp, web):
        conn, graph, facts, _, _ = stores
        _run(settings, conn, cp, scopes=["workspace:w1"])
        _run(settings, conn, web, scopes=["workspace:w1"])
        assert graph.get_node(UI_CALL, namespaces=[NS])["props"]["scopes"] == ["workspace:w1"]
        (call,) = facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS])
        assert call["scopes"] == ["workspace:w1"]

        hidden = _callers(settings, conn, CHECKPOINTS, allowed_scopes=["workspace:other"])
        assert hidden["unresolved"]
        seen = _callers(settings, conn, CHECKPOINTS, allowed_scopes=["workspace:w1"])
        assert UI_CALL in _non_anchor(seen)


class TestPerformance:
    def test_thousand_items_reconcile_in_seconds(self, stores, settings, registry):
        conn, graph, _, _, _ = stores
        files = [f"perf:src/api/m{i}.py" for i in range(20)]
        entities = [{"kind": "component", "key": "perf", "attributes": {}}]
        entities += [{"kind": "source_file", "key": f, "attributes": {}} for f in files]
        relations = [
            {
                "relation": "part_of",
                "from": {"kind": "source_file", "key": f},
                "to": {"kind": "component", "key": "perf"},
            }
            for f in files
        ]
        for i in range(480):
            key = f"GET /api/perf/{i}/{{}}"
            entities.append(
                {
                    "kind": "endpoint",
                    "key": key,
                    "attributes": {"path": f"/api/perf/{i}/{{item_id}}", "method": "GET"},
                    "provenance": {"repo": "perf", "sha": "abc", "path": files[i % 20], "line": i},
                }
            )
            relations.append(
                {
                    "relation": "defined_in",
                    "from": {"kind": "endpoint", "key": key},
                    "to": {"kind": "source_file", "key": files[i % 20]},
                }
            )
        snap = {
            "pack": "software-delivery@1",
            "source": "git:perf",
            "scope": "perf",
            "snapshotId": "perf@1",
            "observedAt": "2026-09-23T10:00:00Z",
            "entities": entities,
            "relations": relations,
        }
        total = len(entities) + len(relations)
        assert total >= 1000

        t0 = time.monotonic()
        first = _run(settings, conn, snap)
        cold = time.monotonic() - t0
        assert first["opened"] == total

        changed = _next(snap, "perf@2", "2026-09-23T11:00:00Z")
        for e in changed["entities"][21:71]:  # 50 эндпоинтов изменились
            e["provenance"]["line"] += 1000
        changed["entities"] = changed["entities"][:-30]  # 30 пропали (и их связи)
        dropped = {e["key"] for e in snap["entities"][-30:]}
        changed["relations"] = [r for r in changed["relations"] if r["from"]["key"] not in dropped]
        t0 = time.monotonic()
        second = _run(settings, conn, changed)
        warm = time.monotonic() - t0
        assert second["entities"]["superseded"] == 50
        assert second["entities"]["closed"] == 80
        assert second["relations"]["closed"] == 30
        print(f"\nreconcile {total} элементов: первый {cold:.2f}s, дифф {warm:.2f}s")
        assert cold < 15 and warm < 15, (cold, warm)

    def test_key_lookups_indexed_on_large_graph(self, stores, settings, registry):
        """Граф ~30k узлов, снимок ~1000 элементов со связями на существующие узлы.

        Выборки по списку ключей (``existing_keys``/``close_entities``) — ``UNWIND`` +
        равенство, с hash-индексом ключа на метке: секунды на снимок, а не на выборку.
        """
        conn, graph, _, _, _ = stores
        big = 30_000
        with conn.transaction():
            graph.ensure_label_index("endpoint")
        with conn.cursor() as cur:
            for start in range(0, big, 10_000):
                rows = [
                    # Треть узлов — в чужом namespace: фильтр namespace тоже работает.
                    {"k": f"GET /api/big/{i}/{{}}", "ns": NS if i % 3 else "tenant:other"}
                    for i in range(start, start + 10_000)
                ]
                cur.execute(
                    f"SELECT * FROM cypher('{graph.graph}', $$ UNWIND $rows AS r "
                    "CREATE (:endpoint {natural_key: r.k, namespace: r.ns, type: 'endpoint'}) "
                    "$$, %s) AS (v agtype)",
                    (agtype({"rows": rows}),),
                )
            cur.execute(f'ANALYZE "{graph.graph}".endpoint')

        keys = [f"GET /api/big/{i}/{{}}" for i in range(0, 3000, 3)] + ["GET /nope/{}"]
        t0 = time.monotonic()
        found = graph.existing_keys("endpoint", keys, NS)
        lookup = time.monotonic() - t0
        assert found == {k for k in keys[:-1] if int(k.split("/")[3]) % 3}
        assert graph.existing_keys("no_such_kind", keys, NS) == set()  # метку не создаёт

        # Индекс применим к выборке: без seq scan план идёт по hash-индексу ключа.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(
                f"EXPLAIN SELECT * FROM cypher('{graph.graph}', $$ UNWIND $keys AS k "
                "MATCH (n:endpoint) WHERE n.natural_key = k AND n.namespace = $ns "
                "RETURN n.natural_key $$, %s) AS (k agtype)",
                (agtype({"keys": keys, "ns": NS}),),
            )
            plan = "\n".join(r[0] for r in cur.fetchall())
        assert "endpoint_pm_nk_hash" in plan, plan

        calls = [i for i in range(1, 1500, 3)]
        entities = [{"kind": "component", "key": "big", "attributes": {}}]
        entities += [
            {"kind": "ui_call", "key": f"big:src/page.tsx:{i}", "attributes": {}} for i in calls
        ]
        relations = [
            {
                "relation": "calls",
                "from": {"kind": "ui_call", "key": f"big:src/page.tsx:{i}"},
                "to": {"kind": "endpoint", "key": f"GET /api/big/{i}/{{}}"},
            }
            for i in calls
        ]
        snap = {
            "pack": "software-delivery@1",
            "source": "git:big",
            "scope": "big",
            "snapshotId": "big@1",
            "observedAt": "2026-09-23T10:00:00Z",
            "entities": entities,
            "relations": relations,
        }
        t0 = time.monotonic()
        first = _run(settings, conn, snap)
        cold = time.monotonic() - t0
        assert first["opened"] == len(entities) + len(relations) >= 1000
        assert first["relations"]["pending"] == 0  # все концы найдены среди 30k узлов

        gone = _next(snap, "big@2", "2026-09-23T11:00:00Z")
        gone["entities"], gone["relations"] = entities[:1], []
        t0 = time.monotonic()
        second = _run(settings, conn, gone)
        warm = time.monotonic() - t0
        assert second["entities"]["closed"] == len(entities) - 1
        print(f"\nключи на {big} узлах: {lookup:.3f}s; снимок {cold:.2f}s, закрытие {warm:.2f}s")
        # Прежний ``IN $keys`` — ~1.3 с на одну такую выборку; теперь — миллисекунды.
        assert lookup < 1.0, lookup
        assert cold < 15 and warm < 15, (cold, warm)


class TestLabelIndexes:
    """Кэш «индексы метки есть» и имена индексов длинных меток (MEM-ADR-020, амендмент)."""

    @staticmethod
    def _indexes(conn, graph: str, label: str) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
                (graph, label),
            )
            return {r[0] for r in cur.fetchall()}

    @staticmethod
    def _table_locks(conn, graph: str, label: str) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND l.pid = pg_backend_pid()",
                (graph, label),
            )
            return {r[0] for r in cur.fetchall()}

    def test_ready_label_skips_create_index(self, stores):
        conn, graph, _, _, _ = stores
        key = (store_mod._db_key(conn), graph.graph, "endpoint", False)
        with conn.transaction():
            assert graph.ensure_label_index("endpoint")
            # Создан этой транзакцией — ещё может откатиться, в кэш не попадает.
            assert key not in store_mod._READY_LABEL_INDEXES
            assert "ShareLock" in self._table_locks(conn, graph.graph, "endpoint")
        with conn.transaction():
            assert graph.ensure_label_index("endpoint", create=False)
            assert key in store_mod._READY_LABEL_INDEXES
        with conn.transaction():
            assert graph.existing_keys("endpoint", ["x"], NS) == set()
            # Повторный вызов из кэша: ни CREATE INDEX, ни его ShareLock на таблицу метки.
            assert "ShareLock" not in self._table_locks(conn, graph.graph, "endpoint")

    def test_rolled_back_index_is_not_cached(self, stores):
        conn, graph, _, _, _ = stores
        key = (store_mod._db_key(conn), graph.graph, "service", False)

        class Boom(Exception):
            pass

        with pytest.raises(Boom), conn.transaction():
            graph.ensure_label_index("service")
            graph.ensure_label_index("service")  # второй вызов в той же транзакции
            raise Boom
        assert key not in store_mod._READY_LABEL_INDEXES
        assert graph.ensure_label_index("service", create=False) is False  # метки нет

    def test_long_labels_with_common_prefix_get_own_indexes(self, stores):
        conn, graph, _, _, _ = stores
        prefix = "k" * 50
        first, second = f"{prefix}_alpha", f"{prefix}_beta"
        with conn.transaction():
            graph.ensure_label_index(first)
            graph.ensure_label_index(second)
        a, b = self._indexes(conn, graph.graph, first), self._indexes(conn, graph.graph, second)
        assert len(a) == 2 and len(b) == 2 and not a & b
        assert all(len(name) <= 63 for name in a | b)
        # Короткая метка — прежние имена (совпадают с созданными на действующих графах).
        assert store_mod._label_index_names("endpoint", edge=False) == [
            "endpoint_pm_props_gin",
            "endpoint_pm_nk_hash",
        ]


EMPTY = {"opened": [], "changed": [], "closed": []}


def _ledger_rows(conn, settings) -> tuple[int, int, int]:
    """Строки журнала: снимки, версии элементов, состояния пар."""
    t = settings.snapshots_table
    with conn.cursor() as cur:
        counts = []
        for table in (t, f"{t}_items", f"{t}_state"):
            cur.execute(f'SELECT count(*) FROM public."{table}"')
            counts.append(cur.fetchone()[0])
    return tuple(counts)


def _plan(result: dict) -> dict:
    """То, что предпросмотр обещает применению: счётчики, ключи, конфликты."""
    keys = ("opened", "closed", "unchanged", "superseded", "entities", "relations")
    return {**{k: result[k] for k in keys}, "changes": result["changes"]}


class TestReconcileState:
    """Амендмент MEM-ADR-020 2026-09-28 (K004): предпросмотр, stateToken, expectedState,
    конфликты естественного ключа."""

    def test_dry_run_writes_nothing_and_plans_what_apply_does(
        self, stores, settings, registry, cp, web
    ):
        conn, graph, facts, _, _ = stores
        _run(settings, conn, web)  # отложенные связи web ждут эндпоинтов cp
        rows, size = _ledger_rows(conn, settings), _graph_size(graph)

        plan = _run(settings, conn, cp, dry_run=True)
        assert plan["dryRun"] is True and plan["duplicate"] is False
        assert plan["relations"]["resolved"] == 3  # план видит разрешение чужих связей
        assert plan["entities"]["opened"] == len(cp["entities"])
        # Ни граф, ни журнал (снимки, версии, отложенные связи, состояние) не изменились.
        assert _ledger_rows(conn, settings) == rows
        assert _graph_size(graph) == size
        assert facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS]) == []
        ledger = SnapshotLedger(conn, settings.snapshots_table)
        assert (
            ledger.snapshot_result(NS, "git:control-plane", "control-plane", cp["snapshotId"])
            is None
        )
        # Токен пары без снимков — токен пустого состояния, по нему можно применить.
        assert plan["stateToken"] == ledger.compute_state_token(NS, "x", "y")

        applied = _run(settings, conn, cp, expected_state=plan["stateToken"])
        assert applied["dryRun"] is False
        assert _plan(applied) == _plan(plan)
        assert applied["stateToken"] != plan["stateToken"]
        (call,) = facts.facts(subject=UI_CALL, predicate="calls", namespaces=[NS])
        assert call["object"] == CHECKPOINTS

    def test_unchanged_snapshot_gives_empty_plan_and_same_token(
        self, stores, settings, registry, cp
    ):
        conn = stores[0]
        first = _run(settings, conn, cp)
        same = _next(cp, "control-plane@same", "2026-09-24T08:00:00Z")

        plan = _run(settings, conn, same, dry_run=True)
        assert (plan["opened"], plan["closed"], plan["superseded"]) == (0, 0, 0)
        assert {k: plan["changes"][k] for k in EMPTY} == EMPTY
        assert plan["conflicts"]["items"] == []
        assert plan["stateToken"] == first["stateToken"]

        applied = _run(settings, conn, same, expected_state=plan["stateToken"])
        assert applied["stateToken"] == first["stateToken"]  # без изменений токен прежний

        # Повтор принятого снимка в предпросмотре — duplicate и пустые списки.
        again = _run(settings, conn, same, dry_run=True)
        assert again["duplicate"] is True and again["dryRun"] is True
        assert {k: again["changes"][k] for k in EMPTY} == EMPTY

    def test_stale_expected_state_is_rejected(self, stores, settings, registry, cp):
        conn, graph, _, _, _ = stores
        plan = _run(settings, conn, cp, dry_run=True)
        applied = _run(settings, conn, cp)  # чужая загрузка успела раньше
        changed = _next(cp, "control-plane@2", "2026-09-24T08:00:00Z")
        next(e for e in changed["entities"] if e["key"] == CHECKPOINTS)["title"] = "другое"
        rows, size = _ledger_rows(conn, settings), _graph_size(graph)

        for dry_run in (False, True):
            with pytest.raises(SnapshotStateMismatch) as exc:
                _run(settings, conn, changed, expected_state=plan["stateToken"], dry_run=dry_run)
            assert exc.value.expected_state == plan["stateToken"]
            assert exc.value.state_token == applied["stateToken"]
        assert _ledger_rows(conn, settings) == rows and _graph_size(graph) == size

        # Повтор принятого снимка expectedState не проверяет: потерянный ответ не падает.
        again = _run(settings, conn, cp, expected_state=plan["stateToken"])
        assert again["duplicate"] is True and again["stateToken"] == applied["stateToken"]

        # Новый план по текущему токену применяется.
        fresh = _run(settings, conn, changed, dry_run=True)
        done = _run(settings, conn, changed, expected_state=fresh["stateToken"])
        assert done["changes"]["changed"] == [{"kind": "endpoint", "key": CHECKPOINTS}]

    def test_token_ignores_other_pairs_and_pending_resolution(
        self, stores, settings, registry, cp, web
    ):
        conn = stores[0]
        ledger = SnapshotLedger(conn, settings.snapshots_table)
        r_web = _run(settings, conn, web)
        _run(settings, conn, cp)  # разрешила отложенные связи web
        assert ledger.state_token(NS, "git:platform-web", "platform-web") == r_web["stateToken"]
        # Состояние считается из открытых версий: сохранённое совпадает с вычисленным
        # (пары, принятые до миграции K004, получают токен вычислением).
        assert r_web["stateToken"] == ledger.compute_state_token(
            NS, "git:platform-web", "platform-web"
        )

    def test_key_conflicts_with_other_source_are_visible(self, stores, settings, registry, cp):
        conn = stores[0]
        _run(settings, conn, cp)
        mirror = _next(cp, "mirror@1", "2026-09-24T08:00:00Z")
        mirror["source"], mirror["scope"] = "git:mirror", "mirror"
        mirror["entities"] = [e for e in mirror["entities"] if e["kind"] == "endpoint"]
        mirror["relations"] = []
        endpoints = sorted(e["key"] for e in mirror["entities"])

        plan = _run(settings, conn, mirror, dry_run=True)
        assert plan["conflicts"] == {
            "items": [
                {
                    "kind": "endpoint",
                    "key": k,
                    "source": "git:control-plane",
                    "scope": "control-plane",
                }
                for k in endpoints
            ],
            "limit": settings.reconcile_changes_limit,
            "truncated": False,
        }
        # Конфликт — сведения, а не отказ: применение проходит и видит то же.
        applied = _run(settings, conn, mirror, expected_state=plan["stateToken"])
        assert applied["conflicts"] == plan["conflicts"]
        # Свой источник с собой не конфликтует.
        own = _run(settings, conn, _next(cp, "control-plane@2", "2026-09-25T08:00:00Z"))
        assert {c["source"] for c in own["conflicts"]["items"]} == {"git:mirror"}

        limited = settings.model_copy(update={"reconcile_changes_limit": 1})
        cut = _run(limited, conn, _next(mirror, "mirror@2", "2026-09-26T08:00:00Z"), dry_run=True)
        assert len(cut["conflicts"]["items"]) == 1 and cut["conflicts"]["truncated"] is True


class TestHttpEndToEnd:
    """Плоский документ снимка через HTTP (локальный режим без ключей)."""

    @pytest.fixture
    def client(self, test_settings, clean_db, monkeypatch):
        from fastapi.testclient import TestClient

        import platform_memory.server.app as mod

        settings = test_settings.model_copy_with(
            server_api_key="", server_api_keys_pii="", api_keys="", iam_enabled=False
        )
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
        return TestClient(mod.app)

    def test_flat_snapshot_documents(self, client, pack, cp, web):
        assert client.post("/api/memory/packages", json=pack).status_code == 201  # version: 1
        put = client.put(
            f"/api/memory/namespaces/{NS}/kinds",
            json={"strict": True, "packages": ["software-delivery@1"]},
        )
        assert put.status_code == 200

        first = client.post("/api/memory/reconcile", params={"namespace": NS}, json=web)
        assert first.status_code == 200, first.text
        assert first.json()["relations"]["pending"] == 3
        second = client.post("/api/memory/reconcile", json={**cp, "namespace": NS})
        assert second.json()["relations"]["resolved"] == 3
        again = client.post("/api/memory/reconcile", json={**cp, "namespace": NS})
        assert again.json()["duplicate"] is True

        # Предпросмотр и применение по состоянию (K004); устаревший план — 409.
        nxt = {**_next(web, "platform-web@2", "2026-09-24T08:00:00Z"), "namespace": NS}
        plan = client.post("/api/memory/reconcile", json={**nxt, "dryRun": True})
        assert plan.status_code == 200 and plan.json()["dryRun"] is True
        token = plan.json()["stateToken"]
        stale = client.post("/api/memory/reconcile", json={**nxt, "expectedState": "st1-old"})
        assert stale.status_code == 409
        assert stale.json()["detail"] == {
            "code": "snapshot_stale",
            "message": stale.json()["detail"]["message"],
            "expectedState": "st1-old",
            "stateToken": token,
        }
        applied = client.post("/api/memory/reconcile", json={**nxt, "expectedState": token})
        assert applied.status_code == 200 and applied.json()["stateToken"] == token

        clash = client.post(
            "/api/memory/reconcile", params={"namespace": NS}, json={**cp, "namespace": "other"}
        )
        assert clash.status_code == 400
        bad = copy.deepcopy(web)
        bad["relations"][0]["relation"] = "imports"
        bad["snapshotId"] = "platform-web@bad"
        assert client.post(f"/api/memory/reconcile?namespace={NS}", json=bad).status_code == 422

        pack_resp = client.post(
            "/api/memory/context/typed",
            json={
                "anchors": [{"kind": "endpoint", "value": CHECKPOINTS}],
                "traverse": [{"relation": "calls", "direction": "in"}],
                "scope": {"namespaces": [NS]},
            },
        )
        assert pack_resp.status_code == 200
        assert UI_CALL in _non_anchor(pack_resp.json())
