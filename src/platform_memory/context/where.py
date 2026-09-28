"""Фильтры ``where`` типизированного обхода (MEM-ADR-020, амендмент 2026-09-28, п.3).

Условие — ``{attr, op, value}`` на атрибут сущности в версии, действующей на ``as_of``;
условия списка соединяются по «И». Операторы:

- ``eq`` — скаляр (строка, число, bool): числа сравниваются численно, остальное — точно;
- ``in`` — список скаляров (1..100): совпадение с любым;
- ``prefix`` — строка, по сегментам через точку: ``62.01`` совпадает с ``62.01`` и
  ``62.01.11``, но не с ``62.011``;
- ``lte``/``gte`` — число или дата/время ISO 8601, включительно; сравниваются только
  значения одного класса (дата без времени — полночь UTC, без зоны — UTC);
- ``exists`` — ``value: bool`` (по умолчанию true): атрибут есть, не null и не пустой
  список.

Атрибут-список выполняет условие (кроме ``exists``), если его выполняет хоть один
элемент; отсутствующий атрибут не выполняет ничего, кроме ``exists: false``.
Некорректное условие — ``ValueError`` (HTTP ``400``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

ATTR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
OPS = ("eq", "in", "prefix", "lte", "gte", "exists")
MAX_WHERE_CLAUSES = 20
MAX_IN_VALUES = 100

_MISSING = object()


@dataclass(frozen=True, slots=True)
class WhereClause:
    attr: str
    op: str
    # eq — скаляр; in — кортеж скаляров; prefix — строка; lte/gte — число | datetime;
    # exists — bool.
    value: Any

    def to_dict(self) -> dict[str, Any]:
        """Условие для трейса: как прислано, даты — строкой ISO."""
        value = self.value
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, tuple):
            value = list(value)
        return {"attr": self.attr, "op": self.op, "value": value}


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _is_scalar(value: Any) -> bool:
    return isinstance(value, str | bool) or _is_number(value)


def parse_date(value: Any) -> datetime | None:
    """Дата/время ISO 8601 (aware, UTC для значений без зоны) либо None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # Дата без времени — полночь.
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_one(raw: Any, path: str) -> WhereClause:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: нужен объект {{attr, op, value}}")
    attr = raw.get("attr")
    if not isinstance(attr, str) or not ATTR_NAME_RE.match(attr):
        raise ValueError(f"{path}.attr: имя атрибута [A-Za-z_][A-Za-z0-9_]{{0,63}}")
    op = raw.get("op")
    if op not in OPS:
        raise ValueError(f"{path}.op: одно из {list(OPS)}")
    value = raw.get("value")
    if op == "eq":
        if not _is_scalar(value):
            raise ValueError(f"{path}.value: eq ждёт строку, число или bool")
    elif op == "in":
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
            raise ValueError(f"{path}.value: in ждёт список из 1..{MAX_IN_VALUES} значений")
        if not all(_is_scalar(v) for v in value):
            raise ValueError(f"{path}.value: in ждёт строки, числа или bool")
        value = tuple(value)
    elif op == "prefix":
        if (
            not isinstance(value, str)
            or not value
            or any(not segment for segment in value.split("."))
        ):
            raise ValueError(
                f"{path}.value: prefix ждёт непустую строку сегментов через точку "
                "(без точки в начале и в конце)"
            )
    elif op in ("lte", "gte"):
        if not _is_number(value):
            parsed = parse_date(value)
            if parsed is None:
                raise ValueError(f"{path}.value: {op} ждёт число или дату ISO 8601")
            value = parsed
    else:  # exists
        if value is None:
            value = True
        if not isinstance(value, bool):
            raise ValueError(f"{path}.value: exists ждёт true или false")
    return WhereClause(attr=attr, op=op, value=value)


def parse_where(raw: Any, path: str = "where") -> list[WhereClause]:
    """Разобрать и провалидировать список условий (пусто/None — без фильтра)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: нужен список условий {{attr, op, value}}")
    if len(raw) > MAX_WHERE_CLAUSES:
        raise ValueError(f"{path}: не больше {MAX_WHERE_CLAUSES} условий")
    return [_parse_one(item, f"{path}[{i}]") for i, item in enumerate(raw)]


def _eq(actual: Any, expected: Any) -> bool:
    if _is_number(expected):
        return _is_number(actual) and actual == expected
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    return isinstance(actual, str) and actual == expected


def _prefix(actual: Any, prefix: str) -> bool:
    return isinstance(actual, str) and (actual == prefix or actual.startswith(prefix + "."))


def _compare(actual: Any, bound: int | float | datetime, op: str) -> bool:
    if isinstance(bound, datetime):
        value: Any = parse_date(actual)
    else:
        value = actual if _is_number(actual) else None
    if value is None:
        return False  # другой класс или неразбираемое значение
    return value <= bound if op == "lte" else value >= bound


def _scalar_matches(actual: Any, clause: WhereClause) -> bool:
    if clause.op == "eq":
        return _eq(actual, clause.value)
    if clause.op == "in":
        return any(_eq(actual, v) for v in clause.value)
    if clause.op == "prefix":
        return _prefix(actual, clause.value)
    return _compare(actual, clause.value, clause.op)


def clause_matches(attributes: dict[str, Any], clause: WhereClause) -> bool:
    actual = attributes.get(clause.attr, _MISSING)
    if clause.op == "exists":
        present = actual is not _MISSING and actual is not None and actual != []
        return present is clause.value
    if actual is _MISSING or actual is None:
        return False
    if isinstance(actual, list):
        return any(_scalar_matches(item, clause) for item in actual)
    return _scalar_matches(actual, clause)


def matches(attributes: dict[str, Any] | None, clauses: list[WhereClause]) -> bool:
    """Выполнены ли все условия (пустой список — да)."""
    attrs = attributes if isinstance(attributes, dict) else {}
    return all(clause_matches(attrs, clause) for clause in clauses)
