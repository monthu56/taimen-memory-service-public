"""Сведение атрибутов сущности по источникам снимков на реальной БД (MEM-ADR-022).

Одна сущность (``legal_entity`` по ИНН) приходит из двух источников: учётная система
(``erp:main``) пишет полный набор реквизитов, CRM (``crm:main``) — только
``{name, inn}``, у ИП — с маской ПДн в имени. Проверяется: чтение (перечень
сущностей, typed-контекст, узел графа, индекс поиска) сводит атрибуты без затирания,
конфликт имени решает ``sourcePriority`` вида независимо от порядка записи, без
приоритета — более поздний ``observedAt``; сверка снимка CRM без компании не трогает
сущность, атрибуты и связи учётной системы; миграция пересводит узлы, записанные до
сведения. Запуск — см. conftest.
"""

from __future__ import annotations

import pytest

from platform_memory.context.entities import query_entities
from platform_memory.context.typed import compile_typed_context
from platform_memory.core.kinds import AttributesError
from platform_memory.domain.reconcile import merge_namespace_nodes, reconcile
from platform_memory.domain.registry import open_registry
from platform_memory.index.entities import EntityIndex

pytestmark = pytest.mark.integration

NS = "tenant:t1:ws:crm"
INN = "7700000001"
T1 = "2026-09-01T00:00:00Z"
T2 = "2026-09-02T00:00:00Z"
T3 = "2026-09-03T00:00:00Z"

ERP_ATTRS = {
    "name": "ООО «Ромашка»",
    "inn": INN,
    "kpp": "770001001",
    "ogrn": "1027700000001",
    "shortName": "Ромашка",
    "own": False,
}
CRM_ATTRS = {"name": "Ромашка (CRM)", "inn": INN}


def _pack(priority: list[str] | None) -> dict:
    kind: dict = {
        "kind": "legal_entity",
        "naturalKey": "{inn}",
        "searchable": {"fields": ["name", "shortName", "kpp"]},
    }
    if priority is not None:
        kind["sourcePriority"] = priority
    return {
        "name": "counterparty" if priority is not None else "counterparty-plain",
        "version": 1,
        "kinds": [kind, {"kind": "deal", "naturalKey": "{number}"}],
        "relations": [
            {"relation": "counterparty_of", "fromKinds": ["legal_entity"], "toKinds": ["deal"]}
        ],
    }


def _enable(stores, settings, priority: list[str] | None = ("erp:*",)) -> None:
    reg = open_registry(stores[0], settings)
    reg.ensure_schema()
    pack = _pack(list(priority) if priority is not None else None)
    reg.register(pack)
    reg.put_settings(NS, strict=False, packages=[pack["name"]])


def _snap(source: str, sid: str, observed_at: str, entities: list, relations=()) -> dict:
    return {
        "source": source,
        "scope": "",
        "snapshotId": sid,
        "observedAt": observed_at,
        "entities": entities,
        "relations": list(relations),
    }


def _company(attrs: dict, title: str = "") -> dict:
    out = {"kind": "legal_entity", "key": INN, "attributes": dict(attrs)}
    if title:
        out["title"] = title
    return out


def _erp(sid="erp-1", observed_at=T1, *, with_company=True, with_deal=True) -> dict:
    entities = [_company(ERP_ATTRS, title=ERP_ATTRS["name"])] if with_company else []
    relations = []
    if with_deal:
        entities.append({"kind": "deal", "key": "D-1", "attributes": {"number": "D-1"}})
        if with_company:
            relations.append(
                {
                    "relation": "counterparty_of",
                    "from": {"kind": "legal_entity", "key": INN},
                    "to": {"kind": "deal", "key": "D-1"},
                }
            )
    return _snap("erp:main", sid, observed_at, entities, relations)


def _crm(sid="crm-1", observed_at=T2, *, with_company=True, attrs=None) -> dict:
    extra = {"kind": "deal", "key": "D-9", "attributes": {"number": "D-9"}}
    attrs = attrs or CRM_ATTRS
    entities = [_company(attrs, title=attrs.get("name", ""))]
    return _snap("crm:main", sid, observed_at, [*(entities if with_company else []), extra])


