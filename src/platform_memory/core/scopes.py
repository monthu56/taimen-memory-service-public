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


def node_scopes(node: dict[str, Any]) -> list[str]:
    """Scopes узла графа — ``props.scopes`` (нет или не список — пусто)."""
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    raw = props.get("scopes")
    return [str(s) for s in raw] if isinstance(raw, list | tuple) else []


def trace_node_scopes(node: dict[str, Any]) -> list[str]:
    """Scopes узла трейса: ``props.scopes`` и у события аудита ещё ``props.payload.scopes``.

    Устойчиво к типам: не-список (в том числе строка) и не-dict payload — пусто, как в
    :func:`node_scopes`; события, записанные до валидации ``payload.scopes``, могут
    нести что угодно (MEM-ADR-019).
    """
    scopes = node_scopes(node)
    if node.get("type") == "audit_event":
        props = node.get("props") if isinstance(node.get("props"), dict) else {}
        payload = props.get("payload")
        if isinstance(payload, dict):
            scopes += node_scopes({"props": payload})
    return scopes


def audit_scopes(snapshot: dict[str, Any] | Iterable[str]) -> dict[str, list[str]]:
    """``{"scopes": …}`` удалённого/выданного объекта для payload аудита (пусто — без scopes).

    Принимает снимок узла или готовый список scopes (наблюдение). Событие несёт
    сведения об объекте; по этим scopes трейс скрывает его от вызывающего, которому
    объект не виден (MEM-ADR-019).
    """
    scopes = node_scopes(snapshot) if isinstance(snapshot, dict) else [str(s) for s in snapshot]
    return {"scopes": scopes} if scopes else {}


# --- видимость на записи (MEM-ADR-019, TASK-001052) ----------------------------


class ForeignObjectError(PermissionError):
    """Запись по ключу существующего объекта вне видимости пишущего.

    Сообщение нейтрально — ни scopes, ни содержимого объекта: HTTP отвечает на это
    так же, как на отсутствие права записи (``403``), существование не раскрывается.
    """

    def __init__(self, natural_key: str):
        super().__init__(f"Нет прав на запись: {natural_key}")
        self.natural_key = natural_key


def check_write_scopes(scopes: Iterable[Any] | None, allowed: Iterable[str] | None) -> None:
    """Scopes видимости записи — только из видимости пишущего (None — без ограничения).

    Иначе пишущий создал бы объект со scope, которого сам не имеет: подсунул бы его
    чужому воркспейсу, а redrive наблюдения перезаписал бы от его имени объекты
    этого воркспейса. Отказ — :class:`ForeignObjectError` с чужим scope: его
    передал сам пишущий, объектов он не раскрывает.
    """
    if allowed is None:
        return
    permitted = {str(a) for a in allowed}
    for scope in scopes or ():
        if str(scope).startswith(VISIBILITY_PREFIXES) and str(scope) not in permitted:
            raise ForeignObjectError(str(scope))


def meta_scopes(meta: dict[str, Any] | None) -> list[str]:
    """Scopes из ``meta`` чанка: список или одна строка (нет — пусто)."""
    raw = (meta or {}).get("scopes")
    if isinstance(raw, str):
        return [raw]
    return [str(s) for s in raw] if isinstance(raw, list | tuple) else []


def merge_scopes(existing: Iterable[str] | None, incoming: Iterable[str]) -> list[str]:
    """Scopes объекта после перезаписи: объединение, видимость не сужается и не стирается.

    ``existing`` None — объекта не было: scopes записи как есть. Иначе:

    * scopes видимости (``workspace:``/``principal:``) объединяются — перезапись не
      стирает чужие; но объект уровня namespace (без scope видимости) им и остаётся:
      scope видимости записи его не приватизирует, иначе писатель скрыл бы общий
      объект от остальных;
    * scopes релевантности (``task:``/``run:``/…) — последний писатель, если он их
      передал, иначе прежние (не накапливаются от записи к записи).
    """
    new = [str(s) for s in incoming]
    if existing is None:
        return list(dict.fromkeys(new))
    old = [str(s) for s in existing]
    old_vis = [s for s in old if s.startswith(VISIBILITY_PREFIXES)]
    new_vis = [s for s in new if s.startswith(VISIBILITY_PREFIXES)]
    visibility = old_vis + new_vis if old_vis else []
    new_rel = [s for s in new if not s.startswith(VISIBILITY_PREFIXES)]
    relevance = new_rel or [s for s in old if not s.startswith(VISIBILITY_PREFIXES)]
    return list(dict.fromkeys(visibility + relevance))


def meta_with_scopes(meta: dict[str, Any] | None, *scope_lists: Iterable[str]) -> dict[str, Any]:
    """``meta`` чанка с ``scopes`` — объединением своих и переданных (пусто — как было).

    Чанк объекта со scopes несёт их в ``meta.scopes``: по ним фильтрует индекс, и
    перезапись чанка не должна их стереть (MEM-ADR-019).
    """
    out = dict(meta or {})
    own = out.get("scopes")
    merged = [str(s) for s in own] if isinstance(own, list | tuple) else []
    for scopes in scope_lists:
        merged.extend(str(s) for s in scopes)
    if merged:
        out["scopes"] = list(dict.fromkeys(merged))
    return out
