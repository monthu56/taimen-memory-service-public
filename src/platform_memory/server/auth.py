"""Auth-гранты движка памяти: реестр ключей с префиксными грантами по namespace (ADR-017).

«Жёсткие роли памяти» на data-plane, продукт-нейтрально: каждый API-ключ несёт список
грантов ``{prefix, read, write}``; запрос авторизуется, если ВСЕ его namespaces покрыты
подходящим грантом. Identity (пользователи, JWT, продуктовый RBAC) остаётся на стороне
хоста — движок различает только вызывающие сервисы.

Конфигурация — ``CB_API_KEYS`` (JSON-массив)::

    [{"name": "tp-api", "key": "…",
      "grants": [{"prefix": "tp:", "read": true, "write": true},
                 {"prefix": "shared", "read": true, "write": false}]},
     {"name": "nexus", "key": "…", "pii": true,
      "grants": [{"prefix": "", "read": true, "write": true}]}]

Пустой префикс покрывает все namespaces. Необязательное ``"service": true`` даёт ключу
service scope — регистрацию доменных пакетов видов (MEM-ADR-020). Необязательное
``"pii": true`` даёт ключу полный допуск к ПДн (как у ``CB_SERVER_API_KEYS_PII``, ADR-002); без него ключ при
включённой ``CB_PII_PROTECTION`` получает маскированную выдачу. Legacy-ключи
``CB_SERVER_API_KEY`` / ``CB_SERVER_API_KEYS_PII`` остаются full-access по namespaces
(обратная совместимость: гранты — опциональное сужение, а не замена).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass


class ApiKeysConfigError(ValueError):
    """Некорректный JSON/структура CB_API_KEYS."""


@dataclass(frozen=True, slots=True)
class NamespaceGrant:
    """Грант на поддерево namespaces: префикс + флаги чтения/записи.

    ``exact=True`` — грант ровно на один namespace, без поддерева (нужен IAM-токенам:
    ``tenant:<uuid>`` не должен покрывать ``tenant:<uuid>0``, см. ``server/iam.py``).
    Реестр ``CB_API_KEYS`` этот флаг не задаёт — его гранты остаются префиксными.
    """

    prefix: str
    read: bool = True
    write: bool = False
    exact: bool = False

    def matches(self, namespace: str) -> bool:
        """Покрывает ли грант namespace (точно или по префиксу)."""
        return namespace == self.prefix if self.exact else namespace.startswith(self.prefix)


@dataclass(frozen=True, slots=True)
class CallerGrants:
    """Права вызывающего: имя (для логов/аудита), список грантов, допуск к ПДн."""

    name: str
    grants: tuple[NamespaceGrant, ...]
    pii: bool = False
    # Service scope (MEM-ADR-020): операции уровня сервиса, не namespace, —
    # регистрация доменных пакетов видов. Ключ реестра — ``"service": true``,
    # IAM-токен — scope ``memory:service``.
    service: bool = False

    def allows(self, namespace: str, *, write: bool) -> bool:
        """Покрыт ли namespace хотя бы одним грантом с нужным флагом."""
        for grant in self.grants:
            if grant.matches(namespace) and (grant.write if write else grant.read):
                return True
        return False


FULL_ACCESS_GRANT = (NamespaceGrant(prefix="", read=True, write=True),)


def full_access(name: str, *, pii: bool = False) -> CallerGrants:
    """Полный доступ ко всем namespaces (legacy-ключ, локальный режим)."""
    return CallerGrants(name=name, grants=FULL_ACCESS_GRANT, pii=pii)


def parse_api_keys(raw: str) -> dict[str, CallerGrants]:
    """Распарсить CB_API_KEYS в отображение ключ -> права. Пусто -> {}."""
    if not raw.strip():
        return {}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiKeysConfigError(f"CB_API_KEYS: невалидный JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise ApiKeysConfigError("CB_API_KEYS: ожидается JSON-массив записей")
    out: dict[str, CallerGrants] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("key"):
            raise ApiKeysConfigError(f"CB_API_KEYS[{i}]: ожидается объект с полем 'key'")
        raw_grants = entry.get("grants") or []
        if not isinstance(raw_grants, list) or not raw_grants:
            raise ApiKeysConfigError(f"CB_API_KEYS[{i}]: нужен непустой список 'grants'")
        grants = []
        for j, g in enumerate(raw_grants):
            if not isinstance(g, dict) or "prefix" not in g:
                raise ApiKeysConfigError(f"CB_API_KEYS[{i}].grants[{j}]: нужно поле 'prefix'")
            grants.append(
                NamespaceGrant(
                    prefix=str(g["prefix"]),
                    read=bool(g.get("read", True)),
                    write=bool(g.get("write", False)),
                )
            )
        key = str(entry["key"])
        out[key] = CallerGrants(
            name=str(entry.get("name") or f"key-{i}"),
            grants=tuple(grants),
            pii=bool(entry.get("pii", False)),
            service=bool(entry.get("service", False)),
        )
    return out


def match_key(token: str, keys: dict[str, CallerGrants]) -> CallerGrants | None:
    """Найти права по токену; сравнение каждого ключа — в постоянном времени."""
    matched: CallerGrants | None = None
    for key, grants in keys.items():
        # Без раннего выхода: время не зависит от позиции совпавшего ключа.
        if hmac.compare_digest(token, key):
            matched = grants
    return matched


def check_grants(
    grants: CallerGrants | None, namespaces: Iterable[str], *, write: bool
) -> str | None:
    """Вернуть первый непокрытый namespace или None, если всё разрешено.

    ``grants=None`` — гранты не настроены (legacy-ключ, локальный режим): всё разрешено.
    """
    if grants is None:
        return None
    for ns in namespaces:
        if not grants.allows(ns, write=write):
            return ns
    return None