def _run(settings, conn, snap: dict) -> dict:
    return reconcile(settings, snapshot=snap, namespace=NS, conn=conn)


def _entity(settings, conn) -> dict:
    page = query_entities(settings, {"kinds": ["legal_entity"], "namespaces": [NS]}, conn=conn)
    (item,) = page["items"]
    return item


def _node(graph) -> dict:
    node = graph.get_node(INN, [NS])
    assert node is not None
    return node


def _node_attrs(node: dict) -> dict:
    props = dict(node.get("props") or {})
    return {k: v for k, v in props.items() if k not in {"aliases", "snapshot", "scopes"}}


class TestMergeOnRead:
    @pytest.mark.parametrize("erp_first", [True, False])
    def test_erp_requisites_survive_crm_snapshot(self, stores, settings, erp_first):
        """Приёмка: реквизиты учётной системы и имя по приоритету — при любом порядке."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        # observedAt у CRM позже: без приоритета победила бы она.
        for snap in [_erp(), _crm()] if erp_first else [_crm(), _erp()]:
            _run(settings, conn, snap)

        item = _entity(settings, conn)
        assert item["attributes"] == ERP_ATTRS
        assert item["title"] == ERP_ATTRS["name"]
        assert item["source"] == "erp:main"  # старшая версия — по приоритету
        assert [s["source"] for s in item["sources"]] == ["erp:main", "crm:main"]
        assert all(s["source_path"] for s in item["sources"])

        # Узел графа (recall, nodes, обход) — то же сведение.
        node = _node(graph)
        assert _node_attrs(node) == ERP_ATTRS
        assert node["title"] == ERP_ATTRS["name"]
        assert node["props"]["snapshot"]["source"] == "erp:main"
        assert node.get("valid_to") is None

        # typed-контекст: сведённые атрибуты и снимки обоих источников в used.
        pack = compile_typed_context(
            settings,
            {"namespaces": [NS], "anchors": [{"kind": "legal_entity", "value": INN}]},
            conn=conn,
        )
        (section,) = pack["sections"]
        (ent,) = section["items"]
        assert ent["attributes"] == ERP_ATTRS
        assert ent["title"] == ERP_ATTRS["name"]
        assert {s["source"] for s in ent["sources"]} == {"erp:main", "crm:main"}
        assert {s["source"] for s in pack["used"]["snapshots"]} == {"erp:main", "crm:main"}

        # Индекс поиска по сущностям — текст сведения.
        index = EntityIndex(
            conn,
            settings.entity_embeddings_table,
            settings.embedding_dim,
            settings.default_namespace,
        )
        (row,) = index.rows(NS, ["legal_entity"]).values()
        assert "kpp: 770001001" in row.text
        assert row.title == ERP_ATTRS["name"]

    def test_pii_mask_does_not_override_priority_source(self, stores, settings):
        """Маска ПДн у ИП в CRM не затирает имя из учётной системы."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm(attrs={"name": "ИП И*** И.И.", "inn": INN}))
        assert _entity(settings, conn)["attributes"]["name"] == ERP_ATTRS["name"]
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]

    def test_without_priority_latest_observed_wins_per_attribute(self, stores, settings):
        """Без sourcePriority конфликт атрибута — более поздний observedAt, а не порядок
        записи; атрибуты, которых у позднего источника нет, остаются."""
        conn, graph, *_ = stores
        _enable(stores, settings, priority=None)
        _run(settings, conn, _crm(observed_at=T2))
        _run(settings, conn, _erp(observed_at=T1))  # записан позже, но наблюдён раньше
        item = _entity(settings, conn)
        assert item["attributes"] == {**ERP_ATTRS, "name": CRM_ATTRS["name"]}
        assert item["title"] == CRM_ATTRS["name"]
        assert _node(graph)["props"]["name"] == CRM_ATTRS["name"]

        # Снимок учётной системы позже CRM с другим КПП: имя учётная система не меняла —
        # «последний писавший» имени по-прежнему CRM (по атрибуту, а не по узлу).
        erp2 = _erp(sid="erp-2", observed_at=T3)
        erp2["entities"][0]["attributes"]["kpp"] = "770002002"
        _run(settings, conn, erp2)
        item = _entity(settings, conn)
        assert item["attributes"]["name"] == CRM_ATTRS["name"]
        assert item["attributes"]["kpp"] == "770002002"
        # Учётная система сменила имя позже CRM — имя переходит к ней.
        erp3 = _erp(sid="erp-3", observed_at="2026-09-04T00:00:00Z")
        erp3["entities"][0]["attributes"].update(kpp="770002002", name="ООО «Ромашка-2»")
        _run(settings, conn, erp3)
        assert _entity(settings, conn)["attributes"]["name"] == "ООО «Ромашка-2»"
        assert _node(graph)["props"]["name"] == "ООО «Ромашка-2»"


