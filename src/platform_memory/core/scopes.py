"""Scope — generic-фильтр видимости ВНУТРИ namespace (ADR-016).

Namespace остаётся жёсткой границей хранения/изоляции KB (ADR-003/054); scope —
пара ``{type, id}``, канонически сериализуемая в строку ``"{type}:{id}"``. Движок
не интерпретирует типы scope (project/task/user/… задаёт потребитель) и не решает
оргправа: caller передаёт список разрешённых scopes, движок фильтрует по нему.

Семантика чтения: запрос без scopes видит всё в namespace; запрос со scopes видит
элементы с непустым пересечением scopes ПЛЮС элементы вовсе без scopes (общая
память KB). Семантика записи: элемент несёт список scopes источника (observation).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# type: строчные буквы/цифры/._- ; id: без пробелов и двоеточий не ограничиваем,
# кроме управляющих символов. Полная строка ≤ 256 символов.
SCOPE_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SCOPE_ID_RE = re.compile(r"^[^\s\x00-\x1f]{1,190}$")
MAX_SCOPE_LEN = 256

# Максимум scopes в одном read-запросе (защита фильтра, как MAX_READ_NAMESPACES).
MAX_READ_SCOPES = 10
# Максимум scopes на одном observation/элементе.
MAX_ITEM_SCOPES = 20


def scope_key(scope_type: str, scope_id: str) -> str:
    """Каноническая строка scope: ``"{type}:{id}"`` (валидирует обе части)."""
    if not isinstance(scope_type, str) or not SCOPE_TYPE_RE.match(scope_type):
        raise ValueError(
            f"Некорректный scope type {scope_type!r}: ожидается [a-z0-9][a-z0-9._-]{{0,63}}"
        )
    if not isinstance(scope_id, str) or not SCOPE_ID_RE.match(scope_id):
        raise ValueError(f"Некорректный scope id {scope_id!r}: непустая строка без пробелов")
    key = f"{scope_type}:{scope_id}"
    if len(key) > MAX_SCOPE_LEN:
        raise ValueError(f"Scope длиннее {MAX_SCOPE_LEN} символов: {key[:64]}…")
    return key


def parse_scope(value: Any) -> str:
    """Принять scope в любом входном виде и вернуть каноническую строку.

    Поддерживаются: строка ``"type:id"``, dict ``{"type": …, "id": …}``.
    """
    if isinstance(value, str):
        stype, sep, sid = value.partition(":")
        if not sep:
            raise ValueError(f"Scope-строка без ':': {value!r} (ожидается 'type:id')")
        return scope_key(stype, sid)
    if isinstance(value, dict):
        return scope_key(str(value.get("type", "")), str(value.get("id", "")))
    raise ValueError(f"Scope должен быть строкой 'type:id' или dict {{type,id}}: {value!r}")


def resolve_scopes(scopes: Iterable[Any] | None, *, limit: int = MAX_READ_SCOPES) -> list[str]:
    """Канонизировать список scopes: валидация + дедуп с сохранением порядка.

    Пусто/None -> [] (без фильтра по scope). Превышение лимита -> ValueError.
    """
    items = [s for s in (scopes or []) if s]
    if not items:
        return []
    if len(items) > limit:
        raise ValueError(f"Слишком много scopes в запросе: {len(items)} > {limit}")
    return list(dict.fromkeys(parse_scope(s) for s in items))


def scope_dicts(keys: Iterable[str]) -> list[dict[str, str]]:
    """Обратное представление для ответов API: ["type:id"] -> [{"type","id"}]."""
    out: list[dict[str, str]] = []
    for key in keys:
        stype, _, sid = str(key).partition(":")
        out.append({"type": stype, "id": sid})
    return out


# --- видимость по principal (TAI-ADR-0031 §4, MEM-ADR-019) --------------------
#
# Namespace — жёсткая граница; внутри него элемент с scope воркспейса или principal
# виден только тому, чьи разрешённые scopes его пересекают («приватно по умолчанию»).
# Элемент без scopes и элемент только с scopes релевантности (task/run/project)
# считается уровнем namespace: его видимость уже решена правом читать namespace.

VISIBILITY_PREFIXES = ("workspace:", "principal:")
MAX_ALLOWED_SCOPES = 500


def has_visibility_scope(scopes: Iterable[str]) -> bool:
    return any(str(s).startswith(VISIBILITY_PREFIXES) for s in scopes)


def is_visible(item_scopes: Iterable[str] | None, allowed: Iterable[str] | None) -> bool:
    """Виден ли элемент вызывающему с разрешёнными scopes ``allowed`` (None — без ограничения)."""
    if allowed is None:
        return True
    items = [str(s) for s in (item_scopes or [])]
    if not items or not has_visibility_scope(items):
        return True
    return bool(set(items) & {str(a) for a in allowed})


def observation_visibility_sql(column: str = "scopes") -> str:
    """SQL-предикат видимости для jsonb-массива scopes (один параметр — список allowed)."""
    return (
        f"({column} = '[]'::jsonb OR {column} ?| %s OR NOT EXISTS ("
        f"SELECT 1 FROM jsonb_array_elements_text({column}) v "
        f"WHERE v LIKE 'workspace:%%' OR v LIKE 'principal:%%'))"
    )


def chunk_visibility_sql() -> str:
    """То же для чанков: scopes лежат в meta->'scopes' и могут отсутствовать вовсе."""
    return "(NOT meta ? 'scopes' OR " + observation_visibility_sql("(meta->'scopes')")[1:]
