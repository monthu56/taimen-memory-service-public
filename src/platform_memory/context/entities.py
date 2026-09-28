"""Перечень сущностей вида с фильтрами (MEM-ADR-020, амендмент «перечень сущностей»).

Запрос::

    {"kinds": ["license"], "where": [{"attr": "validUntil", "op": "lte",
     "value": "2026-12-31"}], "as_of": "2026-09-01T00:00:00Z", "limit": 100,
     "cursor": null, "namespaces": ["tenant:t1"]}

1. Источник — журнал сверки снимков: по одной версии на ``(kind, key, namespace)``,
   валидной на ``as_of`` (пусто — открытые сейчас); если сущность держат несколько
   источников — самая поздняя. Сущности без журнала (граф наблюдений) в перечень не
   входят.
2. Видимость scopes (MEM-ADR-019) — в SQL, до лимита; ``allowed_scopes`` задаёт
   HTTP-слой, не клиент. Неизвестный вид — просто пустой ответ.
3. ``where`` (``context/where.py``) проверяет атрибуты версии.
4. Порядок — ``(kind, key, namespace)``, стабильный; ``nextCursor`` — непрозрачная
   позиция после последней просмотренной строки. Курсор keyset: страницы не
   повторяются и не пропускают сущности, существовавшие на протяжении обхода.
   За запрос просматривается не больше ``MAX_SCAN`` версий: если фильтр редкий,
   страница может быть короче ``limit`` при непустом ``nextCursor`` — конец перечня
   только ``nextCursor: null``.

LLM и эмбеддер не участвуют.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg

from platform_memory.context.where import WhereClause, parse_where
from platform_memory.context.where import matches as where_matches
from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.llm_safety import neutralize_prompt_injection
from platform_memory.core.namespaces import resolve_namespaces
from platform_memory.core.scopes import MAX_ALLOWED_SCOPES, resolve_scopes
from platform_memory.domain.reconcile import SnapshotLedger
from platform_memory.graph.facts import normalize_ts

MAX_KINDS = 20
MAX_LIMIT = 500
DEFAULT_LIMIT = 100
# Версий, просматриваемых за запрос (ограничение работы при редком where).
MAX_SCAN = 5000
# Имя вида — как в пакетах (KindSpecIn.kind); регистр не приводится.
KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_CURSOR_VERSION = 1


@dataclass(slots=True)
class EntityQueryRequest:
    kinds: list[str] = field(default_factory=list)
    where: list[WhereClause] = field(default_factory=list)
    as_of: str = ""
    limit: int = DEFAULT_LIMIT
    after: tuple[str, str, str] | None = None
    namespaces: list[str] = field(default_factory=list)
    # Разрешённые scopes видимости (MEM-ADR-019) — задаёт HTTP-слой, не клиент.
    allowed_scopes: list[str] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EntityQueryRequest:
        """Собрать и провалидировать запрос из JSON (camelCase поддержан)."""
        kinds = payload.get("kinds")
        if not isinstance(kinds, list) or not 1 <= len(kinds) <= MAX_KINDS:
            raise ValueError(f"kinds: нужен список из 1..{MAX_KINDS} видов")
        for i, kind in enumerate(kinds):
            if not isinstance(kind, str) or not KIND_RE.match(kind):
                raise ValueError(f"kinds[{i}]: имя вида [A-Za-z][A-Za-z0-9_]{{0,62}}")
        raw_limit = payload.get("limit", DEFAULT_LIMIT)
        if raw_limit is None:
            raw_limit = DEFAULT_LIMIT
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise ValueError(f"limit: целое 1..{MAX_LIMIT}")
        if not 1 <= raw_limit <= MAX_LIMIT:
            raise ValueError(f"limit: 1..{MAX_LIMIT}")
        as_of = str(payload.get("as_of", payload.get("asOf", "")) or "")
        allowed = payload.get("allowed_scopes", payload.get("allowedScopes"))
        return cls(
            kinds=list(dict.fromkeys(kinds)),
            where=parse_where(payload.get("where")),
            as_of=(normalize_ts(as_of) or "") if as_of else "",
            limit=raw_limit,
            after=decode_cursor(payload.get("cursor")),
            namespaces=list(payload.get("namespaces") or []),
            allowed_scopes=[str(s) for s in allowed] if allowed is not None else None,
        )


def encode_cursor(position: tuple[str, str, str]) -> str:
    """Непрозрачный курсор: base64url JSON позиции ``(kind, key, namespace)``."""
    raw = json.dumps([_CURSOR_VERSION, *position], ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: Any) -> tuple[str, str, str] | None:
    """Позиция из курсора (пусто — с начала); неразбираемый курсор — ``ValueError``."""
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str):
        raise ValueError("cursor: нужна строка nextCursor предыдущей страницы")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("cursor: не курсор перечня") from exc
    if (
        not isinstance(data, list)
        or len(data) != 4
        or data[0] != _CURSOR_VERSION
        or not all(isinstance(x, str) for x in data[1:])
    ):
        raise ValueError("cursor: не курсор перечня")
    return (data[1], data[2], data[3])


def _item(row: dict[str, Any]) -> dict[str, Any]:
    payload = row["payload"]
    attributes = payload.get("attributes")
    return {
        "kind": row["kind"],
        "key": row["key"],
        "namespace": row["namespace"],
        "title": neutralize_prompt_injection(str(payload.get("title") or row["key"])),
        "attributes": dict(attributes) if isinstance(attributes, dict) else {},
        "source": row["source"],
        "scope": row["scope"],
        "snapshot_id": row["snapshot_id"],
        # Цитата — источник версии, действующей на as_of (как в context/typed).
        "source_path": str(
            payload.get("source_path") or f"snapshot:{row['source']}/{row['snapshot_id']}"
        ),
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
    }


def _position(row: dict[str, Any]) -> tuple[str, str, str]:
    return (row["kind"], row["key"], row["namespace"])


def page_entities(
    ledger: SnapshotLedger, req: EntityQueryRequest, allowed: list[str] | None
) -> tuple[list[dict[str, Any]], str | None, int]:
    """Страница перечня: (строки, nextCursor, просмотрено версий)."""
    items: list[dict[str, Any]] = []
    after = req.after
    scanned = 0
    batch = min(MAX_SCAN, max(2 * (req.limit + 1), 100))
    while True:
        want = min(batch, MAX_SCAN - scanned)
        rows = ledger.entity_page(
            req.namespaces,
            req.kinds,
            as_of=req.as_of,
            allowed_scopes=allowed,
            after=after,
            limit=want,
        )
        for row in rows:
            scanned += 1
            after = _position(row)
            payload = row["payload"]
            if not where_matches(payload.get("attributes"), req.where):
                continue
            if len(items) == req.limit:
                # Есть ещё хоть одна подходящая — продолжать с последней отданной.
                return items, encode_cursor(_position(items[-1])), scanned
            items.append(row)
        if len(rows) < want:
            return items, None, scanned  # журнал исчерпан
        if scanned >= MAX_SCAN:
            # Лимит просмотра: продолжить с последней просмотренной версии.
            return items, encode_cursor(after), scanned


def query_entities(
    settings: Settings,
    request: EntityQueryRequest | dict[str, Any],
    *,
    conn: psycopg.Connection | None = None,
) -> dict[str, Any]:
    """Страница сущностей видов с фильтрами (см. docstring модуля)."""
    req = EntityQueryRequest.from_payload(request) if isinstance(request, dict) else request
    req.namespaces = resolve_namespaces(req.namespaces, settings.default_namespace)
    allowed = (
        None
        if req.allowed_scopes is None
        else resolve_scopes(req.allowed_scopes, limit=MAX_ALLOWED_SCOPES)
    )
    own_conn = conn is None
    conn = conn or dbmod.connect(settings, autocommit=True)
    try:
        ledger = SnapshotLedger(conn, settings.snapshots_table)
        if ledger.table_exists():
            rows, next_cursor, scanned = page_entities(ledger, req, allowed)
        else:
            rows, next_cursor, scanned = [], None, 0
    finally:
        if own_conn:
            conn.close()
    return {
        "items": [_item(r) for r in rows],
        "nextCursor": next_cursor,
        "as_of": req.as_of or None,
        "namespaces": req.namespaces,
        "stats": {"scanned": scanned, "returned": len(rows)},
    }


__all__ = [
    "EntityQueryRequest",
    "decode_cursor",
    "encode_cursor",
    "page_entities",
    "query_entities",
]
