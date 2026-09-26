"""Канал resolve (MEM-ADR-020, амендмент): извлечение и нормализация кандидатов,
порядок разрешения и лимиты — чистая логика на фейковом журнале, без БД."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_memory.context.resolve import (
    Candidate,
    entity_text,
    extract_candidates,
    normalize_placeholders,
    resolve_candidates,
    source_path_of,
)
from platform_memory.core.kinds import KindCatalog, parse_pack

pytestmark = pytest.mark.unit

NS = "tenant:t1:ws:w1"
PACK = Path(__file__).parent / "fixtures" / "software_delivery" / "pack.json"


@pytest.fixture(scope="module")
def catalog() -> KindCatalog:
    pack = parse_pack(json.loads(PACK.read_text(encoding="utf-8")))
    return KindCatalog.build([pack])


def _row(key: str, kind: str, *, aliases=(), ns=NS, rid=1, **payload) -> dict:
    return {
        "id": rid,
        "namespace": ns,
        "kind": kind,
        "key": key,
        "source": "git:control-plane",
        "scope": "control-plane",
        "snapshot_id": "control-plane@1",
        "payload": {"title": payload.pop("title", key), "aliases": list(aliases), **payload},
        "valid_from": "2026-09-23T08:45:00Z",
        "valid_to": None,
    }


class FakeLedger:
    """Журнал в памяти с семантикой SnapshotLedger (без scopes/as_of — их проверяет SQL)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[str] = []

    def _ns(self, namespaces):
        return [r for r in self.rows if r["namespace"] in namespaces]

    def entities_by_keys(self, namespaces, keys, **_):
        self.calls.append("keys")
        return [r for r in self._ns(namespaces) if r["key"] in keys]

    def entities_by_aliases(self, namespaces, aliases, **_):
        self.calls.append("aliases")
        return [
            r for r in self._ns(namespaces) if set(aliases) & set(r["payload"].get("aliases", []))
        ]

    def entities_by_suffixes(
        self, namespaces, suffixes, *, per_suffix, per_namespace=False, kinds=None, **_
    ):
        self.calls.append("suffixes")
        kinds = kinds if kinds is not None else [None] * len(suffixes)
        out = []
        for i, suffix in enumerate(suffixes):
            for group in [[ns] for ns in namespaces] if per_namespace else [namespaces]:
                hits = sorted(
                    (
                        r
                        for r in self._ns(group)
                        if r["key"].endswith(suffix) and (kinds[i] is None or r["kind"] in kinds[i])
                    ),
                    key=lambda r: r["key"],
                )
                out += [(i, r) for r in hits[:per_suffix]]
        return out


ROWS = [
    _row(
        "POST /api/v1/goals/{}",
        "endpoint",
        aliases=["/api/v1/goals/{goal_id}"],
        rid=1,
        source_path="control-plane@abc:src/api/goals.py:40",
        attributes={"method": "POST", "path": "/api/v1/goals/{goal_id}"},
    ),
    _row("GET /api/v1/goals/{}", "endpoint", aliases=["/api/v1/goals/{goal_id}"], rid=2),
    _row("goal.created", "event", rid=3),
    _row("CP-0062", "adr", aliases=["ADR-0062", "CP-ADR-0062"], rid=4, title="ADR-0062: Цели"),
    _row("TAI-0062", "adr", aliases=["ADR-0062", "TAI-ADR-0062"], rid=5),
    _row("MEM-020", "adr", aliases=["ADR-020", "MEM-ADR-020"], rid=6),
    _row("control-plane:client.ControlPlaneClient.get_claimability", "client_method", rid=7),
    _row("control-plane:work_rules", "table", rid=8),
    _row("goal.created", "event", ns="tenant:other", rid=9),
]


def _resolve(values, **kw):
    ledger = FakeLedger(kw.pop("rows", ROWS))
    cands = [v if isinstance(v, Candidate) else Candidate(v) for v in values]
    hits, report = resolve_candidates(ledger, cands, kw.pop("namespaces", [NS]), **kw)
    return hits, report, ledger


# --- нормализация и извлечение ---