class TestReconcileAcrossSources:
    def test_crm_snapshot_without_company_keeps_erp_entity(self, stores, settings):
        """Приёмка: сверка снимка CRM без компании не удаляет сущность учётной системы,
        её атрибуты и связи."""
        conn, graph, facts, *_ = stores
        _enable(stores, settings)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm(attrs={**CRM_ATTRS, "manager": "Иванов"}))
        assert _entity(settings, conn)["attributes"]["manager"] == "Иванов"

        result = _run(settings, conn, _crm(sid="crm-2", observed_at=T3, with_company=False))
        assert {"kind": "legal_entity", "key": INN} in result["changes"]["closed"]

        item = _entity(settings, conn)
        assert item["attributes"] == ERP_ATTRS  # атрибут только CRM ушёл вместе с ней
        assert [s["source"] for s in item["sources"]] == ["erp:main"]
        node = _node(graph)
        assert node.get("valid_to") is None
        assert _node_attrs(node) == ERP_ATTRS
        (edge,) = facts.facts(subject=INN, predicate="counterparty_of", namespaces=[NS])
        assert edge["object"] == "D-1" and edge["valid_to"] is None

    def test_erp_leaves_crm_attributes_remain(self, stores, settings):
        """Обратное: учётная система закрыла сущность — узел сведён из версии CRM."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        _run(settings, conn, _erp(sid="erp-2", observed_at=T3, with_company=False))
        assert _entity(settings, conn)["attributes"] == CRM_ATTRS
        node = _node(graph)
        assert _node_attrs(node) == CRM_ATTRS
        assert node["props"]["snapshot"]["source"] == "crm:main"

    def test_closed_by_all_sources_closes_node(self, stores, settings):
        conn, graph, *_ = stores
        _enable(stores, settings)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        _run(settings, conn, _crm(sid="crm-2", observed_at=T3, with_company=False))
        _run(settings, conn, _erp(sid="erp-2", observed_at=T3, with_company=False))
        page = query_entities(settings, {"kinds": ["legal_entity"], "namespaces": [NS]}, conn=conn)
        assert page["items"] == []
        assert _node(graph)["valid_to"] == T3

    def test_as_of_merges_versions_valid_then(self, stores, settings):
        conn, *_ = stores
        _enable(stores, settings)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm(attrs={**CRM_ATTRS, "manager": "Иванов"}))
        _run(settings, conn, _crm(sid="crm-2", observed_at=T3, with_company=False))
        page = query_entities(
            settings,
            {"kinds": ["legal_entity"], "namespaces": [NS], "as_of": "2026-09-02T12:00:00Z"},
            conn=conn,
        )
        (item,) = page["items"]
        assert item["attributes"] == {**ERP_ATTRS, "manager": "Иванов"}
        assert item["valid_from"] == T2 and item["valid_to"] == T3


class TestMigration:
    def test_merge_namespace_nodes_restores_overwritten_node(self, stores, settings):
        """Узел, записанный до сведения (свойства — снимок CRM), пересводится из журнала."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        # Состояние до MEM-ADR-022: последний писавший переписал свойства узла целиком.
        graph.upsert_entities(
            "legal_entity",
            [{"k": INN, "t": CRM_ATTRS["name"], "sp": "", "props": dict(CRM_ATTRS)}],
            NS,
            valid_from=T2,
            last_seen=T2,
        )
        assert _node_attrs(_node(graph)) == CRM_ATTRS

        catalog = open_registry(conn, settings).catalog_for(NS)
        assert merge_namespace_nodes(settings, conn, NS, catalog) == {"nodes": 3}
        node = _node(graph)
        assert _node_attrs(node) == ERP_ATTRS
        assert node["title"] == ERP_ATTRS["name"]
        # Повтор ничего не меняет по существу (идемпотентно).
        merge_namespace_nodes(settings, conn, NS, catalog)
        assert _node_attrs(_node(graph)) == ERP_ATTRS

    def test_migration_does_not_resurrect_deleted_node(self, stores, settings):
        conn, graph, *_ = stores
        _enable(stores, settings)
        _run(settings, conn, _erp())
        graph.delete_node(INN, NS)
        catalog = open_registry(conn, settings).catalog_for(NS)
        assert merge_namespace_nodes(settings, conn, NS, catalog) == {"nodes": 1}
        assert graph.get_node(INN, [NS]) is None


