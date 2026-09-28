"""Поиск по смыслу по сущностям на реальной БД (MEM-ADR-020, амендмент 2026-09-28, K005).

Приёмка задачи: сущность находится по описанию без совпадения слов; закрытая
сущность не находится; вид без ``searchable`` не индексируется. Плюс переиндексация
при включении пакета и смысловой канал ``/api/memory/context``.

Эмбеддер — детерминированная подмена «модели смысла»: слова одного понятия
(«ноутбук», «лэптоп», «портативный», «компьютер») дают один вектор. Текст запроса и
текст сущности общих слов не имеют — совпадение возможно только по смыслу.
"""

from __future__ import annotations

import math
import re

import pytest

from platform_memory.context import build_context
from platform_memory.context.typed import compile_typed_context
from platform_memory.domain.reconcile import reconcile
from platform_memory.domain.registry import open_registry
from platform_memory.domain.searchable import entity_index, reindex_namespace

pytestmark = pytest.mark.integration

NS = "tenant:t1:ws:k5"

CONCEPTS = (
    {"ноутбук", "лэптоп", "портативный", "компьютер"},
    {"кресло", "стул", "мебель", "сидение"},
    {"бумага", "листы", "a4"},
)


class ConceptEmbedder:
    """Слова одного понятия — один вектор; остальное — общий «шумовой» вектор."""

    def __init__(self, dim: int):
        self.dim = dim

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in re.findall(r"\w+", text.lower()):
            for i, words in enumerate(CONCEPTS):
                if token in words:
                    vec[i] += 1.0
        if not any(vec):
            vec[-1] = 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


def _pack(version: str, searchable: bool) -> dict:
    offering: dict = {
        "kind": "offering",
        "attributes": {
            "type": "object",
            "properties": {"description": {"type": "string"}, "okpd2": {"type": "string"}},
        },
    }
    if searchable:
        offering["searchable"] = {"fields": ["description"]}
    return {
        "name": "catalog",
        "version": version,
        "kinds": [offering, {"kind": "supplier"}],
    }


T1 = "2026-09-01T00:00:00Z"
T2 = "2026-09-10T00:00:00Z"

OFFERINGS = [
    {
        "kind": "offering",
        "key": "N-14",
        "title": "Ноутбук 14",
        "attributes": {"description": "лэптоп для офиса", "okpd2": "26.20.11"},
    },
    {
        "kind": "offering",
        "key": "CH-1",
        "title": "Кресло",
        "attributes": {"description": "мебель для офиса", "okpd2": "31.01.11"},
    },
]
# Вид без searchable: тот же смысл в названии, но индексироваться не должен.
SUPPLIER = {"kind": "supplier", "key": "S-1", "title": "Ноутбук сервис"}


@pytest.fixture
def embedder(settings):
    return ConceptEmbedder(settings.embedding_dim)


@pytest.fixture
def conn(stores):
    return stores[0]


def _setup(conn, settings, *, searchable: bool = True) -> None:
    reg = open_registry(conn, settings)
    reg.ensure_schema()
    reg.register(_pack("1", searchable))
    reg.put_settings(NS, strict=True, packages=["catalog@1"])


def _snapshot(sid: str, observed_at: str, entities: list[dict]) -> dict:
    return {
        "pack": "catalog",
        "source": "table:catalog",
        "scope": "",
        "snapshotId": sid,
        "observedAt": observed_at,
        "entities": entities,
    }


def _reconcile(conn, settings, embedder, snap: dict) -> dict:
    return reconcile(settings, snapshot=snap, namespace=NS, conn=conn, embedder=embedder)


def _typed(conn, settings, embedder, value: str, kind: str = "", **extra) -> dict:
    anchor = {"value": value, **({"kind": kind} if kind else {})}
    payload = {"anchors": [anchor], "namespaces": [NS], "allow_semantic": True, **extra}
    return compile_typed_context(settings, payload, conn=conn, embedder=embedder)


def _resolved(pack: dict) -> list[dict]:
    return pack["anchors"][0]["resolved"]


