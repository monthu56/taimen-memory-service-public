"""Сведение версий сущности по источникам (MEM-ADR-022): чистая логика, без БД."""

from __future__ import annotations

import pytest

from platform_memory.context.resolve import Candidate, entity_text, resolve_candidates
from platform_memory.core.kinds import AttributesError, KindCatalog, PackError, parse_pack
from platform_memory.domain.merge import (
    SINCE_KEY,
    merge_by_entity,
    merge_rows,
    node_versions,
    rank_by_catalog,
    stamp_since,
)

pytestmark = pytest.mark.unit

NS = "tenant:t1:ws:crm"
INN = "7700000001"
ERP = {"name": "ООО «Ромашка»", "inn": INN, "kpp": "770001001", "ogrn": "1027700000001"}
CRM = {"name": "Ромашка (CRM)", "inn": INN}


def _row(source: str, attrs: dict, *, vf: str, rid: int, title: str = "", since=None) -> dict:
    payload = {"title": title or INN, "attributes": attrs, "aliases": [], "scopes": []}
    if since is not None:
        payload[SINCE_KEY] = since
    return {
        "id": rid,
        "namespace": NS,
        "kind": "legal_entity",
        "key": INN,
        "source": source,
        "scope": "",
        "snapshot_id": f"{source}@{rid}",
        "payload": payload,
        "valid_from": vf,
        "valid_to": None,
    }


def _catalog(priority: list[str] | None) -> KindCatalog:
    kind: dict = {"kind": "legal_entity"}
    if priority is not None:
        kind["sourcePriority"] = priority
    return KindCatalog.build([parse_pack({"name": "cp", "version": 1, "kinds": [kind]})])


class TestMergeRows:
    def test_attribute_of_one_source_survives_another(self):
        erp = _row("erp:main", ERP, vf="2026-09-01T00:00:00Z", rid=1, title=ERP["name"])
        crm = _row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=2, title=CRM["name"])
        merged = merge_rows([erp, crm], rank_by_catalog(_catalog(["erp:*"])))
        assert merged["payload"]["attributes"] == ERP
        assert merged["payload"]["title"] == ERP["name"]
        assert merged["source"] == "erp:main"
        assert [s["source"] for s in merged["sources"]] == ["erp:main", "crm:main"]

    def test_priority_independent_of_input_order(self):
        erp = _row("erp:main", ERP, vf="2026-09-01T00:00:00Z", rid=9)
        crm = _row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=1)
        rank = rank_by_catalog(_catalog(["erp:*"]))
        assert merge_rows([erp, crm], rank)["payload"] == merge_rows([crm, erp], rank)["payload"]

    def test_without_priority_latest_since_wins_per_attribute(self):
        # Учётная система наблюдена позже (новая версия из-за kpp), но имя держит с T1.
        erp = _row(
            "erp:main",
            ERP,
            vf="2026-09-03T00:00:00Z",
            rid=1,
            since={"attributes": {"name": "2026-09-01T00:00:00Z"}},
        )
        crm = _row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=2)
        merged = merge_rows([erp, crm], rank_by_catalog(_catalog(None)))
        assert merged["payload"]["attributes"]["name"] == CRM["name"]
        assert merged["payload"]["attributes"]["kpp"] == ERP["kpp"]
        assert merged["source"] == "erp:main"  # старшая версия — по valid_from
        assert SINCE_KEY not in merged["payload"]

    def test_null_does_not_erase_and_title_equal_to_key_yields(self):
        erp = _row("erp:main", ERP, vf="2026-09-01T00:00:00Z", rid=1, title=ERP["name"])
        crm = _row("crm:main", {"kpp": None}, vf="2026-09-02T00:00:00Z", rid=2)
        merged = merge_rows([erp, crm])
        assert merged["payload"]["attributes"]["kpp"] == ERP["kpp"]
        assert merged["payload"]["title"] == ERP["name"]

    def test_unlisted_source_ranks_after_listed(self):
        catalog = _catalog(["erp:*", "crm:*"])
        other = _row("sheet:x", {"name": "X"}, vf="2026-09-09T00:00:00Z", rid=3)
        crm = _row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=2)
        merged = merge_rows([other, crm], rank_by_catalog(catalog))
        assert merged["payload"]["attributes"]["name"] == CRM["name"]

    def test_interval_aliases_scopes(self):
        a = _row("erp:main", ERP, vf="2026-09-01T00:00:00Z", rid=1)
        b = _row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=2)
        a["payload"].update(aliases=["A"], scopes=["workspace:a"])
        b["payload"].update(aliases=["B"], scopes=["workspace:b"])
        b["valid_to"] = "2026-09-05T00:00:00Z"
        merged = merge_rows([a, b])
        assert merged["payload"]["aliases"] == ["A", "B"]
        assert merged["payload"]["scopes"] == ["workspace:a", "workspace:b"]
        assert (merged["valid_from"], merged["valid_to"]) == (
            "2026-09-02T00:00:00Z",
            "2026-09-05T00:00:00Z",
        )

    def test_merge_by_entity_groups(self):
        a = _row("erp:main", ERP, vf="2026-09-01T00:00:00Z", rid=1)
        b = {**_row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=2), "key": "other"}
        assert list(merge_by_entity([a, b])) == [
            (NS, "legal_entity", INN),
            (NS, "legal_entity", "other"),
        ]


