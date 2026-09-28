"""Contract-тесты схем OpenAPI маршрутов ядра (MEM-ADR-020, амендмент 2026-09-28).

Спека генерируется приложением (``GET /openapi.json``). Проверяется:

- спека валидна: OpenAPI 3.1, каждая ``$ref`` разрешается, схема каждого компонента —
  корректная JSON Schema 2020-12 (``jsonschema`` приходит с extra ``mcp``; без него
  проверка схем пропускается, структурные проверки идут всегда);
- новые схемы четырёх изменений принимают примеры контракта и отвергают неверные;
- прежние тела запросов и ответов (примеры из INTEGRATION.md) по-прежнему валидны —
  контракт расширен аддитивно.
"""

from __future__ import annotations

from typing import Any

import pytest

from platform_memory.server import app as app_mod

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app_mod.app.openapi()


def _refs(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _validator(spec: dict[str, Any], name: str):
    jsonschema = pytest.importorskip("jsonschema")
    root = {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
    return jsonschema.Draft202012Validator(root)


def _ok(spec, name: str, value: Any) -> None:
    errors = [e.message for e in _validator(spec, name).iter_errors(value)]
    assert not errors, f"{name}: {errors}"


def _bad(spec, name: str, value: Any) -> None:
    assert not _validator(spec, name).is_valid(value), f"{name} принял {value!r}"


def _body_schema(spec, path: str) -> str:
    ref = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    return ref["$ref"].rsplit("/", 1)[-1]


def _response_schema(spec, path: str, method: str, status: str) -> str:
    content = spec["paths"][path][method]["responses"][status]["content"]["application/json"]
    return content["schema"]["$ref"].rsplit("/", 1)[-1]


class TestSpecIsValid:
    def test_openapi_31_and_all_refs_resolve(self, spec):
        assert spec["openapi"].startswith("3.1")
        schemas = spec["components"]["schemas"]
        for ref in _refs(spec):
            assert ref.startswith("#/components/schemas/"), ref
            assert ref.rsplit("/", 1)[-1] in schemas, ref

    def test_component_schemas_are_valid_json_schema(self, spec):
        jsonschema = pytest.importorskip("jsonschema")
        for name, schema in spec["components"]["schemas"].items():
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError as exc:  # pragma: no cover - сообщение для отладки
                pytest.fail(f"{name}: {exc.message}")

    def test_routes_reference_contract_schemas(self, spec):
        assert _body_schema(spec, "/api/memory/reconcile") == "ReconcileIn"
        assert _body_schema(spec, "/api/memory/packages") == "PackIn"
        assert _body_schema(spec, "/api/memory/context/typed") == "TypedContextIn"
        assert _response_schema(spec, "/api/memory/reconcile", "post", "200") == "ReconcileResult"
        assert (
            _response_schema(spec, "/api/memory/reconcile", "post", "409") == "SnapshotStaleError"
        )
        assert _response_schema(spec, "/api/memory/packages", "get", "200") == "PackageList"
        assert _response_schema(spec, "/api/memory/packages", "post", "201") == "PackRegistered"
        assert (
            _response_schema(spec, "/api/memory/context/typed", "post", "200")
            == "TypedContextResult"
        )
        assert _body_schema(spec, "/api/memory/entities:query") == "EntitiesQueryIn"
        assert (
            _response_schema(spec, "/api/memory/entities:query", "post", "200")
            == "EntitiesQueryResult"
        )
        # K004–K007 реализованы: неисполняемых полей контракта больше нет.
        for path in ("/api/memory/reconcile", "/api/memory/context/typed", "/api/memory/packages"):
            assert "501" not in spec["paths"][path]["post"]["responses"], path

    def test_existing_paths_kept(self, spec):
        """Инвариант №2: ни один маршрут ядра не пропал."""
        for path in (
            "/api/memory/packages",
            "/api/memory/packages/{name}",
            "/api/memory/namespaces/{namespace}/kinds",
            "/api/memory/reconcile",
            "/api/memory/context/typed",
            "/api/memory/context",
        ):
            assert path in spec["paths"], path


SNAP = {
    "pack": "code@1",
    "source": "git:web-app",
    "scope": "web-app",
    "snapshotId": "web-app@9ab1c2d",
    "observedAt": "2026-09-23T08:47:12+00:00",
    "entities": [{"kind": "ui_call", "key": "web-app:src/runs/page.tsx:38"}],
    "relations": [],
    "namespace": "tenant:t:ws:w",
    "scopes": ["workspace:w"],
}

# Ответ сверки до амендмента 2026-09-28 (INTEGRATION.md) — без stateToken и conflicts.
RESULT_BEFORE = {
    "source": "git:web-app",
    "scope": "web-app",
    "namespace": "tenant:t:ws:w",
    "snapshot_id": "web-app@9ab1c2d",
    "observed_at": "2026-09-23T08:47:12Z",
    "pack": "code@1",
    "opened": 9,
    "closed": 0,
    "unchanged": 0,
    "superseded": 0,
    "entities": {"opened": 3, "closed": 0, "unchanged": 0, "superseded": 0},
    "relations": {
        "opened": 6,
        "closed": 0,
        "unchanged": 0,
        "superseded": 0,
        "pending": 1,
        "resolved": 0,
        "retried": 0,
    },
    "changes": {
        "opened": [{"kind": "ui_call", "key": "web-app:src/runs/page.tsx:38"}],
        "changed": [],
        "closed": [],
        "limit": 1000,
        "truncated": False,
    },
    "duplicate": False,
}


class TestEntitiesQueryContract:
    """K030: перечень сущностей вида с фильтрами."""

    def test_request(self, spec):
        _ok(
            spec,
            "EntitiesQueryIn",
            {
                "namespaces": ["tenant:t1"],
                "kinds": ["license"],
                "where": [{"attr": "validUntil", "op": "lte", "value": "2026-12-31"}],
                "asOf": "2026-09-01T00:00:00Z",
                "limit": 500,
                "cursor": "WzEsImxpY2Vuc2UiXQ",
            },
        )
        _bad(spec, "EntitiesQueryIn", {"kinds": []})
        _bad(spec, "EntitiesQueryIn", {"kinds": ["license"], "limit": 501})
        _bad(spec, "EntitiesQueryIn", {"kinds": ["license"], "where": [{"attr": "a", "op": "x"}]})

    def test_result(self, spec):
        item = {
            "kind": "license",
            "key": "lic:1",
            "namespace": "tenant:t1",
            "title": "Лицензия",
            "attributes": {"validUntil": "2026-12-31"},
            "source": "erp:main",
            "scope": "",
            "snapshot_id": "s1",
            "source_path": "snapshot:erp:main/s1",
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_to": None,
        }
        _ok(spec, "EntitiesQueryResult", {"items": [item], "nextCursor": None})
        _bad(spec, "EntitiesQueryResult", {"items": [item]})


class TestReconcileContract:
    def test_previous_body_still_valid(self, spec):
        _ok(spec, "ReconcileIn", SNAP)

    def test_dry_run_and_expected_state(self, spec):
        _ok(spec, "ReconcileIn", {**SNAP, "dryRun": True})
        _ok(spec, "ReconcileIn", {**SNAP, "expectedState": "st-1"})
        _bad(spec, "ReconcileIn", {**SNAP, "dryRun": "yes"})
        _bad(spec, "ReconcileIn", {**SNAP, "expectedState": "x" * 129})

    def test_previous_result_still_valid(self, spec):
        _ok(spec, "ReconcileResult", RESULT_BEFORE)

    def test_result_with_state_and_conflicts(self, spec):
        _ok(
            spec,
            "ReconcileResult",
            {
                **RESULT_BEFORE,
                "dryRun": True,
                "stateToken": "st-2",
                "conflicts": {
                    "items": [{"kind": "offering", "key": "SKU-1", "source": "onec:main"}],
                    "limit": 1000,
                    "truncated": False,
                },
            },
        )

    def test_snapshot_stale_error(self, spec):
        detail = {
            "code": "snapshot_stale",
            "message": "состояние изменилось",
            "expectedState": "st-1",
            "stateToken": "st-2",
        }
        _ok(spec, "SnapshotStaleError", {"detail": detail})
        _bad(spec, "SnapshotStaleError", {"detail": {**detail, "code": "other"}})
        _bad(spec, "SnapshotStaleError", {"detail": {"code": "snapshot_stale"}})


PACK = {
    "name": "code",
    "version": 1,
    "kinds": [
        {"kind": "component", "naturalKey": "<repo>"},
        {
            "kind": "endpoint",
            "naturalKey": "<METHOD> <path>",
            "aliases": ["<path>"],
            "kindAliases": ["route"],
            "idPatterns": ["\\bGET\\s+/\\S+"],
            "attributes": {"type": "object", "properties": {"method": {"type": "string"}}},
        },
    ],
    "relations": [
        {"relation": "part_of", "fromKinds": ["endpoint"], "toKinds": ["component"]},
        {
            "relation": "owned_by",
            "fromKinds": ["component"],
            "toKinds": ["team"],
            "cardinality": "one",
        },
    ],
}


class TestPackContract:
    def test_previous_pack_still_valid(self, spec):
        _ok(spec, "PackIn", PACK)
        _ok(spec, "PackIn", {**PACK, "version": "1.0.0", "description": "код"})

    def test_searchable_kind(self, spec):
        kind = {"kind": "offering", "searchable": {"fields": ["name", "description"]}}
        _ok(spec, "PackIn", {**PACK, "kinds": [kind]})
        _ok(spec, "SearchableSpec", {"fields": ["okpd2"]})
        _bad(spec, "SearchableSpec", {"fields": []})
        _bad(spec, "SearchableSpec", {"fields": ["bad name"]})
        _bad(spec, "SearchableSpec", {"fields": [f"f{i}" for i in range(21)]})

    def test_tenant_pack(self, spec):
        _ok(spec, "PackIn", {**PACK, "scope": "tenant", "namespace": "tenant:t1"})
        _ok(spec, "PackIn", {**PACK, "scope": "common"})
        _bad(spec, "PackIn", {**PACK, "scope": "global"})

    def test_package_list_old_and_new_items(self, spec):
        old = {"name": "default", "versions": ["1"], "latest": "1", "builtin": True}
        tenant = {
            "name": "fleet",
            "versions": ["1"],
            "latest": "1",
            "builtin": False,
            "scope": "tenant",
            "namespace": "tenant:t1",
            "ref": "tenant:fleet",
        }
        _ok(spec, "PackageList", {"packages": [old, tenant]})
        _bad(spec, "PackageSummary", {**tenant, "scope": "private"})

    def test_tenant_pack_conflict(self, spec):
        assert _response_schema(spec, "/api/memory/packages", "post", "409") == "PackScopeConflict"
        detail = {"code": "kind_conflict", "message": "вид issue — общий"}
        _ok(spec, "PackScopeConflict", {"detail": detail})
        _ok(spec, "PackScopeConflict", {"detail": "версия иммутабельна"})
        _bad(spec, "PackScopeConflict", {"detail": {**detail, "code": "name_taken"}})

    def test_registered(self, spec):
        _ok(spec, "PackRegistered", {"status": "created", "pack": PACK})
        _bad(spec, "PackRegistered", {"status": "replaced", "pack": PACK})


TYPED = {
    "anchors": [{"kind": "issue", "value": "PROJ-1"}, {"value": "payment-bug"}],
    "traverse": [
        {"relation": "blocks", "direction": "out", "depth": 2, "limit": 20},
        {"relation": "assigned_to", "direction": "out", "from": "previous"},
    ],
    "as_of": "2026-09-05T00:00:00Z",
    "allow_semantic": False,
    "scope": {"namespaces": ["tenant:t:ws:w"]},
}


class TestTypedWhereContract:
    def test_previous_body_still_valid(self, spec):
        _ok(spec, "TypedContextIn", TYPED)

    @pytest.mark.parametrize(
        "clause",
        [
            {"attr": "okpd2", "op": "prefix", "value": "62.01"},
            {"attr": "status", "op": "eq", "value": "active"},
            {"attr": "unit", "op": "in", "value": ["796", "839"]},
            {"attr": "validUntil", "op": "gte", "value": "2026-10-01"},
            {"attr": "limit", "op": "lte", "value": 5000000},
            {"attr": "ktru", "op": "exists"},
            {"attr": "ktru", "op": "exists", "value": False},
        ],
    )
    def test_where_clause_ops(self, spec, clause):
        _ok(spec, "WhereClause", clause)
        _ok(spec, "TypedContextIn", {**TYPED, "where": [clause]})

    def test_where_clause_rejects(self, spec):
        _bad(spec, "WhereClause", {"attr": "okpd2", "op": "like", "value": "62"})
        _bad(spec, "WhereClause", {"attr": "bad attr", "op": "eq", "value": 1})
        _bad(spec, "WhereClause", {"op": "eq", "value": 1})
        too_many = [{"attr": "a", "op": "exists"}] * 21
        _bad(spec, "TypedContextIn", {**TYPED, "where": too_many})

    def test_where_in_traverse_step(self, spec):
        step = {
            "relation": "performs",
            "direction": "in",
            "where": [{"attr": "validUntil", "op": "gte", "value": "2026-10-01"}],
        }
        _ok(spec, "TraverseStepIn", step)
        _ok(spec, "TypedContextIn", {**TYPED, "traverse": [step]})

    def test_result_anchor_semantic_and_filtered(self, spec):
        result = {
            "as_of": "",
            "namespaces": ["tenant:t:ws:w"],
            "anchors": [
                {
                    "input": {"kind": "offering", "value": "ноутбук 14 дюймов"},
                    "matchedBy": "semantic",
                    "filtered": 2,
                    "resolved": [
                        {
                            "natural_key": "SKU-1",
                            "namespace": "tenant:t:ws:w",
                            "kind": "offering",
                            "method": "semantic",
                            "evidence": "inferred",
                            "score": 0.83,
                            "matchedOn": "entity",
                        }
                    ],
                }
            ],
            "unresolved": [],
            "sections": [],
            "facts": [],
            "used": {"entities": [], "facts": [], "snapshots": []},
            "sources": [],
            "trace_id": "ctx-1",
        }
        _ok(spec, "TypedContextResult", result)
        anchor = result["anchors"][0]["resolved"][0]
        _bad(spec, "AnchorMatch", {**anchor, "matchedOn": "graph"})