def _run_scoped(settings, conn, snap: dict, scopes: list[str]) -> dict:
    return reconcile(settings, snapshot=snap, namespace=NS, scopes=scopes, conn=conn)


def _index_row(settings, conn):
    index = EntityIndex(
        conn, settings.entity_embeddings_table, settings.embedding_dim, settings.default_namespace
    )
    (row,) = index.rows(NS, ["legal_entity"]).values()
    return row


class TestVisibilityAcrossSources:
    """Узел графа и строка индекса видимость версий не расширяют (MEM-ADR-022, п.2а)."""

    W1, W2 = "workspace:w1", "workspace:w2"

    def _entity_for(self, settings, conn, allowed: list[str]) -> dict:
        page = query_entities(
            settings,
            {"kinds": ["legal_entity"], "namespaces": [NS], "allowed_scopes": allowed},
            conn=conn,
        )
        (item,) = page["items"]
        return item

    def test_restricted_scopes_do_not_leak_into_node_and_index(self, stores, settings):
        """ERP в W1, CRM в W2: реквизиты W1 не видны через узел и индекс вызывающему с W2."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        _run_scoped(settings, conn, _erp(), [self.W1])
        _run_scoped(settings, conn, _crm(), [self.W2])

        node = _node(graph)
        assert node["props"]["scopes"] == [self.W1]  # класс старшей версии (erp:*)
        assert _node_attrs(node) == ERP_ATTRS
        row = _index_row(settings, conn)
        assert row.scopes == [self.W1]
        assert row.source == "erp:main"

        # Читатели журнала сводят только видимые вызывающему версии.
        assert self._entity_for(settings, conn, [self.W2])["attributes"] == CRM_ATTRS
        assert self._entity_for(settings, conn, [self.W1])["attributes"] == ERP_ATTRS
        both = self._entity_for(settings, conn, [self.W1, self.W2])
        assert both["attributes"] == ERP_ATTRS
        assert [s["source"] for s in both["sources"]] == ["erp:main", "crm:main"]

    def test_public_version_stays_visible_to_everyone(self, stores, settings):
        """ERP в W1, CRM без scopes: узел и индекс — публичная версия, без реквизитов W1."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        _run_scoped(settings, conn, _erp(), [self.W1])
        _run_scoped(settings, conn, _crm(), [])

        node = _node(graph)
        assert not node["props"].get("scopes")
        assert _node_attrs(node) == CRM_ATTRS
        row = _index_row(settings, conn)
        assert row.scopes == []
        assert ERP_ATTRS["kpp"] not in row.text
        assert self._entity_for(settings, conn, [self.W2])["attributes"] == CRM_ATTRS
        assert self._entity_for(settings, conn, [self.W1])["attributes"] == ERP_ATTRS

        # Публичная версия ушла — узел сводится из оставшегося класса W1.
        _run_scoped(settings, conn, _crm(sid="crm-2", observed_at=T3, with_company=False), [])
        node = _node(graph)
        assert node["props"]["scopes"] == [self.W1]
        assert _node_attrs(node) == ERP_ATTRS