class TestNodeVisibility:
    """Узел и строка индекса видимость версий не расширяют (MEM-ADR-022, п.2а)."""

    def _pair(self, erp_scopes, crm_scopes):
        erp = _row("erp:main", ERP, vf="2026-09-01T00:00:00Z", rid=1, title=ERP["name"])
        crm = _row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=2, title=CRM["name"])
        erp["payload"]["scopes"] = erp_scopes
        crm["payload"]["scopes"] = crm_scopes
        return erp, crm

    def test_same_scopes_merge_all(self):
        erp, crm = self._pair(["workspace:w1"], ["workspace:w1"])
        assert node_versions([erp, crm]) == [erp, crm]

    def test_different_restricted_scopes_take_primary_class_only(self):
        erp, crm = self._pair(["workspace:w1"], ["workspace:w2"])
        rank = rank_by_catalog(_catalog(["erp:*"]))
        (merged,) = merge_by_entity([erp, crm], rank, node=True).values()
        assert merged["payload"]["scopes"] == ["workspace:w1"]
        assert merged["payload"]["attributes"] == ERP
        # Без приоритета старшая — более поздняя версия (CRM): реквизиты W1 не уходят в W2.
        (merged,) = merge_by_entity([erp, crm], node=True).values()
        assert merged["payload"]["scopes"] == ["workspace:w2"]
        assert merged["payload"]["attributes"] == CRM
        assert [s["source"] for s in merged["sources"]] == ["crm:main"]

    def test_public_class_wins_over_restricted(self):
        # Публичные атрибуты не пропадают у всех, у кого нет W1, даже если ERP старше.
        erp, crm = self._pair(["workspace:w1"], [])
        rank = rank_by_catalog(_catalog(["erp:*"]))
        (merged,) = merge_by_entity([erp, crm], rank, node=True).values()
        assert merged["payload"]["scopes"] == []
        assert merged["payload"]["attributes"] == CRM

    def test_relevance_only_class_wider_than_visibility(self):
        erp, crm = self._pair(["workspace:w1"], ["project:p"])
        assert node_versions([erp, crm]) == [crm]

    def test_scope_order_does_not_split_class(self):
        erp, crm = self._pair(["workspace:w1", "project:p"], ["project:p", "workspace:w1"])
        assert node_versions([erp, crm]) == [erp, crm]

    def test_ledger_readers_merge_every_visible_version(self):
        erp, crm = self._pair(["workspace:w1"], ["workspace:w2"])
        (merged,) = merge_by_entity([erp, crm]).values()
        assert merged["payload"]["attributes"]["kpp"] == ERP["kpp"]


