"""Поиск по смыслу по сущностям (MEM-ADR-020, амендмент 2026-09-28, K005) без БД.

``searchable`` у вида пакета: разбор и отказы, текст эмбеддинга, каноническая форма
прежних пакетов; ``sync_entities`` — индекс следует открытым версиям журнала
(индексирует открытое и изменённое, снимает закрытое, вид без ``searchable`` не
индексирует, эмбеддер вызывается только для нового текста).
"""

from __future__ import annotations

import pytest

from platform_memory.core.kinds import KindCatalog, PackError, parse_pack
from platform_memory.domain.searchable import sync_entities

pytestmark = pytest.mark.unit

CATALOG_PACK = {
    "name": "catalog",
    "version": "1",
    "kinds": [
        {
            "kind": "offering",
            "attributes": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "okpd2": {"type": "string"},
                    "tags": {"type": "array"},
                },
            },
            "searchable": {"fields": ["description", "tags"]},
        },
        {"kind": "unit"},
    ],
}


def _catalog(pack: dict = CATALOG_PACK) -> KindCatalog:
    return KindCatalog.build([parse_pack(pack)])


class TestSearchableSpec:
    def test_parsed_and_in_payload(self):
        pack = parse_pack(CATALOG_PACK)
        offering, unit = pack.kinds
        assert offering.searchable == ("description", "tags")
        assert offering.to_payload()["searchable"] == {"fields": ["description", "tags"]}
        assert unit.searchable == ()
        assert "searchable" not in unit.to_payload()

    def test_pack_without_searchable_keeps_canonical_form(self):
        """Пакеты, зарегистрированные раньше, не меняют хэш версии."""
        plain = {"name": "p", "version": "1", "kinds": [{"kind": "unit"}]}
        assert "searchable" not in parse_pack(plain).canonical_json()

    @pytest.mark.parametrize(
        "searchable",
        [
            [],
            {"fields": []},
            {"fields": "description"},
            {"fields": ["bad name"]},
            {"fields": ["description", "description"]},
            {"fields": [f"f{i}" for i in range(21)]},
            {"fields": ["price"]},  # не объявлен в attributes.properties
        ],
    )
    def test_invalid_rejected(self, searchable):
        kinds = [{**CATALOG_PACK["kinds"][0], "searchable": searchable}]
        with pytest.raises(PackError, match="searchable"):
            parse_pack({**CATALOG_PACK, "kinds": kinds})

    def test_fields_free_without_declared_properties(self):
        pack = parse_pack(
            {"name": "p", "version": "1", "kinds": [{"kind": "x", "searchable": {"fields": ["a"]}}]}
        )
        assert pack.kinds[0].searchable == ("a",)

    def test_search_text_title_and_fields_in_order(self):
        spec = _catalog().spec("offering")
        text = spec.search_text(
            "Ноутбук 14", {"okpd2": "26.20", "tags": ["портативный", "офис"], "description": " x "}
        )
        assert text == "Ноутбук 14\ndescription: x\ntags: портативный, офис"
        assert spec.search_text("Ноутбук", {"description": None, "tags": []}) == "Ноутбук"
        assert _catalog().spec("unit").search_text("шт", {}) == ""

    def test_catalog_lists_searchable_kinds(self):
        catalog = _catalog()
        assert sorted(catalog.searchable_kinds()) == ["offering"]
        assert catalog.to_payload()["searchable"] == {"offering": ["description", "tags"]}


# --- sync_entities на подменах журнала и индекса ---


class _Ledger:
    def __init__(self):
        self.open: dict[tuple[str, str], dict] = {}

    def put(self, kind, key, title, attributes=None, scopes=None, source="src"):
        self.open[(kind, key)] = {
            "kind": kind,
            "key": key,
            "source": source,
            "scope": "",
            "snapshot_id": "s1",
            "valid_from": "2026-09-01T00:00:00Z",
            "payload": {"title": title, "attributes": attributes or {}, "scopes": scopes or []},
        }

    def open_entity_versions(self, ns, kinds, keys=None, *, rank=None):
        return [
            v
            for (kind, key), v in sorted(self.open.items())
            if kind in kinds and (keys is None or key in keys)
        ]


