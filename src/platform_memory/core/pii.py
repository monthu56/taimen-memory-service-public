"""Детекция и маскирование персональных данных (152-ФЗ) в текстовом контенте.

Регекс-детектор РФ-паттернов: телефон, email, паспорт РФ, СНИЛС, ИНН (только с
текстовым маркером «ИНН» — голые 10/12-значные числа слишком шумны), банковская
карта (16 цифр + контрольная сумма Луна против ложных срабатываний на кодах).

Сознательные ограничения (см. ADR-002): ФИО и даты рождения регексами не
детектируются — слишком шумно; они покрываются явной меткой ``pii`` при записи
(`POST /api/brain/retain`). Маска ``[ПДн:<категория>]`` сама паттернами не
матчится — маскирование идемпотентно.

Используется двумя барьерами: маркировка узлов при записи (учёт/инвентаризация)
и маскирование исходящего текста при чтении токеном без допуска.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Порядок применения важен: более специфичные/длинные паттерны раньше,
# чтобы, например, карта (16 цифр) не была частично съедена паспортом (10 цифр).
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 16 цифр группами по 4 (пробел/дефис/слитно) — плюс Лун-фильтр ниже
    ("card", re.compile(r"(?<![\d-])(?:\d{4}[ -]?){3}\d{4}(?![\d-])")),
    # +7/8 форматы: +7 999 123-45-67, 8 (495) 123 45 67, +79991234567
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+7|8)[ (-]*\d{3}[ )-]*\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)"),
    ),
    # СНИЛС: 123-456-789 00 (последняя пара через пробел или дефис)
    ("snils", re.compile(r"(?<![\d-])\d{3}-\d{3}-\d{3}[ -]\d{2}(?![\d-])")),
    # Паспорт РФ: серия 2+2 цифры, пробел, номер 6 цифр («45 06 123456»)
    ("passport_rf", re.compile(r"(?<!\d)\d{2} ?\d{2} \d{6}(?!\d)")),
    # ИНН: только с текстовым маркером — голые числа дают слишком много ложных
    ("inn", re.compile(r"(?<=\bИНН)\W{0,3}\d{10}(?:\d{2})?(?!\d)")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.UNICODE)),
]


def _luhn_ok(digits: str) -> bool:
    """Контрольная сумма Луна: отсекает случайные 16-значные коды от номеров карт."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _match_valid(category: str, match: re.Match[str]) -> bool:
    """Дополнительная валидация совпадения (сейчас — только Лун для карт)."""
    if category == "card":
        return _luhn_ok(re.sub(r"\D", "", match.group()))
    return True


def _scan(text: str, on_match: Callable[[str, re.Match[str]], str | None]) -> str:
    """Единый проход по паттернам; on_match возвращает замену или None (оставить)."""
    for category, pattern in PII_PATTERNS:

        def _repl(m: re.Match[str], _cat: str = category) -> str:
            if not _match_valid(_cat, m):
                return m.group()
            return on_match(_cat, m) or m.group()

        text = pattern.sub(_repl, text)
    return text


def detect_pii(text: str) -> dict[str, int]:
    """Найти ПДн в тексте: {категория: число вхождений}; пусто — ПДн не обнаружены."""
    found: dict[str, int] = {}

    def _count(category: str, _m: re.Match[str]) -> None:
        found[category] = found.get(category, 0) + 1
        return None  # текст не меняем

    _scan(text, _count)
    return found


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """Заменить ПДн масками ``[ПДн:<категория>]``; вернуть (текст, счётчики).

    Идемпотентно: маска не матчится ни одним паттерном.
    """
    found: dict[str, int] = {}

    def _mask(category: str, _m: re.Match[str]) -> str:
        found[category] = found.get(category, 0) + 1
        return f"[ПДн:{category}]"

    return _scan(text, _mask), found
