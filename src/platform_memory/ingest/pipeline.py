"""Оркестрация ingest: vault (read-only) -> узлы/рёбра в граф + чанки в pgvector. Идемпотентно."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from platform_memory.core import db as dbmod
from platform_memory.core.config import Settings
from platform_memory.core.models import Chunk, Edge, Node, Provenance
from platform_memory.graph import GraphStore
from platform_memory.index import VectorIndex, build_embedder
from platform_memory.index.embeddings import Embedder
from platform_memory.ingest import questions as qmod
from platform_memory.ingest.cache import ChunkCache, EmbeddingCache
from platform_memory.ingest.chunker import TextChunk, chunk_markdown
from platform_memory.ingest.links import (
    extract_wikilinks,
    parse_frontmatter_link,
    parse_frontmatter_links,
)
from platform_memory.ingest.resolver import Resolver
from platform_memory.ingest.vault import VaultDoc, read_docs

# Значения enum `project` (синтетические узлы-проекты) вынесены из кода в конфиг
# (``CB_PROJECT_VALUES``) ради продукт-нейтральности движка — см. ``Settings.project_value_set``.

Logger = Callable[[str], None]


@dataclass
class IngestStats:
    """Счётчики результатов прогона ingest (узлы, рёбра, чанки, самоочистка)."""

    files: int = 0
    nodes_by_type: Counter = field(default_factory=Counter)
    edges_by_type: Counter = field(default_factory=Counter)
    edges_ok: int = 0
    edges_unresolved: int = 0
    chunks: int = 0
    pruned_nodes: int = 0
    pruned_edges: int = 0
    pruned_chunks: int = 0
    embeddings_cached: int = 0  # входов эмбеддинга, взятых из кэша (не перебиллено)
    embeddings_computed: int = 0  # уникальных входов, реально отправленных эмбеддеру
    entities_extracted: int = 0  # свободных сущностей (после дедупа) спроецировано
    entity_mentions: int = 0  # рёбер MENTIONS заметка→сущность
    entity_merge_candidates: int = 0  # спорных слияний сущностей на арбитраж
    merge_candidates: list = field(default_factory=list)  # сами MergeCandidate (на персист)
    unresolved_samples: list[str] = field(default_factory=list)

    @property
    def nodes_total(self) -> int:
        """Суммарное число узлов по всем типам."""
        return sum(self.nodes_by_type.values())

    @property
    def edges_total(self) -> int:
        """Суммарное число рёбер по всем типам."""
        return sum(self.edges_by_type.values())


def _run_token() -> str:
    """Уникальный штамп прогона для mark-and-sweep (ISO-таймстамп с микросекундами)."""
    return dt.datetime.now().isoformat()


def _strip_ext(relpath: str) -> str:
    return relpath[:-3] if relpath.endswith(".md") else relpath


def _first_h1(body: str) -> str | None:
    for line in body.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _doc_natural_key(doc: VaultDoc) -> str:
    raw_id = doc.frontmatter.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    return _strip_ext(doc.relpath)


def _doc_title(doc: VaultDoc) -> str:
    fm = doc.frontmatter
    for key in ("title", "product_name", "name", "subject"):
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    h1 = _first_h1(doc.body)
    if h1:
        return h1
    return _strip_ext(doc.relpath).split("/")[-1]


def ingest_vault(
    settings: Settings,
    vault_path: str,
    *,
    reset: bool = False,
    embedder: Embedder | None = None,
    log: Logger | None = None,
    cache_dir: str | None = None,
    entity_llm: object | None = None,
    merge_decisions: dict | None = None,
) -> IngestStats:
    """Спроецировать vault в граф + индекс. Vault только читается.

    ``cache_dir`` (или ``settings.cache_dir``) включает семантический кэш: неизменённые
    заметки не перечанкуются и не переэмбеддятся (Eden AI не перебиллится).
    ``entity_llm`` (при ``settings.extract_entities``) включает извлечение свободных
    сущностей (люди/орги) из тел заметок с fuzzy-дедупом и рёбрами MENTIONS.
    ``merge_decisions`` (``{"approved": [(a,b)], "blocked": [frozenset({a,b})]}``) — решения
    арбитра: одобренные слияния применяются автоматически, отклонённые не предлагаются снова.
    """
    say: Logger = log or (lambda _m: None)
    embedder = embedder or build_embedder(settings)
    stats = IngestStats()

    # Whitelist слагов проектов из конфига (продукт-нейтральность; пусто = без project-узлов).
    project_values = settings.project_value_set()

    cache_root = cache_dir if cache_dir is not None else (settings.cache_dir or None)
    chunk_cache = ChunkCache(cache_root) if cache_root else None
    embed_cache = (
        EmbeddingCache(cache_root, model=settings.embedding_model, dim=settings.embedding_dim)
        if cache_root
        else None
    )

    say(f"Читаю vault (read-only): {vault_path}")
    docs = read_docs(vault_path)
    stats.files = len(docs)
    say(f"Найдено заметок: {len(docs)}")

    # === ПРОХОД 1: узлы ===
    nodes: list[Node] = []
    resolver = Resolver()
    project_nodes: dict[str, Node] = {}
    # Для прохода 2 храним связь doc -> nk и сами вопросы (с путём их реестра).
    doc_keys: dict[int, str] = {}
    question_rows: list[tuple[qmod.QuestionRow, str]] = []
    run_token = _run_token()

    for i, doc in enumerate(docs):
        entity_type = doc.frontmatter.get("entity_type")
        etype = entity_type if isinstance(entity_type, str) and entity_type else "entity"
        nk = _doc_natural_key(doc)
        doc_keys[i] = nk
        source_id = doc.frontmatter.get("id")
        source_id = source_id.strip() if isinstance(source_id, str) else None

        node = Node(
            type=etype,
            natural_key=nk,
            title=_doc_title(doc),
            properties=dict(doc.frontmatter),
            provenance=Provenance(
                source_path=doc.relpath,
                source_id=source_id,
                confidence=1.0,
                last_seen=run_token,
            ),
        )
        nodes.append(node)
        resolver.add_node(nk, source_id=source_id, relpath=doc.relpath)

        # Узлы-проекты из поля project.
        proj = doc.frontmatter.get("project")
        if isinstance(proj, str) and proj in project_values:
            pkey = f"project:{proj}"
            if pkey not in project_nodes:
                project_nodes[pkey] = Node(
                    type="project",
                    natural_key=pkey,
                    title=proj,
                    properties={"slug": proj},
                    provenance=Provenance(source_path=doc.relpath, last_seen=run_token),
                )
                resolver.add_node(pkey)

        # Реестр вопросов -> узлы question.
        if etype == "open_questions":
            rows = qmod.parse_questions(doc.body)
            for row in rows:
                qnode = Node(
                    type="question",
                    natural_key=row.qid,
                    title=row.text,
                    properties={"status": row.status},
                    provenance=Provenance(
                        source_path=doc.relpath,
                        source_id=row.qid,
                        confidence=1.0,
                        last_seen=run_token,
                    ),
                )
                nodes.append(qnode)
                resolver.add_node(row.qid, source_id=row.qid)
            question_rows.extend((row, doc.relpath) for row in rows)

    nodes.extend(project_nodes.values())

    say(
        f"Узлов к проекции: {len(nodes)} (проектов: {len(project_nodes)}, "
        f"вопросов: {len(question_rows)})"
    )

    # === Запись узлов в граф ===
    conn = dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name)
        index = VectorIndex(conn, settings.chunks_table, settings.embedding_dim)
        if reset:
            say("Сброс графа и индекса (--reset)")
            graph.reset()
            index.reset()
        graph.ensure_schema()
        index.ensure_schema()

        say("Проекция узлов…")
        graph.upsert_nodes(nodes)
        for node in nodes:
            stats.nodes_by_type[node.type] += 1

        # === Извлечение свободных сущностей (люди/орги) из тел заметок ===
        if settings.extract_entities and entity_llm is not None:
            from platform_memory.ingest.cache import ExtractionCache
            from platform_memory.ingest.entities import resolve_document_entities

            say("Извлечение сущностей из тел заметок (LLM)…")
            ext_cache = (
                ExtractionCache(cache_root, model=settings.llm_model) if cache_root else None
            )
            ent_docs = [
                (doc_keys[i], doc.relpath, doc.body)
                for i, doc in enumerate(docs)
                if doc.frontmatter.get("entity_type") != "open_questions"
            ]
            md = merge_decisions or {}
            ent_result = resolve_document_entities(
                ent_docs,
                llm=entity_llm,
                run_token=run_token,
                cache=ext_cache,
                forced_merges=md.get("approved"),
                blocked_pairs=set(md.get("blocked") or []),
            )
            graph.upsert_nodes(ent_result.nodes)
            for node in ent_result.nodes:
                stats.nodes_by_type[node.type] += 1
            ent_ok, _ = graph.upsert_edges(ent_result.edges, run_token)
            for edge in ent_result.edges:
                stats.edges_by_type[edge.type] += 1
            stats.entities_extracted = len(ent_result.nodes)
            stats.entity_mentions = ent_ok
            stats.entity_merge_candidates = len(ent_result.candidates)
            stats.merge_candidates = ent_result.candidates
            say(
                f"Сущностей: {stats.entities_extracted}, упоминаний: {stats.entity_mentions}, "
                f"кандидатов на арбитраж: {stats.entity_merge_candidates}"
            )

        # === ПРОХОД 2: рёбра ===
        edges: list[Edge] = []
        seen_edges: set[tuple[str, str, str]] = set()

        def add_edge(src: str, dst: str, etype: str, source_path: str) -> None:
            """Добавить ребро, пропуская пустые/петлевые и дедуплицируя по (src, dst, type)."""
            if not dst or src == dst:
                return
            key = (src, dst, etype)
            if key in seen_edges:
                return
            seen_edges.add(key)
            edges.append(Edge(type=etype, src=src, dst=dst, source_path=source_path))

        for i, doc in enumerate(docs):
            nk = doc_keys[i]
            fm = doc.frontmatter

            refs = parse_frontmatter_links(fm.get("links")) + extract_wikilinks(doc.body)
            for ref in refs:
                dst = resolver.resolve(ref, source_relpath=doc.relpath)
                if dst:
                    add_edge(nk, dst, "LINKS_TO", doc.relpath)
                else:
                    stats.edges_unresolved += 1
                    if len(stats.unresolved_samples) < 25:
                        stats.unresolved_samples.append(f"{nk} -> {ref.raw}")

            superseded = fm.get("superseded_by")
            if isinstance(superseded, str) and superseded.strip():
                ref = parse_frontmatter_link(superseded)
                dst = resolver.resolve(ref, source_relpath=doc.relpath) if ref else None
                if dst:
                    add_edge(nk, dst, "SUPERSEDED_BY", doc.relpath)
                else:
                    stats.edges_unresolved += 1
                    if len(stats.unresolved_samples) < 25:
                        stats.unresolved_samples.append(f"{nk} -superseded_by-> {superseded}")

            proj = fm.get("project")
            if isinstance(proj, str) and proj in project_values:
                add_edge(nk, f"project:{proj}", "PART_OF_PROJECT", doc.relpath)

        # Рёбра из строк реестра вопросов (относительные ссылки резолвим от пути их реестра).
        for row, reg_path in question_rows:
            for ref in row.links:
                dst = resolver.resolve(ref, source_relpath=reg_path)
                if dst:
                    add_edge(row.qid, dst, "LINKS_TO", reg_path)

        say(f"Рёбер к проекции: {len(edges)} (неразрешённых ссылок: {stats.edges_unresolved})")
        ok, skipped = graph.upsert_edges(edges, run_token)
        stats.edges_ok = ok
        for edge in edges:
            stats.edges_by_type[edge.type] += 1
        if skipped:
            say(f"⚠ Пропущено рёбер (узел-цель не найден в графе): {skipped}")

        # === Чанкинг + эмбеддинги + индекс ===
        say("Чанкинг тела заметок…")
        all_chunks: list[Chunk] = []

        for i, doc in enumerate(docs):
            if doc.frontmatter.get("entity_type") == "open_questions":
                continue  # тело — большая таблица; вопросы индексируются как Q-узлы
            nk = doc_keys[i]
            title = _doc_title(doc)
            # AST-аналог: чанки кэшируются по хэшу тела и версии чанкера.
            cached = chunk_cache.get_chunks(doc.body) if chunk_cache else None
            if cached is not None:
                tchunks = [
                    TextChunk(text=c["text"], heading=c.get("heading", ""), order=c.get("order", 0))
                    for c in cached
                ]
            else:
                tchunks = chunk_markdown(doc.body)
                if chunk_cache is not None:
                    chunk_cache.put_chunks(
                        doc.body,
                        [
                            {"text": tc.text, "heading": tc.heading, "order": tc.order}
                            for tc in tchunks
                        ],
                    )
            for tc in tchunks:
                all_chunks.append(
                    Chunk(
                        node_key=nk,
                        text=tc.text,
                        source_path=doc.relpath,
                        title=title,
                        heading=tc.heading,
                        order=tc.order,
                    )
                )
            if not tchunks:
                # Документ без чанкуемого тела (только заголовки/frontmatter) — fallback
                # по заголовку, чтобы узел оставался искабельным.
                all_chunks.append(
                    Chunk(
                        node_key=nk,
                        text=title,
                        source_path=doc.relpath,
                        title=title,
                        heading="",
                        order=0,
                    )
                )

        # По чанку на каждый вопрос (делает содержимое Q-NNN искабельным).
        for row, reg_path in question_rows:
            all_chunks.append(
                Chunk(
                    node_key=row.qid,
                    text=f"{row.qid}: {row.text}",
                    source_path=reg_path,
                    title=row.qid,
                    heading="Открытые вопросы",
                    order=0,
                )
            )

        say(f"Эмбеддинг {len(all_chunks)} чанков (провайдер: {settings.embedding_provider})…")
        if all_chunks:
            inputs = [c.embedding_input() for c in all_chunks]
            if embed_cache is not None:
                vectors, stats.embeddings_cached, stats.embeddings_computed = (
                    embed_cache.embed_with_cache(inputs, embedder.embed)
                )
                say(
                    f"Кэш эмбеддингов: {stats.embeddings_cached} из кэша, "
                    f"{stats.embeddings_computed} вычислено"
                )
            else:
                vectors = embedder.embed(inputs)
                stats.embeddings_computed = len(inputs)
            stats.chunks = index.add_chunks(all_chunks, vectors, seen_run=run_token)

        # === Mark-and-sweep: удалить всё, чего нет в текущем прогоне (самоочистка) ===
        stats.pruned_nodes, stats.pruned_edges = graph.prune(run_token)
        stats.pruned_chunks = index.prune_chunks(run_token)
        if stats.pruned_nodes or stats.pruned_edges or stats.pruned_chunks:
            say(
                f"Очищено устаревших: узлов {stats.pruned_nodes}, рёбер {stats.pruned_edges}, "
                f"чанков {stats.pruned_chunks}"
            )

        say("Готово.")
        return stats
    finally:
        conn.close()
