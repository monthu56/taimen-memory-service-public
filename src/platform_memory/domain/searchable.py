"""Индекс сущностей для поиска по смыслу: сверка журнала с ``EntityIndex`` (MEM-ADR-020).

Амендмент 2026-09-28, п.2. Источник истины — открытые версии журнала сверки: строка
индекса есть ровно у открытой сущности вида с ``searchable`` в каталоге namespace.
``sync_entities`` приводит индекс к журналу:

* по ключам, тронутым сверкой (открытые, изменённые, закрытые), — в той же транзакции,
  что и сверка: сущность индексируется при открытии и изменении и снимается при
  закрытии; закрытая одним источником, но открытая другим — остаётся с его версией;
* по всему namespace (``keys=None``) — переиндексация при смене пакетов namespace
  (``PUT …/kinds``): виды, ставшие индексируемыми, добавляются, переставшие — снимаются,
  изменившиеся ``fields`` — пересчитываются.

Вектор пересчитывается, только если изменился текст (``text_hash``); смена цитаты или
scopes без смены текста обновляет строку без вызова эмбеддера. Эмбеддер создаётся
лениво — сверка без индексируемых изменений его не вызывает.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import psycopg

from platform_memory.core.config import Settings
from platform_memory.core.kinds import KindCatalog
from platform_memory.core.ontology import sanitize_label
from platform_memory.index.embeddings import Embedder
from platform_memory.index.entities import EntityIndex, EntityRow

if TYPE_CHECKING:  # сверка сама вызывает sync_entities: импорт журнала — лениво
    from platform_memory.domain.reconcile import SnapshotLedger

# Батч эмбеддера при переиндексации (память процесса и размер запроса к gateway).
EMBED_BATCH = 256


def entity_index(conn: psycopg.Connection, settings: Settings) -> EntityIndex:
    """Индекс сущностей на соединении по имени таблицы и размерности из настроек."""
    return EntityIndex(
        conn, settings.entity_embeddings_table, settings.embedding_dim, settings.default_namespace
    )


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def desired_rows(
    ns: str, catalog: KindCatalog, versions: Iterable[dict[str, Any]]
) -> dict[tuple[str, str], EntityRow]:
    """Строки индекса для открытых версий журнала: (вид, ключ) -> строка."""
    specs = catalog.searchable_kinds()
    out: dict[tuple[str, str], EntityRow] = {}
    for v in versions:
        spec = specs.get(v["kind"])
        if spec is None:
            continue
        payload = v.get("payload") or {}
        title = str(payload.get("title") or v["key"])
        attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        text = spec.search_text(title, attrs)
        if not text:
            continue
        scopes = payload.get("scopes")
        row = EntityRow(
            namespace=ns,
            kind=v["kind"],
            natural_key=v["key"],
            title=title,
            text=text,
            text_hash=_text_hash(text),
            source_path=str(
                payload.get("source_path") or f"snapshot:{v['source']}/{v['snapshot_id']}"
            ),
            source=str(v.get("source") or ""),
            scope=str(v.get("scope") or ""),
            snapshot_id=str(v.get("snapshot_id") or ""),
            valid_from=str(v.get("valid_from") or ""),
            scopes=[str(s) for s in scopes] if isinstance(scopes, list) else [],
        )
        out[row.ident] = row
    return out


def sync_entities(
    index: EntityIndex,
    ledger: SnapshotLedger,
    ns: str,
    catalog: KindCatalog,
    embedder: Callable[[], Embedder],
    *,
    idents: Iterable[tuple[str, str]] | None = None,
) -> dict[str, int]:
    """Привести индекс namespace к открытым версиям журнала; вернуть счётчики.

    ``idents`` — пары ``(вид, ключ)``, тронутые сверкой (None — весь namespace).
    ``embedder`` — фабрика: вызывается, только если есть текст к векторизации.
    Счётчики: ``indexed`` — записано с новым вектором, ``updated`` — обновлена цитата
    или scopes без смены текста, ``removed`` — снято, ``unchanged``.
    """
    counts = {"indexed": 0, "updated": 0, "removed": 0, "unchanged": 0}
    searchable = sorted(catalog.searchable_kinds())
    if idents is None:
        wanted = None
        versions = ledger.open_entity_versions(ns, searchable)
        existing = index.rows(ns)
    else:
        wanted = set(idents)
        if not wanted:
            return counts
        kinds = sorted({k for k, _ in wanted})
        existing = {i: r for i, r in index.rows(ns, kinds).items() if i in wanted}
        versions = [
            v
            for v in ledger.open_entity_versions(
                ns, [k for k in kinds if k in searchable], [key for _, key in wanted]
            )
            if (v["kind"], v["key"]) in wanted
        ]
    desired = desired_rows(ns, catalog, versions)

    stale = [ident for ident in existing if ident not in desired]
    counts["removed"] = index.delete(ns, stale)
    to_embed: list[EntityRow] = []
    to_touch: list[EntityRow] = []
    for ident, row in desired.items():
        old = existing.get(ident)
        if old is None or old.text_hash != row.text_hash:
            to_embed.append(row)
        elif old != row:
            to_touch.append(row)
        else:
            counts["unchanged"] += 1
    counts["updated"] = index.update_meta(to_touch)
    if to_embed:
        emb = embedder()
        for i in range(0, len(to_embed), EMBED_BATCH):
            batch = to_embed[i : i + EMBED_BATCH]
            counts["indexed"] += index.upsert(batch, emb.embed([r.text for r in batch]))
    return counts


def reindex_namespace(
    settings: Settings,
    conn: psycopg.Connection,
    ns: str,
    catalog: KindCatalog,
    embedder: Callable[[], Embedder],
) -> dict[str, int]:
    """Переиндексировать namespace целиком (смена пакетов: ``PUT …/kinds``).

    Одной транзакцией под тем же advisory-локом, что и сверки namespace: сверка не
    вклинивается между чтением журнала и записью индекса, а ошибка эмбеддера
    оставляет прежний индекс (повтор ``PUT`` доделает). Без журнала сверки
    индексировать нечего; без таблицы индекса и без индексируемых видов — снимать
    нечего, таблица не создаётся.
    """
    from platform_memory.domain.reconcile import SnapshotLedger

    ledger = SnapshotLedger(conn, settings.snapshots_table)
    index = entity_index(conn, settings)
    if not catalog.searchable_kinds() and not index.table_exists():
        return {"indexed": 0, "updated": 0, "removed": 0, "unchanged": 0}
    index.ensure_schema()
    source = ledger if ledger.table_exists() else _EmptyLedger()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (reconcile_lock_key(sanitize_label(settings.graph_name), ns),),
            )
        return sync_entities(index, source, ns, catalog, embedder)


def reconcile_lock_key(graph_name: str, ns: str) -> str:
    """Ключ advisory-лока сверок namespace (общий со сверкой снимков; граф — санитизированный)."""
    return f"{graph_name}:reconcile:{ns}"


class _EmptyLedger:
    """Журнал без таблицы: открытых версий нет."""

    def open_entity_versions(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []


__all__ = [
    "desired_rows",
    "entity_index",
    "reconcile_lock_key",
    "reindex_namespace",
    "sync_entities",
]
