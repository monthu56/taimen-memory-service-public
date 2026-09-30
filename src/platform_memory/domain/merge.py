"""Сведение версий сущности из нескольких источников снимков (MEM-ADR-022).

Одну сущность ``(kind, key)`` могут держать открытой несколько ``(source, scope)``:
журнал сверки хранит версию каждого источника отдельно. Состояние сущности — сведение
этих версий, а не самая поздняя из них:

* атрибут, заданный хоть одним источником, есть в сведении: снимок другого источника,
  где атрибута нет (или он ``null``), его не стирает;
* конфликт — один атрибут задан несколькими источниками — решается по атрибуту, а не
  по узлу: ранг источника по ``sourcePriority`` вида (меньше — сильнее; источник вне
  списка — после всех), затем «последний писавший» — более позднее время, с которого
  источник держит это значение (``since`` версии: ``observedAt`` снимка, который его
  установил; снимок, подтвердивший то же значение, время не сдвигает), затем
  ``(source, scope)`` по порядку строк — детерминированно, от порядка записи между
  источниками не зависит;
* ``title`` — по тому же правилу; заголовок, равный ключу (снимок заголовка не дал),
  уступает любому настоящему;
* ``aliases`` и ``scopes`` — объединение; ``provenance``, ``source_path`` и снимок —
  старшей версии (``primary``: ранг, затем более поздний ``valid_from``); ``sources`` —
  все сведённые версии по старшинству;
* интервал сведения — ``[max(valid_from), min(valid_to))``: пока ни одна из
  сведённых версий не сменилась.

Узел графа и строка индекса поиска видны по своим ``scopes`` всем, кого они
пропускают, поэтому их сведение видимость не расширяет (``merge_for_node``): в узел
сводятся только версии одного класса видимости — с тем же набором scopes. Класс —
самый широкий из открытых: без scopes, затем без scopes видимости (только scopes
релевантности), затем со scopes видимости; внутри уровня — класс старшей версии.
Версии других классов видны только читателям журнала, которые сводят видимые
вызывающему версии.

Модуль чистый (БД не нужна): журнал отдаёт версии, читатели сводят их здесь.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from platform_memory.core.scopes import has_visibility_scope

if TYPE_CHECKING:
    from platform_memory.core.kinds import KindCatalog

# (namespace, kind, source) -> ранг источника: меньше — сильнее.
SourceRank = Callable[[str, str, str], int]
# Ключ payload версии: с какого момента источник держит значения
# ``{"title": ts, "attributes": {имя: ts}}``. В хэш содержимого не входит.
SINCE_KEY = "since"


def no_priority(_ns: str, _kind: str, _source: str) -> int:
    """Без приоритета источников: конфликт решает только ``observedAt``."""
    return 0


def rank_by_catalog(catalog: KindCatalog) -> SourceRank:
    """Ранг источника по ``sourcePriority`` вида в каталоге одного namespace."""
    return lambda _ns, kind, source: catalog.source_rank(kind, source)


def rank_by_catalogs(catalogs: Mapping[str, KindCatalog]) -> SourceRank:
    """Ранг по каталогам нескольких namespaces; namespace без каталога — без приоритета."""

    def _rank(ns: str, kind: str, source: str) -> int:
        catalog = catalogs.get(ns)
        return catalog.source_rank(kind, source) if catalog is not None else 0

    return _rank


def _precedence(rows: Iterable[dict[str, Any]], rank: SourceRank) -> list[dict[str, Any]]:
    """Версии по старшинству: ранг источника, поздний valid_from, (source, scope), поздний id.

    ``id`` различает только версии одного ``(source, scope)``: между источниками
    старшинство от порядка записи не зависит.
    """
    ordered = sorted(rows, key=lambda r: int(r.get("id") or 0), reverse=True)
    ordered.sort(key=lambda r: (str(r.get("source", "")), str(r.get("scope", ""))))
    ordered.sort(key=lambda r: str(r.get("valid_from") or ""), reverse=True)
    ordered.sort(key=lambda r: rank(str(r["namespace"]), str(r["kind"]), str(r.get("source", ""))))
    return ordered


def stamp_since(
    content: dict[str, Any],
    old_payload: dict[str, Any] | None,
    old_valid_from: str | None,
    observed_at: str,
) -> dict[str, Any]:
    """``since`` новой версии источника: значение, не изменившееся с прошлой версии
    того же ``(source, scope)``, сохраняет её время; новое или изменённое — ``observed_at``.

    У прошлой версии без ``since`` (записана до MEM-ADR-022) время значения — её
    ``valid_from``.
    """
    old = old_payload or {}
    old_since = old.get(SINCE_KEY) if isinstance(old.get(SINCE_KEY), dict) else {}
    old_attrs = old.get("attributes") if isinstance(old.get("attributes"), dict) else {}
    old_attr_since = old_since.get("attributes") if isinstance(old_since, dict) else None
    old_attr_since = old_attr_since if isinstance(old_attr_since, dict) else {}
    fallback = old_valid_from or observed_at

    def _kept(same: bool, stamp: Any) -> str:
        return str(stamp or fallback) if old_payload is not None and same else observed_at

    attributes = {
        name: _kept(name in old_attrs and old_attrs[name] == value, old_attr_since.get(name))
        for name, value in (content.get("attributes") or {}).items()
        if value is not None
    }
    title = _kept(old.get("title") == content.get("title"), old_since.get("title"))
    return {"title": title, "attributes": attributes}


def _since(row: dict[str, Any], name: str | None) -> str:
    """С какого момента версия держит значение атрибута ``name`` (None — заголовок)."""
    payload = row.get("payload") or {}
    since = payload.get(SINCE_KEY) if isinstance(payload.get(SINCE_KEY), dict) else {}
    if name is None:
        stamp = since.get("title")
    else:
        attrs = since.get("attributes") if isinstance(since.get("attributes"), dict) else {}
        stamp = attrs.get(name)
    return str(stamp or row.get("valid_from") or "")


def _winner(rows: list[dict[str, Any]], rank: SourceRank, name: str | None) -> dict[str, Any]:
    """Версия, чьё значение атрибута ``name`` побеждает: ранг, поздний since, (source, scope)."""
    ordered = sorted(rows, key=lambda r: int(r.get("id") or 0), reverse=True)
    ordered.sort(key=lambda r: (str(r.get("source", "")), str(r.get("scope", ""))))
    ordered.sort(key=lambda r: _since(r, name), reverse=True)
    ordered.sort(key=lambda r: rank(str(r["namespace"]), str(r["kind"]), str(r.get("source", ""))))
    return ordered[0]


def merge_rows(rows: list[dict[str, Any]], rank: SourceRank | None = None) -> dict[str, Any]:
    """Свести версии ОДНОЙ сущности (строки журнала формы ``_entity_row``) в одну строку.

    Строка сведения — той же формы (``id``, ``namespace``, ``kind``, ``key``, ``source``,
    ``scope``, ``snapshot_id``, ``payload``, ``valid_from``, ``valid_to``) плюс
    ``sources``: версии по старшинству ``{source, scope, snapshot_id, source_path,
    valid_from}``. ``source``/``scope``/``snapshot_id`` — старшей версии.
    """
    if not rows:
        raise ValueError("merge_rows: нужна хотя бы одна версия")
    rank = rank or no_priority
    ordered = _precedence(rows, rank)
    primary = ordered[0]
    key = str(primary["key"])
    holders: dict[str, list[dict[str, Any]]] = {}
    aliases: set[str] = set()
    scopes: set[str] = set()
    for row in ordered:
        payload = row.get("payload") or {}
        attrs = payload.get("attributes")
        for name, value in attrs.items() if isinstance(attrs, dict) else ():
            if value is not None:
                holders.setdefault(name, []).append(row)
        aliases.update(str(a) for a in payload.get("aliases") or [])
        scopes.update(str(s) for s in payload.get("scopes") or [])
    attributes = {
        name: _winner(group, rank, name)["payload"]["attributes"][name]
        for name, group in sorted(holders.items())
    }
    titled = [r for r in ordered if str((r.get("payload") or {}).get("title") or key) != key]
    title = str(_winner(titled, rank, None)["payload"]["title"]) if titled else key
    head = primary.get("payload") or {}
    payload = {
        **{
            k: v for k, v in head.items() if k not in ("attributes", "aliases", "scopes", SINCE_KEY)
        },
        "title": title,
        "attributes": attributes,
        "aliases": sorted(aliases),
        "scopes": sorted(scopes),
        "source_path": source_path_of(primary),
    }
    ends = [str(r["valid_to"]) for r in rows if r.get("valid_to")]
    return {
        **primary,
        "id": max(int(r.get("id") or 0) for r in rows),
        "payload": payload,
        "valid_from": max(str(r.get("valid_from") or "") for r in rows) or None,
        "valid_to": min(ends) if ends else None,
        "sources": [
            {
                "source": r.get("source", ""),
                "scope": r.get("scope", ""),
                "snapshot_id": r.get("snapshot_id", ""),
                "source_path": source_path_of(r),
                "valid_from": r.get("valid_from"),
            }
            for r in ordered
        ],
    }


def source_path_of(row: dict[str, Any]) -> str:
    """Цитата версии: ``source_path`` снимка, иначе ссылка на сам снимок."""
    payload = row.get("payload") or {}
    return str(
        payload.get("source_path")
        or f"snapshot:{row.get('source', '')}/{row.get('snapshot_id', '')}"
    )


def visibility_class(row: dict[str, Any]) -> tuple[str, ...]:
    """Класс видимости версии — её набор scopes (порядок и повторы не важны)."""
    scopes = (row.get("payload") or {}).get("scopes")
    return tuple(sorted({str(s) for s in scopes})) if isinstance(scopes, list) else ()


def _class_width(cls: tuple[str, ...]) -> int:
    """Уровень класса: 0 — без scopes (видна всем), 1 — только scopes релевантности,
    2 — со scopes видимости (видна только тем, чьи разрешённые scopes её пересекают)."""
    if not cls:
        return 0
    return 2 if has_visibility_scope(cls) else 1


def node_versions(
    rows: list[dict[str, Any]], rank: SourceRank | None = None
) -> list[dict[str, Any]]:
    """Версии ОДНОЙ сущности, которые сводятся в узел графа и строку индекса.

    Только версии одного класса видимости: самого широкого из открытых, внутри
    уровня — класса старшей версии. Узел со scopes этого класса не показывает никому
    атрибутов версий, которые тому не видны (сведение видимость не расширяет).
    """
    if not rows:
        return []
    ordered = _precedence(rows, rank or no_priority)
    chosen = visibility_class(min(ordered, key=lambda r: _class_width(visibility_class(r))))
    return [r for r in rows if visibility_class(r) == chosen]


def merge_by_entity(
    rows: Iterable[dict[str, Any]], rank: SourceRank | None = None, *, node: bool = False
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Свести версии по сущностям: ``(namespace, kind, key)`` -> строка сведения.

    ``node`` — сведение для узла графа и строки индекса: только версии одного класса
    видимости (``node_versions``). Порядок сущностей — порядок первой встречи в ``rows``.
    """
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["namespace"], row["kind"], row["key"]), []).append(row)
    return {
        ident: merge_rows(node_versions(group, rank) if node else group, rank)
        for ident, group in grouped.items()
    }


__all__ = [
    "SINCE_KEY",
    "SourceRank",
    "merge_by_entity",
    "merge_rows",
    "no_priority",
    "node_versions",
    "rank_by_catalog",
    "rank_by_catalogs",
    "source_path_of",
    "stamp_since",
    "visibility_class",
]