def test_normalize_placeholders():
    assert normalize_placeholders("POST /api/v1/goals/{goal_id}") == "POST /api/v1/goals/{}"
    assert normalize_placeholders("/a/{x}/b/{y}") == "/a/{}/b/{}"
    assert normalize_placeholders("GET /api/v1/tasks") == "GET /api/v1/tasks"
    assert Candidate("/a/{x}").forms == ["/a/{x}", "/a/{}"]
    assert Candidate("goal.created").forms == ["goal.created"]


def test_extract_candidates_from_query_by_pack_patterns(catalog):
    query = "Правки в POST /api/v1/goals/{goal_id}, событие goal.created, CP-ADR-0062"
    values = [c.value for c in extract_candidates(query, [], [catalog])]
    # Порядок появления; части более точных форм поглощены.
    assert values[:3] == ["POST /api/v1/goals/{goal_id}", "goal.created", "CP-ADR-0062"]
    assert "/api/v1/goals/{goal_id}" not in values
    assert "ADR-0062" not in values


def test_extract_candidates_anchors_first_and_kind_hint(catalog):
    cands = extract_candidates(
        "про MEM-ADR-020",
        ["task:TASK-1", {"type": "adr", "id": "CP-0062"}, {"key": "goal.created"}],
        [catalog],
    )
    assert [(c.value, c.kind, c.origin) for c in cands] == [
        ("task:TASK-1", "", "anchor"),
        ("CP-0062", "adr", "anchor"),
        ("goal.created", "", "anchor"),
        ("MEM-ADR-020", "", "query"),
    ]


def test_extract_candidates_limit(catalog):
    query = " ".join(f"CP-{n:04d}" for n in range(100))
    assert len(extract_candidates(query, [], [catalog], limit=40)) == 40


def test_extract_candidates_without_catalog_uses_generic_extractor():
    values = [c.value for c in extract_candidates("таблица work_rules и M1.3", [], [])]
    assert "work_rules" in values and "M1.3" in values


# --- порядок разрешения ---


def test_exact_natural_key():
    hits, report, _ = _resolve(["goal.created"])
    assert [(h.key, h.method, h.exact) for h in hits] == [("goal.created", "natural_key", True)]
    assert report[0]["resolved"][0]["method"] == "natural_key"


def test_adr_reference_forms():
    # ADR-0035 -> все ряды; CP-ADR-0062 -> CP-0062; MEM-ADR-020 -> MEM-020.
    hits, _, _ = _resolve(["ADR-0062"])
    assert sorted(h.key for h in hits) == ["CP-0062", "TAI-0062"]
    assert {h.method for h in hits} == {"alias"}
    assert [h.key for h in _resolve(["CP-ADR-0062"])[0]] == ["CP-0062"]
    assert [h.key for h in _resolve(["CP-0062"])[0]] == ["CP-0062"]
    assert [h.key for h in _resolve(["MEM-ADR-020"])[0]] == ["MEM-020"]


def test_endpoint_forms():
    # Метод + путь с именованными параметрами -> ключ с {}.
    hits, _, _ = _resolve(["POST /api/v1/goals/{id}"])
    assert [(h.key, h.method) for h in hits] == [("POST /api/v1/goals/{}", "normalized")]
    # Путь с исходными именами параметров — псевдоним: все методы.
    hits, _, _ = _resolve(["/api/v1/goals/{goal_id}"])
    assert sorted(h.key for h in hits) == ["GET /api/v1/goals/{}", "POST /api/v1/goals/{}"]
    assert {h.method for h in hits} == {"alias"}
    # Путь с другими именами параметров — суффикс нормализованной формы: все методы.
    hits, _, _ = _resolve(["/api/v1/goals/{x}"])
    assert sorted(h.key for h in hits) == ["GET /api/v1/goals/{}", "POST /api/v1/goals/{}"]
    assert {(h.method, h.exact) for h in hits} == {("suffix", False)}


def test_suffix_names():
    for value in ("get_claimability", "ControlPlaneClient.get_claimability"):
        hits, _, _ = _resolve([value])
        assert [(h.kind, h.method) for h in hits] == [("client_method", "suffix")]
    hits, _, _ = _resolve(["work_rules"])
    assert [h.key for h in hits] == ["control-plane:work_rules"]
    # Не по границе разделителя — не суффикс; короткий кандидат суффиксом не ищется.
    assert not _resolve(["claimability"])[0]
    assert not _resolve(["les"])[0]


