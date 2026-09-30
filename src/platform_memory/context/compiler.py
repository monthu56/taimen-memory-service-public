"""Context Compiler: ContextRequest -> ContextPack (ADR-016 §6, §16–25).

Конвейер: каналы-кандидаты (resolve, lexical, vector, graph, facts, recent) -> RRF-merge
-> temporal/scope-фильтры -> прозрачный weighted ranking -> dedup -> секции ->
token budgeting -> provenance -> trace. LLM не участвует: компилятор собирает
evidence, синтез ответа — отдельный слой (``query()``).

Ranking детерминирован и документирован (§23): базис — Reciprocal Rank Fusion
по каналам (вес канала / (60 + rank)), поверх — аддитивные бонусы recency,
temporal validity, evidence strength, scope proximity, identifier match и
разрешение идентификатора в сущность (канал ``resolve``, MEM-ADR-020 амендмент).
Сигналы каждого элемента возвращаются в ``signals`` — выдача объяснима.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import psycopg

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.identifiers import extract_identifiers
from platform_memory.core.llm_safety import neutralize_prompt_injection
from platform_memory.core.namespaces import resolve_namespaces
from platform_memory.core.observations import entity_ref_key
from platform_memory.core.scopes import MAX_ALLOWED_SCOPES, is_visible, resolve_scopes
from platform_memory.domain.merge import rank_by_catalogs
from platform_memory.domain.reconcile import SnapshotLedger, valid_at
from platform_memory.domain.registry import open_registry
from platform_memory.domain.searchable import entity_index
from platform_memory.graph.facts import EVIDENCE_RANK, FactStore, normalize_ts
from platform_memory.graph.store import GraphStore
from platform_memory.index import VectorIndex, build_embedder
from platform_memory.index.entities import EntityIndex
from platform_memory.index.embeddings import Embedder
from platform_memory.context.budget import TokenEstimator, apply_budget
from platform_memory.context.models import (
    SECTION_CURRENT,
    SECTION_DOCUMENTS,
    SECTION_ENTITIES,
    SECTION_EPISODES,
    SECTION_FACTS,
    SECTION_OBSERVATIONS,
    ContextItem,
    ContextPack,
    ContextRequest,
    ContextSection,
)
from platform_memory.context.resolve import (
    MAX_PER_SUFFIX,
    MAX_RESOLVED,
    entity_text,
    extract_candidates,
    resolve_candidates,
    source_path_of,
)
from platform_memory.context.trace import ContextTraceStore
from platform_memory.observations.store import ObservationStore

# Веса каналов в RRF-базисе (документированы в docs/context-engine.md).
CHANNEL_WEIGHTS = {
    "resolve": 1.0,
    "lexical": 1.0,
    "vector": 1.0,
    "facts": 1.0,
    "graph": 0.7,
    "recent": 0.6,
}
RRF_K = 60

# Аддитивные бонусы (малы относительно RRF-базиса ~1/60, но решают ничьи).
BONUS_IDENTIFIER = 0.010  # точное совпадение идентификатора из запроса
BONUS_SCOPE = 0.004  # элемент в явно запрошенном scope
BONUS_VALID_OPEN = 0.003  # действующий факт (интервал не закрыт)
BONUS_EVIDENCE = 0.002  # за единицу ранга evidence (asserted=3 -> +0.006)
BONUS_RECENCY_MAX = 0.005  # максимум за свежесть; экспоненциальный спад
RECENCY_HALFLIFE_DAYS = 14.0
# Сущность, в которую разрешён идентификатор запроса (канал resolve): точный ключ,
# псевдоним или нормализованная форма — выше любого наблюдения. Максимум наблюдения
# (RRF lexical + recent и все бонусы) ≈ 0.0452; точно разрешённая на последнем ранге
# (19) без прочих бонусов — 1/80 + 0.040 = 0.0525. Суффикс ключа — слабее.
BONUS_RESOLVED_EXACT = 0.040
BONUS_RESOLVED_SUFFIX = 0.015


def _recency_bonus(occurred_at: str, now: dt.datetime) -> float:
    """Бонус свежести: экспоненциальный спад с полупериодом RECENCY_HALFLIFE_DAYS."""
    if not occurred_at:
        return 0.0
    try:
        seen = dt.datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=dt.UTC)
    days = max(0.0, (now - seen).total_seconds() / 86400.0)
    return BONUS_RECENCY_MAX * (0.5 ** (days / RECENCY_HALFLIFE_DAYS))


def _scopes_of(raw: Any) -> list[str]:
    return [str(s) for s in raw] if isinstance(raw, list) else []


class _Candidate:
    """Кандидат до секционирования: накапливает ранги каналов и сигналы."""

    __slots__ = ("kind", "id", "text", "title", "source_path", "provenance", "signals", "scopes")

    def __init__(self, kind: str, cid: str, **kw: Any):
        self.kind = kind
        self.id = cid
        self.text = str(kw.get("text", "") or "")
        self.title = str(kw.get("title", "") or "")
        self.source_path = str(kw.get("source_path", "") or "")
        self.provenance = dict(kw.get("provenance") or {})
        self.signals: dict[str, Any] = {"channels": {}}
        self.scopes = list(kw.get("scopes") or [])

    def seen_in(self, channel: str, rank: int) -> None:
        self.signals["channels"].setdefault(channel, rank)


def score_candidates(
    candidates: dict[str, _Candidate],
    *,
    identifiers: list[str],
    request_scopes: list[str],
    now: dt.datetime,
) -> list[ContextItem]:
    """Прозрачный детерминированный скоринг (чистая функция; §23).

    score = Σ_каналов weight/(RRF_K + rank) + бонусы; все слагаемые видны в
    ``signals`` элемента.
    """
    items: list[ContextItem] = []
    req_scope_set = set(request_scopes)
    for cand in candidates.values():
        score = 0.0
        for channel, rank in cand.signals["channels"].items():
            score += CHANNEL_WEIGHTS.get(channel, 0.5) / (RRF_K + rank + 1)
        text_l = cand.text.lower()
        if identifiers and any(tok.lower() in text_l for tok in identifiers):
            score += BONUS_IDENTIFIER
            cand.signals["identifier_match"] = True
        if req_scope_set and req_scope_set.intersection(cand.scopes):
            score += BONUS_SCOPE
            cand.signals["scope_match"] = True
        evidence = str(cand.signals.get("evidence", "") or "")
        if evidence:
            score += BONUS_EVIDENCE * EVIDENCE_RANK.get(evidence, 0)
        elif set(cand.signals["channels"]) == {"vector"}:
            # Найдено только семантикой — помечается, но бонуса не меняет.
            cand.signals["evidence"] = "inferred"
        resolved = cand.signals.get("resolve")
        if isinstance(resolved, dict):
            score += BONUS_RESOLVED_EXACT if resolved.get("exact") else BONUS_RESOLVED_SUFFIX
        if cand.signals.get("valid_open"):
            score += BONUS_VALID_OPEN
        recency = _recency_bonus(str(cand.signals.get("occurred_at", "") or ""), now)
        if recency:
            score += recency
            cand.signals["recency_bonus"] = round(recency, 6)
        items.append(
            ContextItem(
                kind=cand.kind,
                id=cand.id,
                # Барьер prompt-injection обязателен на каждом пути выдачи.
                text=neutralize_prompt_injection(cand.text),
                title=cand.title,
                source_path=cand.source_path,
                score=score,
                signals=cand.signals,
                provenance=cand.provenance,
                conflict=bool(cand.signals.get("conflict")),
            )
        )
    # Детерминизм: score убыв., затем стабильный id.
    items.sort(key=lambda i: (-i.score, i.id))
    return items


def build_sections(items: list[ContextItem], ephemeral: dict[str, Any]) -> list[ContextSection]:
    """Разложить элементы по секциям (чистая функция; §22)."""
    by_kind: dict[str, list[ContextItem]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    sections: list[ContextSection] = []
    current = ContextSection(SECTION_CURRENT)
    if ephemeral:
        current.items.append(
            ContextItem(
                kind="current",
                id="current:ephemeral",
                text=json.dumps(ephemeral, ensure_ascii=False, indent=1),
                title="Текущее состояние",
                signals={"authoritative": True},
                provenance={"origin": "caller"},
            )
        )
    sections.append(current)
    sections.append(ContextSection(SECTION_FACTS, by_kind.get("fact", [])))
    sections.append(ContextSection(SECTION_EPISODES, by_kind.get("episode", [])))
    sections.append(ContextSection(SECTION_DOCUMENTS, by_kind.get("document", [])))
    sections.append(ContextSection(SECTION_ENTITIES, by_kind.get("entity", [])))
    sections.append(ContextSection(SECTION_OBSERVATIONS, by_kind.get("observation", [])))
    return sections


def _channels_for(strategy: str) -> set[str]:
    """Каналы retrieval по стратегии (§17); дефолт — hybrid (все)."""
    # resolve — во всех, кроме чисто семантической: детерминированное разрешение
    # идентификаторов запроса и якорей в сущности (MEM-ADR-020, амендмент).
    mapping = {
        "semantic": {"vector"},
        "exact": {"resolve", "lexical"},
        "graph": {"resolve", "graph", "facts"},
        "hybrid": {"resolve", "lexical", "vector", "graph", "facts", "recent"},
        "context": {"resolve", "lexical", "vector", "graph", "facts", "recent"},
        # briefing (TAI-ADR-0031 §6): стоячий контекст principal без запроса —
        # факты, соседи якорей и свежие наблюдения; lexical/vector без query молчат.
        "briefing": {"resolve", "graph", "facts", "recent"},
        "": {"resolve", "lexical", "vector", "graph", "facts", "recent"},
    }
    return mapping[strategy]


def _resolve_channel(
    conn: psycopg.Connection,
    settings: Settings,
    graph: GraphStore,
    req: ContextRequest,
    nss: list[str],
    scopes: list[str],
    allowed: list[str] | None,
    as_of: str,
) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """Канал resolve (MEM-ADR-020, амендмент): (разрешённые, отчёт, входы-кандидаты).

    Индекс — журнал версий сверки; без журнала канал молчит. Разрешённая версия
    проходит те же фильтры, что узлы graph-канала: запрошенные scopes (пересечение
    или без scopes) и видимость по principal (MEM-ADR-019/021, уже в SQL), — и
    должна существовать узлом графа (узел, удалённый через API, не всплывает).
    """
    ledger = SnapshotLedger(conn, settings.snapshots_table)
    if not ledger.table_exists():
        return [], [], []
    registry = open_registry(conn, settings)
    catalogs = [registry.catalog_for(ns) for ns in nss]
    anchors = list(req.anchors) + ([req.subject] if req.subject else [])
    candidates = extract_candidates(req.query, anchors, catalogs)
    if not candidates:
        return [], [], []
    hits, report = resolve_candidates(
        ledger,
        candidates,
        nss,
        catalogs=catalogs,
        as_of=as_of,
        allowed_scopes=allowed,
        rank=rank_by_catalogs(dict(zip(nss, catalogs, strict=True))),
    )
    wanted = set(scopes)
    hits = [
        h
        for h in hits
        if not wanted
        or not _scopes_of(h.row["payload"].get("scopes"))
        or wanted & set(_scopes_of(h.row["payload"].get("scopes")))
    ]
    by_kind: dict[tuple[str, str], list[str]] = {}
    for h in hits:
        by_kind.setdefault((h.namespace, h.kind), []).append(h.key)
    present = {
        (ns, kind, key)
        for (ns, kind), keys in sorted(by_kind.items())
        for key in graph.existing_keys(kind, keys, ns)
    }
    hits = [h for h in hits if (h.namespace, h.kind, h.key) in present]
    kept = {(h.namespace, h.kind, h.key) for h in hits}
    for entry in report:
        entry["resolved"] = [
            r for r in entry["resolved"] if (r["namespace"], r["kind"], r["key"]) in kept
        ]
    return hits, report, [c.value for c in candidates]


def _entity_vector_channel(
    conn: psycopg.Connection,
    settings: Settings,
    graph: GraphStore,
    ents: EntityIndex,
    q_emb: list[float],
    k: int,
    nss: list[str],
    scopes: list[str],
    allowed: list[str] | None,
    as_of: str,
    add: Callable[..., Any],
) -> None:
    """Сущности индекса ``searchable`` в канал ``vector`` (MEM-ADR-020, амендмент 2026-09-28).

    Кандидат — сама сущность (``node:<ключ>``, как у каналов resolve и graph — один
    элемент на узел), ранги — свои в том же канале. Фильтры — как у resolve: scopes
    запроса и видимость principal (в SQL), узел существует в графе; индекс держит
    текущие версии, поэтому при ``as_of`` сущность, открытая позже, не берётся.
    """
    registry = open_registry(conn, settings)
    kinds = {kind for ns in nss for kind in registry.catalog_for(ns).searchable_kinds()}
    if not kinds:
        return
    hits = [
        h
        for h in ents.search(
            q_emb, k, namespaces=nss, kinds=kinds, scopes=scopes, allowed_scopes=allowed
        )
        if not as_of or not h.row.valid_from or h.row.valid_from <= as_of
    ]
    by_kind: dict[tuple[str, str], list[str]] = {}
    for h in hits:
        by_kind.setdefault((h.row.namespace, h.row.kind), []).append(h.row.natural_key)
    present = {
        (ns, kind, key)
        for (ns, kind), keys in sorted(by_kind.items())
        for key in graph.existing_keys(kind, keys, ns)
    }
    rank = 0
    for h in hits:
        row = h.row
        if (row.namespace, row.kind, row.natural_key) not in present:
            continue
        cand = add(
            "entity",
            f"node:{row.natural_key}",
            "vector",
            rank,
            text=row.text,
            title=neutralize_prompt_injection(row.title),
            source_path=row.source_path,
            provenance={
                "node_key": row.natural_key,
                "node_type": row.kind,
                "namespace": row.namespace,
                "snapshot": {
                    "source": row.source,
                    "scope": row.scope,
                    "snapshot_id": row.snapshot_id,
                },
            },
            scopes=row.scopes,
        )
        cand.signals.setdefault("similarity", h.score)
        cand.signals["matched_on"] = "entity"
        rank += 1


def build_context(
    settings: Settings,
    request: ContextRequest | dict[str, Any],
    *,
    embedder: Embedder | None = None,
    conn: psycopg.Connection | None = None,
) -> ContextPack:
    """Скомпилировать bounded ContextPack по запросу (без LLM-синтеза).

    Свежие наблюдения доступны сразу после ingest (lexical/recent-каналы) —
    консолидации ждать не нужно (§31). ``ephemeral_context`` не сохраняется.
    """
    req = ContextRequest.from_payload(request) if isinstance(request, dict) else request
    nss = resolve_namespaces(req.namespaces, settings.default_namespace)
    scopes = resolve_scopes(req.scopes)
    allowed = (
        None
        if req.allowed_scopes is None
        else resolve_scopes(req.allowed_scopes, limit=MAX_ALLOWED_SCOPES)
    )
    identifiers = extract_identifiers(req.query)
    try:
        as_of = (normalize_ts(req.as_of) or "") if req.as_of else ""
    except ValueError:
        as_of = req.as_of
    channels = _channels_for(req.strategy)
    max_tokens = max(0, req.max_tokens) or settings.context_default_max_tokens
    now = dt.datetime.now(dt.UTC)
    trace_id = f"ctx-{uuid.uuid4().hex[:16]}"
    latencies: dict[str, int] = {}

    own_conn = conn is None
    conn = conn or dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        facts_store = FactStore(graph)
        index = VectorIndex(
            conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
        )
        obs_store = ObservationStore(conn, settings.observations_table, settings.default_namespace)

        candidates: dict[str, _Candidate] = {}

        def _add(kind: str, cid: str, channel: str, rank: int, **kw: Any) -> _Candidate:
            cand = candidates.get(cid)
            if cand is None:
                cand = candidates[cid] = _Candidate(kind, cid, **kw)
            cand.seen_in(channel, rank)
            return cand

        anchor_keys = [entity_ref_key(a) for a in req.anchors]
        if req.subject:
            anchor_keys.append(entity_ref_key(req.subject))
        anchor_keys = list(dict.fromkeys(anchor_keys))

        # --- resolve: идентификаторы запроса и якоря -> сущности (без LLM и вектора) ---
        resolved = []
        resolve_report: list[dict[str, Any]] = []
        resolve_inputs: list[str] = []
        if "resolve" in channels:
            t0 = time.monotonic()
            resolved, resolve_report, resolve_inputs = _resolve_channel(
                conn, settings, graph, req, nss, scopes, allowed, as_of
            )
            for rank, hit in enumerate(resolved):
                payload = hit.row.get("payload") or {}
                title, text = entity_text(hit)
                cand = _add(
                    "entity",
                    f"node:{hit.key}",
                    "resolve",
                    rank,
                    text=text,
                    title=neutralize_prompt_injection(title),
                    source_path=source_path_of(hit),
                    provenance={
                        "node_key": hit.key,
                        "node_type": hit.kind,
                        "namespace": hit.namespace,
                        "snapshot": {
                            "source": hit.row.get("source", ""),
                            "scope": hit.row.get("scope", ""),
                            "snapshot_id": hit.row.get("snapshot_id", ""),
                        },
                        **(
                            {"source": payload["provenance"]}
                            if isinstance(payload.get("provenance"), dict)
                            else {}
                        ),
                    },
                    scopes=_scopes_of(payload.get("scopes")),
                )
                cand.signals["evidence"] = "resolved"
                cand.signals["resolve"] = {
                    "input": hit.input,
                    "method": hit.method,
                    "exact": hit.exact,
                }
                cand.signals["occurred_at"] = str(hit.row.get("valid_from") or "")
            latencies["resolve_ms"] = int((time.monotonic() - t0) * 1000)

        # --- lexical: чанки + наблюдения (работает без эмбеддингов) ---
        if "lexical" in channels and (req.query.strip() or identifiers):
            t0 = time.monotonic()
            if index.table_exists():
                for rank, hit in enumerate(
                    index.lexical_search(
                        req.query,
                        identifiers,
                        k=req.k,
                        namespaces=nss,
                        scopes=scopes,
                        allowed_scopes=allowed,
                    )
                ):
                    cand = _add(
                        "document",
                        f"chunk:{hit.chunk_id}",
                        "lexical",
                        rank,
                        text=hit.text,
                        title=hit.title,
                        source_path=hit.source_path,
                        provenance={"node_key": hit.node_key, "namespace": hit.namespace},
                        scopes=_scopes_of(hit.meta.get("scopes")),
                    )
                    if hit.meta.get("observation_id"):
                        cand.provenance["observation_id"] = hit.meta["observation_id"]
            if obs_store.table_exists():
                for rank, rec in enumerate(
                    obs_store.search_lexical(
                        req.query,
                        identifiers,
                        namespaces=nss,
                        scopes=scopes,
                        limit=req.k,
                        allowed_scopes=allowed,
                    )
                ):
                    cand = _add(
                        "observation",
                        rec.observation_id,
                        "lexical",
                        rank,
                        text=rec.content,
                        title=rec.kind or "observation",
                        source_path=str(rec.provenance.get("uri", "") or ""),
                        provenance={
                            "observation_id": rec.observation_id,
                            "source": {
                                "system": rec.source_system,
                                "stream": rec.source_stream,
                                "external_id": rec.external_id,
                            },
                        },
                        scopes=rec.scopes,
                    )
                    cand.signals["occurred_at"] = rec.occurred_at
            latencies["lexical_ms"] = int((time.monotonic() - t0) * 1000)

        # --- vector: семантика по чанкам и по индексу сущностей (searchable) ---
        vector_hits = []
        ents = entity_index(conn, settings)
        has_chunks = index.table_exists()
        has_entities = "vector" in channels and ents.table_exists()
        if "vector" in channels and req.query.strip() and (has_chunks or has_entities):
            t0 = time.monotonic()
            embedder = embedder or build_embedder(settings)
            q_emb = embedder.embed_one(req.query)
            if has_chunks:
                vector_hits = index.search(
                    q_emb, req.query, k=req.k, namespaces=nss, scopes=scopes, allowed_scopes=allowed
                )
            if has_entities:
                _entity_vector_channel(
                    conn, settings, graph, ents, q_emb, req.k, nss, scopes, allowed, as_of, _add
                )
            for rank, hit in enumerate(vector_hits):
                cand = _add(
                    "document",
                    f"chunk:{hit.chunk_id}",
                    "vector",
                    rank,
                    text=hit.text,
                    title=hit.title,
                    source_path=hit.source_path,
                    provenance={"node_key": hit.node_key, "namespace": hit.namespace},
                    scopes=_scopes_of(hit.meta.get("scopes")),
                )
                cand.signals["similarity"] = hit.score
                if hit.meta.get("observation_id"):
                    cand.provenance["observation_id"] = hit.meta["observation_id"]
            latencies["vector_ms"] = int((time.monotonic() - t0) * 1000)

        # --- graph expansion: от якорей и топ-хитов, с жёсткими лимитами (§24) ---
        if "graph" in channels:
            t0 = time.monotonic()
            hops = max(1, min(int(settings.context_max_depth), 2))
            # Разрешённые сущности — отдельные seeds со своим лимитом: соседи якорей
            # (наблюдения задачи) не вытесняют окрестность контрактов. Закрытые на
            # as_of соседи из этой окрестности не берутся.
            resolved_seeds = list(dict.fromkeys(h.key for h in resolved))
            neighbors = []
            if resolved_seeds:
                neighbors = [
                    n
                    for n in graph.expand_neighbors(
                        resolved_seeds,
                        hops=hops,
                        limit=settings.context_max_nodes,
                        namespaces=sorted({h.namespace for h in resolved}),
                    )
                    if valid_at(n.get("valid_from"), n.get("valid_to"), as_of)
                ]
            seeds = list(anchor_keys)
            seeds += [h.node_key for h in vector_hits[:3]]
            seeds = list(dict.fromkeys(seeds))[: settings.context_max_nodes]
            if seeds:
                neighbors += graph.expand_neighbors(
                    seeds, hops=hops, limit=settings.context_max_nodes, namespaces=nss
                )
            if neighbors:
                rank = 0
                for node in neighbors:
                    props = node.get("props") if isinstance(node.get("props"), dict) else {}
                    node_scopes = _scopes_of(props.get("scopes"))
                    # Scope-фильтр соседей: пересечение или узел без scopes; плюс
                    # видимость по principal (MEM-ADR-019).
                    if scopes and node_scopes and not set(scopes) & set(node_scopes):
                        continue
                    if not is_visible(node_scopes, allowed):
                        continue
                    nk = str(node.get("natural_key", ""))
                    if not nk:
                        continue
                    ntype = str(node.get("type", "entity"))
                    kind = "episode" if ntype == "episode" else "entity"
                    text = str(props.get("content", "") or "").strip() or str(node.get("title", ""))
                    cand = _add(
                        kind,
                        f"node:{nk}",
                        "graph",
                        rank,
                        text=text,
                        title=str(node.get("title", "")),
                        source_path=str(node.get("source_path", "") or ""),
                        provenance={"node_key": nk, "node_type": ntype},
                        scopes=node_scopes,
                    )
                    if props.get("observation_id"):
                        cand.provenance["observation_id"] = props["observation_id"]
                    rank += 1
            latencies["graph_ms"] = int((time.monotonic() - t0) * 1000)

        # --- facts: temporal-факты вокруг якорей (as_of; конфликты помечаются) ---
        if "facts" in channels and anchor_keys:
            t0 = time.monotonic()
            all_facts: list[dict[str, Any]] = []
            for key in anchor_keys[:10]:
                for direction in ("subject", "obj"):
                    got = facts_store.facts(
                        **{direction: key},
                        namespaces=nss,
                        as_of=req.as_of,
                        include_closed=bool(req.as_of),
                        limit=req.k * 4,
                    )
                    all_facts.extend(got)
            # Дедуп по fact_id до пометки конфликтов.
            uniq: dict[str, dict[str, Any]] = {}
            for fact in all_facts:
                fid = str(fact.get("fact_id", ""))
                if fid:
                    uniq[fid] = fact
            # Scope-фильтр фактов (как у чанков/узлов): пересечение или факт без scopes.
            visible = [
                f
                for f in uniq.values()
                if (
                    not scopes
                    or not _scopes_of(f.get("scopes"))
                    or set(scopes) & set(_scopes_of(f.get("scopes")))
                )
                and is_visible(_scopes_of(f.get("scopes")), allowed)
            ]
            marked = FactStore.find_conflicts(visible)
            for rank, fact in enumerate(marked):
                fid = str(fact.get("fact_id"))
                subj_t = str(fact.get("subject_title") or fact.get("subject"))
                obj_t = str(fact.get("object_title") or fact.get("object"))
                vf, vt = str(fact.get("valid_from") or ""), fact.get("valid_to")
                interval = (
                    f" (с {vf}" + (f" по {vt})" if vt else ", действует)") if vf or vt else ""
                )
                text = f"{subj_t} —{fact.get('predicate')}→ {obj_t}{interval}"
                cand = _add(
                    "fact",
                    fid,
                    "facts",
                    rank,
                    text=text,
                    title=str(fact.get("predicate", "")),
                    source_path=str(fact.get("source_path", "") or ""),
                    provenance={
                        "fact_id": fid,
                        "observation_ids": list(fact.get("observation_ids") or []),
                        "subject": fact.get("subject"),
                        "object": fact.get("object"),
                    },
                    scopes=_scopes_of(fact.get("scopes")),
                )
                cand.signals["evidence"] = str(fact.get("evidence", "") or "")
                cand.signals["confidence"] = fact.get("confidence")
                cand.signals["valid_open"] = not fact.get("valid_to")
                cand.signals["occurred_at"] = str(fact.get("observed_at", "") or "")
                if fact.get("conflict"):
                    cand.signals["conflict"] = True
                    cand.signals["conflicts_with"] = list(fact.get("conflicts_with") or [])
            latencies["facts_ms"] = int((time.monotonic() - t0) * 1000)

        # --- recent: свежие наблюдения по scopes (read-after-ingest, §31) ---
        if "recent" in channels and obs_store.table_exists():
            t0 = time.monotonic()
            for rank, rec in enumerate(
                obs_store.list_recent(nss, scopes=scopes, limit=req.k, allowed_scopes=allowed)
            ):
                if not rec.content:
                    continue
                cand = _add(
                    "observation",
                    rec.observation_id,
                    "recent",
                    rank,
                    text=rec.content,
                    title=rec.kind or "observation",
                    source_path=str(rec.provenance.get("uri", "") or ""),
                    provenance={
                        "observation_id": rec.observation_id,
                        "source": {
                            "system": rec.source_system,
                            "stream": rec.source_stream,
                            "external_id": rec.external_id,
                        },
                    },
                    scopes=rec.scopes,
                )
                cand.signals["occurred_at"] = rec.occurred_at
            latencies["recent_ms"] = int((time.monotonic() - t0) * 1000)

        # --- скоринг, секции, бюджет ---
        items = score_candidates(
            candidates, identifiers=identifiers, request_scopes=scopes, now=now
        )
        sections = build_sections(items, req.ephemeral_context)
        estimator = TokenEstimator(settings.context_chars_per_token)
        sections, budget_report, total = apply_budget(
            sections, max_tokens=max_tokens, estimator=estimator
        )

        # Источники: дедуп по source_path/node_key среди вошедших элементов.
        sources: list[dict[str, str]] = []
        seen_src: set[tuple[str, str]] = set()
        conflicts: list[str] = []
        for section in sections:
            for item in section.items:
                key = (item.source_path, str(item.provenance.get("node_key", item.id)))
                if item.source_path and key not in seen_src:
                    seen_src.add(key)
                    sources.append(
                        {
                            "source_path": item.source_path,
                            "node_key": str(item.provenance.get("node_key", "")),
                            "title": item.title,
                        }
                    )
                if item.conflict:
                    conflicts.append(item.id)

        stats = {
            "candidates": len(candidates),
            "selected": sum(len(s.items) for s in sections),
            "channels": {
                ch: sum(1 for c in candidates.values() if ch in c.signals["channels"])
                for ch in sorted(channels)
            },
            "identifiers": identifiers,
            "resolved": [
                {"namespace": h.namespace, "kind": h.kind, "key": h.key, "method": h.method}
                for h in resolved
            ],
            "latencies_ms": latencies,
        }
        pack = ContextPack(
            query=req.query,
            sections=sections,
            sources=sources,
            token_estimate=total,
            budget=budget_report,
            conflicts=conflicts,
            trace_id=trace_id,
            stats=stats,
        )

        # --- trace: request-эхо (без ephemeral-содержимого) + решения ---
        try:
            traces = ContextTraceStore(
                conn, settings.context_traces_table, settings.default_namespace
            )
            traces.ensure_schema()
            traces.record(
                trace_id,
                nss[0],
                request={
                    "query": req.query,
                    "scopes": scopes,
                    "anchors": [entity_ref_key(a) for a in req.anchors],
                    "resolve_candidates": resolve_inputs,
                    "strategy": req.strategy or "hybrid",
                    "namespaces": nss,
                    # видимость автора: по ней GET трейса решает, кому его показать
                    "allowed_scopes": allowed,
                    "as_of": req.as_of,
                    "max_tokens": max_tokens,
                    # ephemeral_context сознательно НЕ сохраняется (§18) — только размер.
                    "ephemeral_bytes": len(json.dumps(req.ephemeral_context, ensure_ascii=False))
                    if req.ephemeral_context
                    else 0,
                },
                decisions={
                    "stats": stats,
                    "budget": budget_report,
                    "token_estimate": total,
                    "ranking": {
                        "rrf_k": RRF_K,
                        "channel_weights": CHANNEL_WEIGHTS,
                        "bonuses": {
                            "resolved_exact": BONUS_RESOLVED_EXACT,
                            "resolved_suffix": BONUS_RESOLVED_SUFFIX,
                            "identifier": BONUS_IDENTIFIER,
                            "scope": BONUS_SCOPE,
                            "valid_open": BONUS_VALID_OPEN,
                            "evidence_per_rank": BONUS_EVIDENCE,
                            "recency_max": BONUS_RECENCY_MAX,
                        },
                    },
                    "selected_ids": [i.id for s in pack.sections for i in s.items],
                    "resolve": {
                        "limits": {"resolved": MAX_RESOLVED, "per_suffix": MAX_PER_SUFFIX},
                        "candidates": resolve_report,
                    },
                },
            )
        except psycopg.Error:
            # Трейс не должен ронять компиляцию (как аудит PII в app.py).
            pack.stats["trace_error"] = True
        return pack
    finally:
        if own_conn:
            conn.close()
