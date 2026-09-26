"""Token budgeting Context Compiler'а (ADR-016 §21): жадный отбор под лимит.

Оценка размера — конфигурируемый ``chars_per_token`` (CB_CONTEXT_CHARS_PER_TOKEN,
дефолт 4.0): идеальный токенизатор всех моделей не нужен, нужен консервативный
детерминированный предел. Секция ``current`` (ephemeral-состояние вызывающего)
бюджетируется первой — это authoritative-контекст текущей задачи.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from platform_memory.context.models import (
    SECTION_CURRENT,
    SECTION_ORDER,
    ContextItem,
    ContextSection,
)


@dataclass(slots=True)
class TokenEstimator:
    """Оценка числа токенов текста по числу символов."""

    chars_per_token: float = 4.0

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text) / max(self.chars_per_token, 1.0)))


def apply_budget(
    sections: list[ContextSection],
    *,
    max_tokens: int,
    estimator: TokenEstimator,
) -> tuple[list[ContextSection], dict[str, object], int]:
    """Отобрать элементы под бюджет; вернуть (секции, отчёт, итоговая оценка).

    Правила (детерминированные):
    1. каждому элементу проставляется ``token_estimate``;
    2. секция ``current`` включается первой (обрезается только при переполнении
       её одной);
    3. остальные элементы отбираются жадно по убыванию score (при равенстве —
       стабильный порядок секций/элементов), пока влезают в остаток;
    4. не влезшие элементы попадают в отчёт ``dropped`` — вызывающий видит, что
       именно не вошло и почему (§21: отдавать информацию о dropped).
    """
    for section in sections:
        for item in section.items:
            item.token_estimate = estimator.estimate(item.text)

    remaining = max_tokens
    dropped: list[dict[str, object]] = []
    truncated = False

    # 1) current — первым, с обрезкой при переполнении.
    for section in sections:
        if section.kind != SECTION_CURRENT:
            continue
        kept: list[ContextItem] = []
        for item in section.items:
            if item.token_estimate <= remaining:
                kept.append(item)
                remaining -= item.token_estimate
            elif remaining > 0:
                keep_chars = int(remaining * estimator.chars_per_token)
                item.text = item.text[:keep_chars]
                item.token_estimate = estimator.estimate(item.text)
                item.signals["truncated"] = True
                kept.append(item)
                remaining = 0
                truncated = True
            else:
                dropped.append({"id": item.id, "kind": item.kind, "reason": "budget"})
        section.items = kept

    # 2) остальные — жадно по score, стабильный порядок при равенстве.
    order = {kind: i for i, kind in enumerate(SECTION_ORDER)}
    candidates: list[tuple[float, int, int, ContextSection, ContextItem]] = []
    for section in sections:
        if section.kind == SECTION_CURRENT:
            continue
        for pos, item in enumerate(section.items):
            candidates.append((-item.score, order.get(section.kind, 99), pos, section, item))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    keep_ids: set[int] = set()
    for neg_score, _sec, _pos, _section, item in candidates:
        if item.token_estimate <= remaining:
            keep_ids.add(id(item))
            remaining -= item.token_estimate
        else:
            dropped.append(
                {"id": item.id, "kind": item.kind, "reason": "budget", "score": -neg_score}
            )
    for section in sections:
        if section.kind == SECTION_CURRENT:
            continue
        section.items = [i for i in section.items if id(i) in keep_ids]

    total = sum(i.token_estimate for s in sections for i in s.items)
    report: dict[str, object] = {
        "max_tokens": max_tokens,
        "dropped": dropped,
        "truncated": truncated,
    }
    return sections, report, total
