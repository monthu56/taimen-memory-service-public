"""Доменные пакеты видов (MEM-ADR-020): разбор, каталог, строгий режим, idPatterns.

Чистые функции ``core.kinds`` и совместимость ``core.ontology`` — БД не нужна.
"""

from __future__ import annotations

import pytest

from platform_memory.core import ontology
from platform_memory.core.kinds import (
    BASE_KINDS,
    AttributesError,
    KindCatalog,
    PackError,
    UnknownKindError,
    UnknownRelationError,
    default_catalog,
    default_pack,
    parse_pack,
    schema_errors,
    version_key,
)

pytestmark = pytest.mark.unit

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
                "properties": {"status": {"type": "string", "enum": ["open", "closed"]}},
                "required": ["status"],
            },
        },
        {"kind": "service", "naturalKey": "{team}/{name}"},
    ],
    "relations": [
        {"relation": "blocks", "fromKinds": ["issue"], "toKinds": ["issue"]},
        {"relation": "owned_by", "temporal": False},
    ],
}


class TestParsePack:
    def test_roundtrip_and_defaults(self):
        pack = parse_pack(TRACKER)
        assert pack.ref == "tracker@1.0.0"
        assert [k.kind for k in pack.kinds] == ["issue", "service"]
        blocks, owned = pack.relations
        assert blocks.temporal is True and owned.temporal is False
        assert blocks.edge_label == "BLOCKS"
        # Каноническая форма стабильна: повторный разбор даёт то же содержимое.
        assert parse_pack(pack.to_payload()).canonical_json() == pack.canonical_json()

    @pytest.mark.parametrize(
        "patch, fragment",
        [
            ({"name": "Bad Name"}, "имя пакета"),
            ({"version": ""}, "версия"),
            ({"kinds": [{"kind": "document"}]}, "базовый вид"),
            ({"kinds": [{"kind": "Issue"}]}, "имя вида"),
            ({"kinds": [{"kind": "a"}, {"kind": "b", "kindAliases": ["a"]}]}, "уже объявлен"),
            ({"kinds": [{"kind": "a", "aliases": ["ticket"]}]}, "kindAliases"),
            ({"kinds": [{"kind": "a", "kindAliases": ["Bad Alias"]}]}, "имя вида"),
            ({"version": True}, "версия"),
            ({"relations": [{"relation": "r", "cardinality": "two"}]}, "cardinality"),
            ({"kinds": [{"kind": "a", "idPatterns": ["("]}]}, "регулярное"),
            ({"kinds": [{"kind": "a", "attributes": "x"}]}, "JSON Schema"),
            ({"relations": [{"relation": "r", "temporal": "yes"}]}, "temporal"),
            ({"relations": [{"relation": "r"}, {"relation": "r"}]}, "уже объявлена"),
        ],
    )
    def test_invalid(self, patch, fragment):
        with pytest.raises(PackError, match=fragment):
            parse_pack({**TRACKER, **patch})

    def test_numeric_version_is_coerced_to_string(self):
        pack = parse_pack({**TRACKER, "version": 1})
        assert pack.version == "1" and pack.ref == "tracker@1"

    def test_cardinality_defaults_to_many(self):
        pack = parse_pack(
            {**TRACKER, "relations": [{"relation": "a"}, {"relation": "b", "cardinality": "one"}]}
        )
        assert [r.cardinality for r in pack.relations] == ["many", "one"]
        # Значение по умолчанию не попадает в каноническую форму.
        assert "cardinality" not in pack.to_payload()["relations"][0]

    def test_version_order_is_numeric(self):
        assert sorted(["1.10.0", "1.2.0", "1.9.1"], key=version_key) == [
            "1.2.0",
            "1.9.1",
            "1.10.0",
        ]