def _enable_strict(stores, settings) -> None:
    reg = open_registry(stores[0], settings)
    reg.ensure_schema()
    pack = _pack(["erp:*"])
    pack["name"] = "counterparty-strict"
    pack["kinds"][0]["attributes"] = {
        "type": "object",
        "required": ["name", "inn"],
        "properties": {
            **{name: {"type": "string"} for name in ("name", "inn", "kpp", "ogrn", "shortName")},
            "own": {"type": "boolean"},
        },
    }
    reg.register(pack)
    reg.put_settings(NS, strict=True, packages=[pack["name"]])


class TestRequiredOnMergedEntity:
    """``required`` вида — у сведения источников, а не у вклада снимка (MEM-ADR-022, п.6)."""

    def test_partial_contribution_accepted_when_other_source_gives_required(self, stores, settings):
        conn, graph, *_ = stores
        _enable_strict(stores, settings)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm(attrs={"inn": INN}))
        item = _entity(settings, conn)
        assert item["attributes"] == ERP_ATTRS
        assert {s["source"] for s in item["sources"]} == {"erp:main", "crm:main"}

    def test_missing_in_every_source_rejects_snapshot(self, stores, settings):
        conn, graph, *_ = stores
        _enable_strict(stores, settings)
        with pytest.raises(AttributesError, match="name"):
            _run(settings, conn, _crm(attrs={"inn": INN}))
        # Отказ откатывает сверку целиком: ни журнала, ни узла.
        page = query_entities(settings, {"kinds": ["legal_entity"], "namespaces": [NS]}, conn=conn)
        assert page["items"] == []
        assert graph.get_node(INN, [NS]) is None
        assert graph.get_node("D-9", [NS]) is None

    def test_types_still_checked_on_contribution(self, stores, settings):
        conn, *_ = stores
        _enable_strict(stores, settings)
        _run(settings, conn, _erp())
        with pytest.raises(AttributesError, match="own"):
            _run(settings, conn, _crm(attrs={"inn": INN, "own": "да"}))