class TestEntitySemanticSearch:
    def test_found_by_description_without_shared_words(self, conn, settings, embedder):
        _setup(conn, settings)
        result = _reconcile(conn, settings, embedder, _snapshot("s1", T1, [*OFFERINGS, SUPPLIER]))
        assert result["search_index"]["indexed"] == 2

        pack = _typed(conn, settings, embedder, "портативный компьютер", "offering", semantic_k=1)
        (hit,) = _resolved(pack)
        assert (hit["natural_key"], hit["kind"]) == ("N-14", "offering")
        assert (hit["method"], hit["evidence"], hit["matchedOn"]) == (
            "semantic",
            "inferred",
            "entity",
        )
        assert hit["score"] == pytest.approx(1.0)
        # Цитата — источник версии сущности.
        assert pack["sources"][0]["node_key"] == "N-14"

    def test_without_allow_semantic_not_resolved(self, conn, settings, embedder):
        _setup(conn, settings)
        _reconcile(conn, settings, embedder, _snapshot("s1", T1, OFFERINGS))
        pack = _typed(conn, settings, embedder, "портативный компьютер", allow_semantic=False)
        assert pack["unresolved"]

    def test_kind_without_searchable_not_indexed(self, conn, settings, embedder):
        _setup(conn, settings)
        _reconcile(conn, settings, embedder, _snapshot("s1", T1, [*OFFERINGS, SUPPLIER]))
        rows = entity_index(conn, settings).rows(NS)
        assert sorted(rows) == [("offering", "CH-1"), ("offering", "N-14")]
        pack = _typed(conn, settings, embedder, "портативный компьютер", "supplier")
        assert pack["unresolved"]

    def test_closed_entity_not_found(self, conn, settings, embedder):
        _setup(conn, settings)
        _reconcile(conn, settings, embedder, _snapshot("s1", T1, OFFERINGS))
        result = _reconcile(conn, settings, embedder, _snapshot("s2", T2, OFFERINGS[1:]))
        assert result["search_index"]["removed"] == 1
        assert ("offering", "N-14") not in entity_index(conn, settings).rows(NS)
        pack = _typed(conn, settings, embedder, "портативный компьютер", "offering")
        assert "N-14" not in {r["natural_key"] for r in _resolved(pack)}

    def test_changed_text_reindexed(self, conn, settings, embedder):
        _setup(conn, settings)
        _reconcile(conn, settings, embedder, _snapshot("s1", T1, OFFERINGS))
        chair = {**OFFERINGS[1], "attributes": {"description": "пачка бумаги a4"}}
        result = _reconcile(conn, settings, embedder, _snapshot("s2", T2, [OFFERINGS[0], chair]))
        assert result["search_index"]["indexed"] == 1
        pack = _typed(conn, settings, embedder, "листы", "offering", semantic_k=1)
        assert [r["natural_key"] for r in _resolved(pack)] == ["CH-1"]

    def test_visibility_applies_to_index(self, conn, settings, embedder):
        _setup(conn, settings)
        reconcile(
            settings,
            snapshot=_snapshot("s1", T1, OFFERINGS),
            namespace=NS,
            scopes=["principal:owner"],
            conn=conn,
            embedder=embedder,
        )
        hidden = _typed(
            conn, settings, embedder, "компьютер", "offering", allowed_scopes=["principal:other"]
        )
        assert hidden["unresolved"]
        seen = _typed(
            conn, settings, embedder, "компьютер", "offering", allowed_scopes=["principal:owner"]
        )
        assert _resolved(seen)

    def test_entities_and_chunks_merged_by_score(self, conn, settings, embedder):
        """Фрагмент ближе по смыслу, но вид якоря отсекает его до среза semantic_k."""
        from platform_memory.retrieval import write_and_index_fact

        _setup(conn, settings)
        mixed = {**OFFERINGS[0], "attributes": {"description": "лэптоп и кресло"}}
        _reconcile(conn, settings, embedder, _snapshot("s1", T1, [mixed]))
        write_and_index_fact(
            settings,
            "note-laptops",
            "document",
            "Заметка",
            text="ноутбук",
            namespace=NS,
            embedder=embedder,
        )
        any_kind = _typed(conn, settings, embedder, "компьютер", semantic_k=2)
        hits = [(r["natural_key"], r["matchedOn"]) for r in _resolved(any_kind)]
        assert hits == [("note-laptops", "chunk"), ("N-14", "entity")]
        scores = [r["score"] for r in _resolved(any_kind)]
        assert scores[0] == pytest.approx(1.0) and scores[1] == pytest.approx(2 / math.sqrt(5))

        typed = _typed(conn, settings, embedder, "компьютер", "offering", semantic_k=1)
        assert [r["natural_key"] for r in _resolved(typed)] == ["N-14"]

    def test_reindex_when_pack_enabled(self, conn, settings, embedder):
        _setup(conn, settings, searchable=False)
        result = _reconcile(conn, settings, embedder, _snapshot("s1", T1, OFFERINGS))
        assert "search_index" not in result  # индексируемых видов нет — таблица не нужна
        reg = open_registry(conn, settings)
        reg.register(_pack("2", True))
        reg.put_settings(NS, strict=True, packages=["catalog@2"])
        counts = reindex_namespace(settings, conn, NS, reg.catalog_for(NS), lambda: embedder)
        assert counts["indexed"] == 2
        pack = _typed(conn, settings, embedder, "портативный компьютер", "offering", semantic_k=1)
        assert [r["natural_key"] for r in _resolved(pack)] == ["N-14"]
        # Пакет без searchable снова — индекс вида снимается.
        reg.put_settings(NS, strict=True, packages=["catalog@1"])
        counts = reindex_namespace(settings, conn, NS, reg.catalog_for(NS), lambda: embedder)
        assert counts["removed"] == 2
        assert entity_index(conn, settings).rows(NS) == {}

    def test_context_vector_channel_has_entity(self, conn, settings, embedder):
        _setup(conn, settings)
        _reconcile(conn, settings, embedder, _snapshot("s1", T1, OFFERINGS))
        pack = build_context(
            settings,
            {"query": "портативный компьютер", "namespaces": [NS], "k": 1},
            embedder=embedder,
            conn=conn,
        ).to_payload()
        items = [i for s in pack["sections"] for i in s["items"]]
        entity = next(i for i in items if i["id"] == "node:N-14")
        assert entity["kind"] == "entity"
        assert entity["signals"]["matched_on"] == "entity"
        assert entity["signals"]["channels"]["vector"] == 0
        assert entity["provenance"]["snapshot"]["snapshot_id"] == "s1"
        assert entity["source_path"] == "snapshot:table:catalog/s1"


