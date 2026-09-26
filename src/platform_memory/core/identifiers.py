"""Извлечение точных идентификаторов из запроса для lexical retrieval (ADR-016).

Векторный поиск и стемминговый FTS теряют UUID, SHA коммитов, коды ошибок,
имена файлов/классов и версии. Здесь — детерминированное выделение таких
токенов из свободного текста запроса; найденные токены ищутся точным
substring/trigram-совпадением.
"""

from __future__ import annotations

import re

# Токен-кандидат: слово с цифрой/подчёркиванием/точкой/дефисом/слэшем внутри,
# CamelCase-идентификатор, UUID, hex-SHA, версия, путь к файлу.
_CANDIDATE = re.compile(
    r"""
    [0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}  # UUID
    | \b[0-9a-f]{7,40}\b                          # hex (short/full SHA)
    | \b[\w.-]+/[\w./-]+\b                        # путь со слэшем
    | \b\d+\.\d+\.\d+[\w.-]*\b                    # версия (x.y.z и длиннее)
    | \b[A-Za-z_][\w.-]*[0-9_][\w.-]*\b           # слово с цифрой/underscore (ADR-016, T-001)
    | \b[A-Za-z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b    # CamelCase
    | \b[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+\b     # dotted.name
    """,
    re.VERBOSE,
)

# Чисто числовые и слишком короткие токены не считаем идентификаторами.
_REJECT = re.compile(r"^\d{1,4}$")

MAX_IDENTIFIERS = 8


def extract_identifiers(text: str, *, limit: int = MAX_IDENTIFIERS) -> list[str]:
    """Выделить из текста токены-идентификаторы (дедуп, порядок появления)."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _CANDIDATE.finditer(text or ""):
        token = match.group(0)
        if len(token) < 3 or _REJECT.match(token):
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= limit:
            break
    return out