class TestRequiredOnMerge:
    def _catalog(self):
        kind = {
            "kind": "legal_entity",
            "attributes": {
                "type": "object",
                "required": ["name", "inn"],
                "properties": {"inn": {"type": "string"}},
            },
        }
        pack = parse_pack({"name": "cp", "version": 1, "kinds": [kind]})
        return KindCatalog.build([pack], strict=True)

    def test_partial_contribution_skips_required_but_checks_types(self):
        catalog = self._catalog()
        assert catalog.check_entity("legal_entity", attributes={"inn": INN}, partial=True)
        with pytest.raises(AttributesError):
            catalog.check_entity("legal_entity", attributes={"inn": 1}, partial=True)
        with pytest.raises(AttributesError):
            catalog.check_entity("legal_entity", attributes={"inn": INN})

    def test_required_attributes_only_in_strict_mode(self):
        assert self._catalog().required_attributes("legal_entity") == ("name", "inn")
        lax = KindCatalog.build(self._catalog().packs, strict=False)
        assert lax.required_attributes("legal_entity") == ()


class TestStampSince:
    T1, T2 = "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"

    def test_new_version_stamps_observed_at(self):
        content = {"title": "t", "attributes": {"a": 1, "b": None}}
        assert stamp_since(content, None, None, self.T1) == {
            "title": self.T1,
            "attributes": {"a": self.T1},
        }

    def test_unchanged_value_keeps_time_changed_gets_new(self):
        old = {"title": "t", "attributes": {"a": 1, "b": 2}}
        new = {"title": "t", "attributes": {"a": 1, "b": 3}}
        # Прошлая версия без since (до MEM-ADR-022) — время значения = её valid_from.
        since = stamp_since(new, old, self.T1, self.T2)
        assert since == {"title": self.T1, "attributes": {"a": self.T1, "b": self.T2}}
        old2 = {**new, SINCE_KEY: since}
        assert stamp_since(new, old2, self.T2, "2026-09-03T00:00:00Z") == since


class TestSourcePriorityInPack:
    def test_parse_and_rank(self):
        catalog = _catalog(["erp:*", "crm:main"])
        assert catalog.source_rank("legal_entity", "erp:acme") == 0
        assert catalog.source_rank("legal_entity", "crm:main") == 1
        assert catalog.source_rank("legal_entity", "crm:other") == 2
        assert catalog.source_rank("unknown", "erp:acme") == 0
        spec = catalog.spec("legal_entity")
        assert spec is not None
        assert spec.to_payload()["sourcePriority"] == ["erp:*", "crm:main"]

    def test_absent_keeps_canonical_form(self):
        spec = _catalog(None).spec("legal_entity")
        assert spec is not None and "sourcePriority" not in spec.to_payload()

    @pytest.mark.parametrize("bad", [["a", "a"], [""], "erp:*", ["x" * 201]])
    def test_invalid(self, bad):
        pack = {"name": "cp", "version": 1, "kinds": [{"kind": "k", "sourcePriority": bad}]}
        with pytest.raises(PackError):
            parse_pack(pack)


class _Ledger:
    def __init__(self, rows):
        self.rows = rows

    def entities_by_keys(self, namespaces, keys, **_):
        return [r for r in self.rows if r["key"] in keys]

    def entities_by_aliases(self, namespaces, aliases, **_):
        return [r for r in self.rows if set(aliases) & set(r["payload"].get("aliases", []))]

    def entities_by_suffixes(self, *_, **__):
        return []


def test_resolve_channel_merges_sources_and_completes_alias_hits():
    erp = _row("erp:main", ERP, vf="2026-09-01T00:00:00Z", rid=1, title=ERP["name"])
    crm = _row("crm:main", CRM, vf="2026-09-02T00:00:00Z", rid=2, title=CRM["name"])
    crm["payload"]["aliases"] = ["ROMASHKA"]  # псевдоним — только у CRM
    catalog = _catalog(["erp:*"])
    rank = rank_by_catalog(catalog)
    for value in (INN, "ROMASHKA"):
        (hit,), _ = resolve_candidates(
            _Ledger([erp, crm]), [Candidate(value)], [NS], catalogs=[catalog], rank=rank
        )
        title, text = entity_text(hit)
        assert title == ERP["name"]
        assert f"kpp={ERP['kpp']}" in text