class TestCatalog:
    def test_non_strict_keeps_old_behaviour(self):
        cat = KindCatalog.build([parse_pack(TRACKER)])
        assert cat.check_entity("whatever") == "whatever"
        # Псевдоним вида приводится к каноническому имени и без строгого режима.
        assert cat.check_entity("ticket") == "issue"

    def test_strict_rejects_unknown_kind(self):
        cat = KindCatalog.build([parse_pack(TRACKER)], strict=True)
        with pytest.raises(UnknownKindError, match="whatever"):
            cat.check_entity("whatever")
        for base in BASE_KINDS:
            assert cat.check_entity(base) == base

    def test_strict_validates_key_and_attributes(self):
        cat = KindCatalog.build([parse_pack(TRACKER)], strict=True)
        assert (
            cat.check_entity("ticket", natural_key="PROJ-1", attributes={"status": "open"})
            == "issue"
        )
        with pytest.raises(AttributesError, match="naturalKey"):
            cat.check_entity("issue", natural_key="proj-1", attributes={"status": "open"})
        with pytest.raises(AttributesError, match="status"):
            cat.check_entity("issue", natural_key="PROJ-1", attributes={"status": "wip"})
        # Placeholder без атрибутов (attributes=None) схему атрибутов не проверяет.
        assert cat.check_entity("issue", natural_key="PROJ-1") == "issue"

    def test_first_pack_wins_on_conflict(self):
        other = parse_pack({"name": "other", "version": "1", "kinds": [{"kind": "issue"}]})
        cat = KindCatalog.build([parse_pack(TRACKER), other])
        assert cat.kinds["issue"].pack == "tracker"
        assert cat.conflicts == ["other@1:issue"]

    def test_id_patterns_extract_named_group(self):
        cat = KindCatalog.build([parse_pack(TRACKER)])
        assert cat.extract_ids("см. PROJ-42 и OPS-7, а не x-1") == [
            ("issue", "PROJ-42"),
            ("issue", "OPS-7"),
        ]
        assert cat.extract_ids("PROJ-42", ["service"]) == []
        assert cat.id_kind("PROJ-42") == "issue"
        assert cat.id_kind("PROJ-42 и ещё") is None

    def test_template_natural_key(self):
        spec = KindCatalog.build([parse_pack(TRACKER)]).spec("service")
        assert spec.build_key({"team": "pay", "name": "api"}) == "pay/api"
        with pytest.raises(AttributesError, match="team"):
            spec.build_key({"name": "api"})


SOFTWARE = {
    "name": "software-delivery",
    "version": 1,
    "kinds": [
        {"kind": "component", "naturalKey": "<repo>"},
        {"kind": "ui_call", "naturalKey": "<repo>:<path>:<line>"},
        {
            "kind": "endpoint",
            "naturalKey": "<METHOD> <path с параметрами как {}>",
            "aliases": ["<path с исходными именами параметров>"],
        },
        {
            "kind": "adr",
            "naturalKey": "<SERIES>-<number>",
            "aliases": ["ADR-<number>", "<SERIES>-ADR-<number>"],
        },
    ],
    "relations": [
        {"relation": "calls", "fromKinds": ["ui_call"], "toKinds": ["endpoint"]},
        {"relation": "part_of"},
    ],
}


class TestKeyTemplatesAndAliases:
    def test_key_aliases_from_attributes_then_key_parts(self):
        cat = KindCatalog.build([parse_pack(SOFTWARE)])
        attrs = {"path": "/api/v1/runs/{run_id}/checkpoints", "method": "GET"}
        assert cat.key_aliases("endpoint", "GET /api/v1/runs/{}/checkpoints", attrs) == [
            "/api/v1/runs/{run_id}/checkpoints"
        ]
        assert cat.key_aliases("adr", "TAI-0042") == ["ADR-0042", "TAI-ADR-0042"]
        # Значения плейсхолдера нет ни в атрибутах, ни в ключе — форма пропускается.
        assert cat.key_aliases("endpoint", "GET /x/{}") == ["/x/{}"]
        assert cat.key_aliases("component", "control-plane") == []
        assert cat.key_aliases("unknown", "x") == []

    def test_strict_checks_key_shape_by_template(self):
        cat = KindCatalog.build([parse_pack(SOFTWARE)], strict=True)
        assert cat.check_entity("ui_call", natural_key="web:src/a/[id]/page.tsx:38") == "ui_call"
        with pytest.raises(AttributesError, match="шаблону"):
            cat.check_entity("ui_call", natural_key="web-src-page")

    def test_build_key_from_angle_placeholders(self):
        spec = KindCatalog.build([parse_pack(SOFTWARE)]).spec("ui_call")
        assert spec.build_key({"repo": "web", "path": "a.tsx", "line": 3}) == "web:a.tsx:3"
        with pytest.raises(AttributesError, match="line"):
            spec.build_key({"repo": "web", "path": "a.tsx"})


