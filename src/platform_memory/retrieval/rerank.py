"""Реранкинг кандидатов перед выдачей top-k (ADR-005).

RRF (`_rrf_merge`) сливает вектор и FTS по позициям, но не переоценивает
релевантность пары (запрос, кандидат) — и его абсолютный score мал по природе
(≈1/60), непригоден как «уверенность». Реранкер переоценивает каждого кандидата
относительно запроса и даёт единый порядок + настоящий rerank_score 0..1.

Провайдер по умолчанию — **LLM-rerank** поверх существующего OpenAI-совместимого
клиента (инвариант 1: без новых инфраструктурных зависимостей). При недоступности
LLM (echo/без ключа), ошибке или неполном ответе модели — прозрачный fallback:
вернуть None, и вызывающий сохраняет исходный порядок (RRF). Реранк никогда не роняет
поиск.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Protocol

from platform_memory.core.config import Settings

# Сколько символов кандидата отдаём модели (реранк по началу фрагмента — дёшево и точно).
# Держим компактным: пул включает и граф-соседей (ADR-005), а короче кандидаты — быстрее LLM.
_MAX_CAND_CHARS = 600

_SYSTEM = (
    "You are a strict search-relevance judge. For each passage, rate how well it answers "
    "the user's query on a 0-100 scale (100 = directly and completely answers the query; "
    "0 = irrelevant). Judge relevance to the query only, not writing quality."
)


class Reranker(Protocol):
    """Реранкер: оценивает релевантность кандидатов запросу (0..1 по позициям)."""

    def score(self, query: str, candidates: Sequence[str]) -> list[float] | None:
        """Вернуть список оценок 0..1 по кандидатам, либо None при недоступности/ошибке."""
        ...


def _extract_json_map(text: str) -> dict:
    """Достать JSON-объект оценок из ответа модели (с запасом на обёртки/мусор)."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    return {}


class LLMReranker:
    """LLM-rerank поверх OpenAI-совместимого gateway (один батч-вызов на запрос)."""

    def __init__(self, base_url: str, api_key: str, model: str, max_chars: int = _MAX_CAND_CHARS):
        """Создать клиент OpenAI; требует непустой api_key."""
        from openai import OpenAI

        if not api_key:
            raise ValueError("Реранкеру нужен CB_LLM_API_KEY (или отключите CB_RERANK).")
        self._client = OpenAI(base_url=base_url or None, api_key=api_key)
        self.model = model
        self.max_chars = max_chars

    def score(self, query: str, candidates: Sequence[str]) -> list[float] | None:
        """Оценить кандидатов одним вызовом; None при ошибке/неполном ответе → fallback."""
        if not candidates:
            return []
        passages = "\n\n".join(
            f"[{i}] {(c or '')[: self.max_chars]}" for i, c in enumerate(candidates)
        )
        user = (
            f"Query: {query}\n\nPassages:\n{passages}\n\n"
            "Return ONLY a JSON object mapping each passage id (as a string) to its integer "
            'score, covering every id, e.g. {"0": 91, "1": 37}. No prose.'
        )
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0,
                timeout=12,  # всплеск латентности gateway не должен вешать поиск -> fallback RRF
            )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception:  # noqa: BLE001 — реранк не должен ронять поиск
            return None
        data = _extract_json_map(raw)
        if not data:
            return None
        scores: list[float] = []
        for i in range(len(candidates)):
            val = data.get(str(i), data.get(i))
            try:
                s = float(val)
            except (TypeError, ValueError):
                return None  # неполный/битый ответ — честный fallback вместо кривого порядка
            scores.append(max(0.0, min(1.0, s / 100.0)))
        return scores


def build_reranker(settings: Settings) -> Reranker | None:
    """Собрать реранкер по настройкам; None — если реранк невозможен (тогда fallback на RRF)."""
    provider = (settings.rerank_provider or "llm").lower()
    if provider == "llm":
        # Реранк невозможен без реального LLM — прозрачно уходим в fallback.
        if (settings.llm_provider or "").lower() == "echo" or not settings.llm_api_key:
            return None
        model = settings.rerank_model or settings.llm_model
        return LLMReranker(settings.llm_base_url, settings.llm_api_key, model)
    return None