def test_suffix_query_only_for_unresolved():
    _, _, ledger = _resolve(["goal.created", "CP-ADR-0062"])
    assert ledger.calls == ["keys", "aliases"]
    _, _, ledger = _resolve(["goal.created", "get_claimability"])
    assert ledger.calls == ["keys", "aliases", "suffixes"]


def test_kind_hint_filters(catalog):
    assert _resolve([Candidate("goal.created", kind="adr")], catalogs=[catalog])[0] == []
    hits, _, _ = _resolve([Candidate("goal.created", kind="event")], catalogs=[catalog])
    assert [h.key for h in hits] == ["goal.created"]


def test_namespaces_do_not_leak():
    hits, _, _ = _resolve(["goal.created"], namespaces=["tenant:other"])
    assert [(h.namespace, h.key) for h in hits] == [("tenant:other", "goal.created")]
    assert not _resolve(["CP-0062"], namespaces=["tenant:other"])[0]


def test_limits_and_dedup():
    rows = [_row(f"repo:mod.C.m{i:02d}", "client_method", rid=i) for i in range(30)]
    rows += [_row(f"repo:x.C{i:02d}.run_all", "client_method", rid=100 + i) for i in range(30)]
    hits, report, _ = _resolve(["run_all"], rows=rows, per_suffix=10)
    assert len(hits) == 10
    hits, report, _ = _resolve(
        [f"m{i:02d}x" for i in range(3)] + ["run_all", "run_all"],
        rows=rows,
        max_resolved=5,
        per_suffix=10,
    )
    assert len(hits) == 5
    assert report[3].get("truncated") is True
    # Одна сущность — один раз, первым разрешившим её кандидатом.
    hits, _, _ = _resolve(["CP-0062", "CP-ADR-0062"])
    assert [(h.key, h.input) for h in hits] == [("CP-0062", "CP-0062")]


def test_exact_resolved_before_suffix_exhausts_limit():
    """Сценарий ревью TASK-000330: два неоднозначных имени метода в начале текста
    (по 25 методов с каждым суффиксом) не вытесняют точный ADR из лимита."""
    rows = [_row(f"repo:mod.C{i:02d}.status_view", "client_method", rid=i) for i in range(25)]
    rows += [_row(f"repo:mod.C{i:02d}.list_items", "client_method", rid=100 + i) for i in range(25)]
    rows.append(_row("CP-0062", "adr", aliases=["ADR-0062", "CP-ADR-0062"], rid=500))
    hits, report, _ = _resolve(["status_view", "list_items", "CP-ADR-0062"], rows=rows)
    assert len(hits) == 20
    assert report[2] == {
        "input": "CP-ADR-0062",
        "resolved": [{"namespace": NS, "kind": "adr", "key": "CP-0062", "method": "alias"}],
    }
    # Точное — первым в списке, суффиксы делят остаток лимита в порядке текста.
    assert (hits[0].key, hits[0].method) == ("CP-0062", "alias")
    assert [len(e["resolved"]) for e in report] == [10, 9, 1]
    assert report[1].get("truncated") is True and "truncated" not in report[0]


def test_latest_version_wins():
    rows = [
        _row("goal.created", "event", rid=1, title="old"),
        _row("goal.created", "event", rid=7, title="new"),
    ]
    hits, _, _ = _resolve(["goal.created"], rows=rows)
    assert [h.row["payload"]["title"] for h in hits] == ["new"]


def test_entity_text_and_source_path():
    hits, _, _ = _resolve(["POST /api/v1/goals/{goal_id}", "CP-0062", "goal.created"])
    by_key = {h.key: h for h in hits}
    title, text = entity_text(by_key["POST /api/v1/goals/{}"])
    assert title == "POST /api/v1/goals/{}"
    assert text.startswith("POST /api/v1/goals/{} (endpoint)")
    assert "path=/api/v1/goals/{goal_id}" in text
    assert source_path_of(by_key["POST /api/v1/goals/{}"]) == (
        "control-plane@abc:src/api/goals.py:40"
    )
    title, text = entity_text(by_key["CP-0062"])
    assert text.startswith("ADR-0062: Цели (adr CP-0062)")
    # Без цитаты в снимке — ссылка на сам снимок.
    assert source_path_of(by_key["goal.created"]) == "snapshot:git:control-plane/control-plane@1"