class TestRelationCheck:
    def test_non_strict_accepts_any_relation(self):
        cat = KindCatalog.build([parse_pack(SOFTWARE)])
        assert cat.check_relation("Imports", "x", "y") == "imports"

    def test_strict_checks_declaration_and_end_kinds(self):
        cat = KindCatalog.build([parse_pack(SOFTWARE)], strict=True)
        assert cat.check_relation("calls", "ui_call", "endpoint") == "calls"
        assert cat.check_relation("part_of", "anything", "else") == "part_of"  # пусто — любой
        with pytest.raises(UnknownRelationError, match="imports"):
            cat.check_relation("imports", "ui_call", "endpoint")
        with pytest.raises(UnknownRelationError, match="fromKinds"):
            cat.check_relation("calls", "endpoint", "endpoint")
        with pytest.raises(UnknownRelationError, match="toKinds"):
            cat.check_relation("calls", "ui_call", "ui_call")


class TestSchemaSubset:
    def test_types_and_nesting(self):
        schema = {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        }
        assert schema_errors({"n": 2, "tags": ["a"]}, schema) == []
        errors = schema_errors({"n": True, "tags": [1], "x": 1}, schema)
        assert any("$.n" in e for e in errors)
        assert any("$.tags[0]" in e for e in errors)
        assert any("$.x" in e for e in errors)


class TestDefaultPackReplacesOntologyCode:
    def test_default_pack_is_data(self):
        pack = default_pack()
        assert pack.name == "default"
        kinds = {k.kind for k in pack.kinds}
        assert {"decision", "task", "question", "meeting", "project"} <= kinds
        assert ontology.CORE_TYPES == frozenset(kinds - ontology.SYNTHETIC_TYPES)

    def test_natural_id_resolver_uses_pack_patterns(self):
        assert ontology.is_natural_id("BIZ-010")
        assert ontology.is_natural_id(" T-001 ")
        assert not ontology.is_natural_id("T-1")
        assert not ontology.is_natural_id("xBIZ-010")
        assert ontology.id_type("Q-024") == "question"
        assert ontology.id_type("M-100") == "meeting"
        assert ontology.id_type("nothing") is None

    def test_resolver_takes_patterns_from_given_catalog(self):
        cat = KindCatalog.build([parse_pack(TRACKER)])
        assert ontology.id_type("PROJ-42", cat) == "issue"
        assert not ontology.is_natural_id("T-001", cat)  # T- — шаблон default, не tracker
        assert default_catalog().id_kind("BIZ-010") == "decision"


def test_tenant_pack_ref_and_split() -> None:
    """K007: пакет арендатора адресуется ``tenant:<имя>[@<версия>]``; владелец не в хэше."""
    import dataclasses

    from platform_memory.core.kinds import split_pack_ref

    pack = parse_pack({"name": "fleet", "version": 1, "kinds": [{"kind": "vehicle"}]})
    owned = dataclasses.replace(pack, owner="tenant:t1")
    assert (pack.ref, owned.ref, owned.ref_name) == ("fleet@1", "tenant:fleet@1", "tenant:fleet")
    assert owned.canonical_json() == pack.canonical_json()
    assert split_pack_ref("tenant:fleet@1") == (True, "fleet", "1")
    assert split_pack_ref(" tenant:fleet ") == (True, "fleet", "")
    assert split_pack_ref("fleet@2") == (False, "fleet", "2")