class _Index:
    def __init__(self):
        self.data: dict[tuple[str, str], tuple] = {}

    def rows(self, ns, kinds=None):
        return {i: r for i, (r, _) in self.data.items() if kinds is None or i[0] in kinds}

    def delete(self, ns, idents):
        gone = [i for i in idents if i in self.data]
        for i in gone:
            del self.data[i]
        return len(gone)

    def update_meta(self, rows):
        for r in rows:
            self.data[r.ident] = (r, self.data[r.ident][1])
        return len(rows)

    def upsert(self, rows, embeddings):
        for r, e in zip(rows, embeddings, strict=True):
            self.data[r.ident] = (r, e)
        return len(rows)


class _Embedder:
    dim = 2

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def world():
    ledger, index, emb = _Ledger(), _Index(), _Embedder()

    def sync(idents=None, catalog=None):
        return sync_entities(index, ledger, "ns", catalog or _catalog(), lambda: emb, idents=idents)

    return ledger, index, emb, sync


class TestSyncEntities:
    def test_open_indexes_searchable_kind_only(self, world):
        ledger, index, emb, sync = world
        ledger.put("offering", "N-1", "Ноутбук", {"description": "портативный компьютер"})
        ledger.put("unit", "pcs", "штука")
        counts = sync([("offering", "N-1"), ("unit", "pcs")])
        assert counts["indexed"] == 1
        assert list(index.data) == [("offering", "N-1")]
        row = index.data[("offering", "N-1")][0]
        assert row.text == "Ноутбук\ndescription: портативный компьютер"
        assert row.source_path == "snapshot:src/s1"
        assert emb.calls == [[row.text]]

    def test_change_reembeds_only_new_text(self, world):
        ledger, index, emb, sync = world
        ledger.put("offering", "N-1", "Ноутбук", {"description": "a"})
        sync([("offering", "N-1")])
        # Смена атрибута вне fields: текст тот же — вектор не пересчитывается.
        ledger.put("offering", "N-1", "Ноутбук", {"description": "a", "okpd2": "26"}, ["team:x"])
        counts = sync([("offering", "N-1")])
        assert (counts["indexed"], counts["updated"]) == (0, 1)
        assert index.data[("offering", "N-1")][0].scopes == ["team:x"]
        ledger.put("offering", "N-1", "Ноутбук", {"description": "b"}, ["team:x"])
        counts = sync([("offering", "N-1")])
        assert counts["indexed"] == 1
        assert len(emb.calls) == 2

    def test_closed_removed(self, world):
        ledger, index, _, sync = world
        ledger.put("offering", "N-1", "Ноутбук")
        sync([("offering", "N-1")])
        del ledger.open[("offering", "N-1")]
        counts = sync([("offering", "N-1")])
        assert counts["removed"] == 1
        assert index.data == {}

    def test_untouched_keys_not_read_nor_removed(self, world):
        ledger, index, emb, sync = world
        ledger.put("offering", "N-1", "Ноутбук")
        ledger.put("offering", "N-2", "Монитор")
        sync()
        del ledger.open[("offering", "N-2")]
        sync([("offering", "N-1")])
        assert sorted(index.data) == [("offering", "N-1"), ("offering", "N-2")]
        assert sync([]) == {"indexed": 0, "updated": 0, "removed": 0, "unchanged": 0}

    def test_full_reindex_follows_catalog(self, world):
        ledger, index, _, sync = world
        ledger.put("offering", "N-1", "Ноутбук")
        ledger.put("unit", "pcs", "штука")
        assert sync()["indexed"] == 1
        # Вид unit стал индексируемым, offering — перестал.
        switched = {
            "name": "catalog",
            "version": "2",
            "kinds": [{"kind": "offering"}, {"kind": "unit", "searchable": {"fields": ["x"]}}],
        }
        counts = sync(catalog=_catalog(switched))
        assert (counts["indexed"], counts["removed"]) == (1, 1)
        assert list(index.data) == [("unit", "pcs")]

    def test_no_embedder_call_without_new_text(self, world):
        ledger, _, _, _ = world
        index = _Index()

        def boom():
            raise AssertionError("эмбеддер не нужен")

        ledger.put("unit", "pcs", "штука")
        counts = sync_entities(index, ledger, "ns", _catalog(), boom, idents=[("unit", "pcs")])
        assert counts["indexed"] == 0
