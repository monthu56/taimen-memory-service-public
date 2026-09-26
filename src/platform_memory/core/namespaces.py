"""Namespace — единица изоляции памяти (ADR-054).

Namespace — опаковая строка: движок валидирует формат и фильтрует по точному
совпадению, но НЕ интерпретирует сегменты. Иерархия (``tp:tenant:<uuid>:team:<uuid>``)
существует только для потребителей: деривация на стороне хоста, префиксные
auth-гранты, purge по префиксу. Чтение принимает список namespaces, запись — ровно один.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Строчные буквы/цифры, разделители ._:- ; без пробелов; максимум 200 символов.
NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")

# Namespace существующих данных (Nexus/Company Brain) до мультитенантности.
# Настраивается через CB_DEFAULT_NAMESPACE; запросы без scope работают в нём.
FALLBACK_NAMESPACE = "nexus"

# Общая память, доступная поверх собственного namespace (read-only для продуктов).
SHARED_NAMESPACE = "shared"

# Максимум namespaces в одном read-запросе (защита от деградации фильтра).
MAX_READ_NAMESPACES = 50


def validate_namespace(namespace: str) -> str:
    """Проверить формат namespace; вернуть его же или бросить ValueError."""
    if not isinstance(namespace, str) or not NAMESPACE_RE.match(namespace):
        raise ValueError(
            f"Некорректный namespace {namespace!r}: ожидается строка вида "
            f"[a-z0-9][a-z0-9._:-]{{0,199}}"
        )
    return namespace


def resolve_namespace(namespace: str, default: str = "") -> str:
    """Пустой namespace -> default (или FALLBACK_NAMESPACE); непустой валидируется."""
    resolved = namespace or default or FALLBACK_NAMESPACE
    return validate_namespace(resolved)


def resolve_namespaces(namespaces: Iterable[str] | None, default: str = "") -> list[str]:
    """Список для чтения: валидация + дедуп с сохранением порядка; пусто -> [default]."""
    items = [ns for ns in (namespaces or []) if ns]
    if not items:
        return [resolve_namespace("", default)]
    if len(items) > MAX_READ_NAMESPACES:
        raise ValueError(
            f"Слишком много namespaces в запросе: {len(items)} > {MAX_READ_NAMESPACES}"
        )
    return list(dict.fromkeys(validate_namespace(ns) for ns in items))


# --- namespaces платформы (TAI-ADR-0031 §4): tenant, воркспейс верхнего уровня, principal ---

TENANT_PREFIX = "tenant:"


def tenant_namespace(tenant_id: str) -> str:
    return f"{TENANT_PREFIX}{tenant_id}"


def workspace_namespace(tenant_id: str, workspace_id: str) -> str:
    return f"{TENANT_PREFIX}{tenant_id}:ws:{workspace_id}"


def principal_namespace(tenant_id: str, principal_id: str) -> str:
    return f"{TENANT_PREFIX}{tenant_id}:principal:{principal_id}"


def namespace_from_policy_object(tenant_id: str, object_id: str) -> str | None:
    """Объект ``memory_namespace`` policy-service (``ws-<id>``, ``principal-<id>``,
    ``tenant-<id>``) → имя namespace; неизвестная форма → None."""
    kind, sep, ident = object_id.partition("-")
    if not sep or not ident:
        return None
    if kind == "ws":
        return workspace_namespace(tenant_id, ident)
    if kind == "principal":
        return principal_namespace(tenant_id, ident)
    if kind == "tenant":
        return tenant_namespace(ident)
    return None