class CountingEmbedder(ConceptEmbedder):
    """Считает вызовы: предпросмотр сверки эмбеддер звать не должен."""

    calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return super().embed(texts)


class TestDryRunLeavesIndex:
    """K004 × K005: ``dryRun`` не трогает индекс и не вызывает эмбеддер."""

    def _dry(self, conn, settings, embedder, snap: dict) -> dict:
        return reconcile(
            settings, snapshot=snap, namespace=NS, conn=conn, embedder=embedder, dry_run=True
        )

    def test_dry_run_skips_index_then_apply_indexes(self, conn, settings):
        embedder = CountingEmbedder(settings.embedding_dim)
        index = entity_index(conn, settings)
        _setup(conn, settings)

        plan = self._dry(conn, settings, embedder, _snapshot("s1", T1, OFFERINGS))
        assert plan["dryRun"] and plan["entities"]["opened"] == 2
        assert "search_index" not in plan
        assert embedder.calls == 0
        assert not index.table_exists() or index.rows(NS) == {}

        result = _reconcile(conn, settings, embedder, _snapshot("s1", T1, OFFERINGS))
        assert result["search_index"]["indexed"] == 2
        calls, before = embedder.calls, index.rows(NS)
        assert calls > 0

        # Закрыть N-14 и сменить текст CH-1 — в предпросмотре индекс прежний.
        chair = {**OFFERINGS[1], "attributes": {"description": "пачка бумаги a4"}}
        plan = self._dry(conn, settings, embedder, _snapshot("s2", T2, [chair]))
        assert plan["entities"]["closed"] >= 1 and "search_index" not in plan
        assert embedder.calls == calls
        assert index.rows(NS) == before

        result = _reconcile(conn, settings, embedder, _snapshot("s2", T2, [chair]))
        assert (result["search_index"]["removed"], result["search_index"]["indexed"]) == (1, 1)
        assert embedder.calls > calls
        assert sorted(index.rows(NS)) == [("offering", "CH-1")]