class TestPutKindsRemerge:
    """``PUT …/kinds`` пересводит узлы по отпечатку последнего сведения, а не по
    настройке до запроса (MEM-ADR-022, п. 5)."""

    @pytest.fixture
    def client(self, stores, settings, monkeypatch):
        from fastapi.testclient import TestClient

        import platform_memory.server.app as mod

        local = settings.model_copy_with(
            server_api_key="", server_api_keys_pii="", api_keys="", iam_enabled=False
        )
        monkeypatch.setattr(mod, "get_settings", lambda: local)
        return TestClient(mod.app)

    @staticmethod
    def _put(client) -> dict:
        resp = client.put(
            f"/api/memory/namespaces/{NS}/kinds",
            json={"strict": False, "packages": ["counterparty"]},
        )
        assert resp.status_code == 200
        return resp.json()["merge"]

    @staticmethod
    def _register_v2(conn, settings, priority: list[str]) -> None:
        pack = {**_pack(priority), "version": 2}
        open_registry(conn, settings).register(pack)

    def test_new_version_of_unversioned_pack_remerges(self, client, stores, settings):
        conn, graph, *_ = stores
        _enable(stores, settings)
        assert "error" not in self._put(client)
        assert self._put(client) == {"nodes": 0, "skipped": True}
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]

        # Новая версия пакета (он включён без версии): приоритет у CRM. Настройка
        # namespace не меняется, но узлы сведены по прежнему sourcePriority.
        self._register_v2(conn, settings, ["crm:*"])
        assert self._put(client) == {"nodes": 3}
        assert _node(graph)["props"]["name"] == CRM_ATTRS["name"]
        assert self._put(client) == {"nodes": 0, "skipped": True}

    def test_repeat_after_merge_error_remerges(self, client, stores, settings, monkeypatch):
        import platform_memory.server.memory_api as api_mod

        conn, graph, *_ = stores
        _enable(stores, settings)
        assert "error" not in self._put(client)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        self._register_v2(conn, settings, ["crm:*"])

        real = api_mod.merge_namespace_nodes

        def failing(*_a, **_k):
            raise RuntimeError("БД занята")

        monkeypatch.setattr(api_mod, "merge_namespace_nodes", failing)
        assert self._put(client) == {"error": "БД занята"}
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]

        monkeypatch.setattr(api_mod, "merge_namespace_nodes", real)
        assert self._put(client) == {"nodes": 3}
        assert _node(graph)["props"]["name"] == CRM_ATTRS["name"]

    @staticmethod
    def _put_refs(client, packages: list[str]) -> dict:
        resp = client.put(
            f"/api/memory/namespaces/{NS}/kinds",
            json={"strict": False, "packages": packages},
        )
        assert resp.status_code == 200
        return resp.json()["merge"]

    def test_rollback_after_reconcile_by_newer_version_remerges(self, client, stores, settings):
        """Сценарий D: сверка переписала узел по новой версии пакета — откат настройки на
        прежнюю версию пересводит узлы, хотя отпечаток равен прежнему."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        assert "error" not in self._put(client)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]

        self._register_v2(conn, settings, ["crm:*"])
        _run(settings, conn, _crm("crm-2", T3, attrs={"name": "Ромашка (CRM-2)", "inn": INN}))
        assert _node(graph)["props"]["name"] == "Ромашка (CRM-2)"
        assert open_registry(conn, settings).merged_priorities(NS) is None

        assert self._put_refs(client, ["counterparty@1"]) == {"nodes": 3}
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]
        assert self._put_refs(client, ["counterparty@1"]) == {"nodes": 0, "skipped": True}

    def test_version_returning_old_priority_after_reconcile_remerges(
        self, client, stores, settings
    ):
        """Сценарий H: v3 возвращает приоритет v1 после сверки по v2 — пересведение."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        assert "error" not in self._put(client)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())

        self._register_v2(conn, settings, ["crm:*"])
        _run(settings, conn, _crm("crm-2", T3, attrs={"name": "Ромашка (CRM-2)", "inn": INN}))
        assert _node(graph)["props"]["name"] == "Ромашка (CRM-2)"

        open_registry(conn, settings).register({**_pack(["erp:*"]), "version": 3})
        assert self._put(client) == {"nodes": 3}
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]

    def test_reconcile_by_merged_priority_keeps_fingerprint(self, client, stores, settings):
        """Сверка по тому же sourcePriority отпечаток не сбрасывает: PUT без изменений
        по-прежнему ``skipped``."""
        conn, *_ = stores
        _enable(stores, settings)
        assert "error" not in self._put(client)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        assert open_registry(conn, settings).merged_priorities(NS) == {"legal_entity": ["erp:*"]}
        assert self._put(client) == {"nodes": 0, "skipped": True}

    def test_dry_run_keeps_fingerprint(self, client, stores, settings):
        conn, *_ = stores
        _enable(stores, settings)
        assert "error" not in self._put(client)
        self._register_v2(conn, settings, ["crm:*"])
        reconcile(settings, snapshot=_crm(), namespace=NS, conn=conn, dry_run=True)
        assert open_registry(conn, settings).merged_priorities(NS) == {"legal_entity": ["erp:*"]}

    def test_catalog_read_before_lock_resets_fingerprint(
        self, client, stores, settings, monkeypatch
    ):
        """Гонка: сверка прочитала каталог v1 до лока, PUT успел пересвести по v2 и
        записать отпечаток — сверка пишет узлы по v1 и сбрасывает отпечаток."""
        from platform_memory.domain import registry as reg_mod

        conn, graph, *_ = stores
        _enable(stores, settings)
        assert "error" not in self._put(client)
        _run(settings, conn, _erp())
        stale = open_registry(conn, settings).catalog_for(NS)
        self._register_v2(conn, settings, ["crm:*"])
        assert self._put(client) == {"nodes": 2}

        with monkeypatch.context() as m:
            m.setattr(reg_mod.KindRegistry, "catalog_for", lambda self, ns: stale)
            _run(settings, conn, _crm())
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]
        assert open_registry(conn, settings).merged_priorities(NS) is None

        assert self._put(client) == {"nodes": 3}
        assert _node(graph)["props"]["name"] == CRM_ATTRS["name"]

    @pytest.mark.parametrize("searchable", [True, False], ids=["searchable", "plain"])
    def test_j_put_skip_while_reconcile_before_reset(
        self, client, stores, settings, monkeypatch, searchable
    ):
        """Сценарий J: сверка по v2 держит лок, узел уже записан, отпечаток v1 ещё не
        сброшен — ``PUT packages=["counterparty@1"]`` ждёт лока и пересводит по v1, а не
        отвечает ``skipped`` по закоммиченному отпечатку. Без ``searchable``-видов
        ``reindex_namespace`` выходит, не взяв лок, — лок берёт само сравнение."""
        import threading

        from platform_memory.core import db as dbmod
        from platform_memory.domain import registry as reg_mod

        conn, graph, *_ = stores

        def pack(priority: list[str], version: int) -> dict:
            out = {**_pack(priority), "version": version}
            if not searchable:
                out["kinds"] = [
                    {k: v for k, v in kind.items() if k != "searchable"} for kind in out["kinds"]
                ]
            return out

        reg = open_registry(conn, settings)
        reg.ensure_schema()
        reg.register(pack(["erp:*"], 1))
        reg.put_settings(NS, strict=False, packages=["counterparty"])
        assert "error" not in self._put(client)
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]
        reg.register(pack(["crm:*"], 2))

        # Сверка останавливается под локом: узел записан по v2, отпечаток ещё v1.
        entered, release = threading.Event(), threading.Event()
        real_reset = reg_mod.KindRegistry.reset_stale_merge

        def paused_reset(self, ns, priorities):
            entered.set()
            assert release.wait(30)
            return real_reset(self, ns, priorities)

        monkeypatch.setattr(reg_mod.KindRegistry, "reset_stale_merge", paused_reset)
        errors: list[BaseException] = []
        put_result: dict = {}

        def reconcile_by_v2():
            other = dbmod.connect(settings)
            try:
                _run(
                    settings,
                    other,
                    _crm("crm-2", T3, attrs={"name": "Ромашка (CRM-2)", "inn": INN}),
                )
            except BaseException as exc:  # noqa: BLE001 — отдать в основной поток
                errors.append(exc)
                entered.set()
            finally:
                other.close()

        def put_v1():
            try:
                put_result["merge"] = self._put_refs(client, ["counterparty@1"])
            except BaseException as exc:  # noqa: BLE001 — отдать в основной поток
                errors.append(exc)

        rec = threading.Thread(target=reconcile_by_v2)
        rec.start()
        assert entered.wait(30)
        put = threading.Thread(target=put_v1)
        put.start()
        # Ждём, пока PUT встанет на лок (или, при гонке, успеет ответить).
        for _ in range(300):
            if not put.is_alive():
                break
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
                if cur.fetchone()[0]:
                    break
            put.join(0.1)
        release.set()
        rec.join(30)
        put.join(30)
        assert not rec.is_alive() and not put.is_alive()
        assert not errors, errors

        assert put_result["merge"] == {"nodes": 3}
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]
        assert open_registry(conn, settings).merged_priorities(NS) == {"legal_entity": ["erp:*"]}
        assert self._put_refs(client, ["counterparty@1"]) == {"nodes": 0, "skipped": True}

    def test_reconcile_on_settings_table_without_fingerprint_column(self, stores, settings):
        """Таблица настроек до MEM-ADR-022 (без ``merged_priority``): сверка не падает,
        отпечатка нет."""
        conn, graph, *_ = stores
        _enable(stores, settings)
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE public.{settings.namespace_settings_table} "
                "DROP COLUMN merged_priority"
            )
        _run(settings, conn, _erp())
        _run(settings, conn, _crm())
        assert _node(graph)["props"]["name"] == ERP_ATTRS["name"]
        assert open_registry(conn, settings).merged_priorities(NS) is None
