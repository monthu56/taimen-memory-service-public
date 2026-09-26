"""Тесты реранкера (ADR-005): переоценка кандидатов, порядок, fallback, парсинг LLM.

Без сети/БД: LLM-клиент и build_reranker подменяются фейками. Ключевые контракты:
реранк пересортирует по rerank_score и проставляет его; при недоступности/ошибке —
прозрачный fallback на исходный порядок RRF (реранк не роняет поиск); флаг off —
поведение прежнее.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sys

import platform_memory.retrieval.query  # noqa: F401 — регистрирует подмодуль в sys.modules
from platform_memory.core.config import Settings
from platform_memory.index.store import SearchHit
from platform_memory.retrieval.rerank import LLMReranker, _extract_json_map, build_reranker

# ВАЖНО: `from platform_memory.retrieval import query` дал бы ФУНКЦИЮ query (реэкспорт
# из __init__), а не модуль. Берём модуль из sys.modules — там всегда подмодуль.
query_mod = sys.modules["platform_memory.retrieval.query"]

pytestmark = pytest.mark.unit


def _hit(cid: int, key: str, text: str = "", score: float = 0.03) -> SearchHit:
    return SearchHit(
        chunk_id=cid, node_key=key, source_path=key, title=key, heading="", text=text, score=score
    )


def _fake_client(content: str):
    """OpenAI-совместимый клиент-заглушка: возвращает заданный content без сети."""
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                )
            )
        )
    )


# ---------------- _apply_rerank: порядок и fallback ----------------


def test_apply_rerank_reorders_and_sets_score(monkeypatch):
    hits = [_hit(1, "a"), _hit(2, "b"), _hit(3, "c")]
    fake = SimpleNamespace(score=lambda q, cands: [0.2, 0.9, 0.5])
    monkeypatch.setattr(query_mod, "build_reranker", lambda s: fake)
    out = query_mod._apply_rerank(Settings(), "q", hits)
    assert [h.node_key for h in out] == ["b", "c", "a"]  # пересортировано по rerank_score
    assert out[0].rerank_score == 0.9


def test_apply_rerank_fallback_when_scores_none(monkeypatch):
    hits = [_hit(1, "a"), _hit(2, "b")]
    monkeypatch.setattr(
        query_mod, "build_reranker", lambda s: SimpleNamespace(score=lambda q, c: None)
    )
    out = query_mod._apply_rerank(Settings(), "q", hits)
    assert [h.node_key for h in out] == ["a", "b"]  # порядок RRF сохранён
    assert out[0].rerank_score is None


def test_apply_rerank_fallback_on_length_mismatch(monkeypatch):
    hits = [_hit(1, "a"), _hit(2, "b")]
    monkeypatch.setattr(
        query_mod, "build_reranker", lambda s: SimpleNamespace(score=lambda q, c: [0.9])
    )
    out = query_mod._apply_rerank(Settings(), "q", hits)
    assert [h.node_key for h in out] == ["a", "b"]  # неполный ответ — не доверяем


def test_apply_rerank_no_reranker_available(monkeypatch):
    hits = [_hit(1, "a")]
    monkeypatch.setattr(query_mod, "build_reranker", lambda s: None)
    assert query_mod._apply_rerank(Settings(), "q", hits) == hits


# ---------------- build_reranker: доступность ----------------


def test_build_reranker_none_without_llm():
    # echo или пустой ключ → реранк невозможен → None (движок уйдёт в fallback RRF)
    assert build_reranker(Settings(rerank_provider="llm", llm_provider="echo")) is None
    assert (
        build_reranker(Settings(rerank_provider="llm", llm_provider="openai", llm_api_key=""))
        is None
    )
    assert build_reranker(Settings(rerank_provider="unknown")) is None


# ---------------- LLMReranker: парсинг и нормализация ----------------


def test_extract_json_map():
    assert _extract_json_map('{"0": 90, "1": 10}') == {"0": 90, "1": 10}
    assert _extract_json_map('scores: {"0": 5, "1": 7} done') == {"0": 5, "1": 7}
    assert _extract_json_map("no json here") == {}


def test_llm_reranker_parses_and_normalizes():
    r = LLMReranker.__new__(LLMReranker)  # минуем __init__ (без сети/ключа)
    r.model, r.max_chars = "m", 900
    r._client = _fake_client('{"0": 80, "1": 20}')
    assert r.score("q", ["c0", "c1"]) == [0.8, 0.2]


def test_llm_reranker_bad_json_returns_none():
    r = LLMReranker.__new__(LLMReranker)
    r.model, r.max_chars = "m", 900
    r._client = _fake_client("garbage, no json")
    assert r.score("q", ["c0"]) is None


def test_llm_reranker_missing_id_returns_none():
    r = LLMReranker.__new__(LLMReranker)
    r.model, r.max_chars = "m", 900
    r._client = _fake_client('{"0": 80}')  # не покрыт id=1
    assert r.score("q", ["c0", "c1"]) is None


def test_rerank_disabled_by_default_in_settings():
    assert Settings.model_fields["rerank_enabled"].default is False
