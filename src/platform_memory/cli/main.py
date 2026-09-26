"""CLI platform_memory: ingest / query / stats / init-db / communities.

NB: control plane (heartbeat ``tick``, арбитраж слияний ``merge-candidates``) НЕ входит
в platform_memory — он живёт в platform_orchestrator (M2).
"""

from __future__ import annotations

import sys

import typer

from platform_memory.core import db as dbmod
from platform_memory.core.config import get_settings
from platform_memory.graph import GraphStore, assign_communities
from platform_memory.index import VectorIndex
from platform_memory.ingest import ingest_vault
from platform_memory.retrieval import query as run_query

app = typer.Typer(
    add_completion=False,
    help="platform_memory — граф знаний компании + retrieval поверх Obsidian-vault.",
)


def _err(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


@app.command()
def init_db() -> None:
    """Создать граф AGE и таблицу чанков (идемпотентно)."""
    settings = get_settings()
    try:
        conn = dbmod.connect(settings)
    except Exception as exc:  # noqa: BLE001
        _err(f"Не удалось подключиться к БД: {exc}")
        raise typer.Exit(1) from exc
    try:
        GraphStore(conn, settings.graph_name).ensure_schema()
        VectorIndex(conn, settings.chunks_table, settings.embedding_dim).ensure_schema()
        # Context Memory Engine (ADR-016): observations + трейсы компиляций.
        from platform_memory.context.trace import ContextTraceStore
        from platform_memory.observations.store import ObservationStore

        ObservationStore(
            conn, settings.observations_table, settings.default_namespace
        ).ensure_schema()
        ContextTraceStore(
            conn, settings.context_traces_table, settings.default_namespace
        ).ensure_schema()
        # Доменные пакеты видов и журнал снимков источников (MEM-ADR-020).
        from platform_memory.domain.reconcile import ledger_for
        from platform_memory.domain.registry import open_registry

        open_registry(conn, settings).ensure_schema()
        ledger_for(settings, conn).ensure_schema()
        typer.secho(
            f"OK: граф '{settings.graph_name}', таблицы '{settings.chunks_table}' "
            f"(dim={settings.embedding_dim}), '{settings.observations_table}' и "
            f"'{settings.context_traces_table}' готовы.",
            fg=typer.colors.GREEN,
        )
    finally:
        conn.close()


@app.command()
def ingest(
    vault: str | None = typer.Option(None, "--vault", help="Путь к Obsidian-vault (read-only)."),
    reset: bool = typer.Option(False, "--reset", help="Пересоздать граф и индекс перед ingest."),
    extract_entities: bool = typer.Option(
        False,
        "--extract-entities",
        help="Извлекать свободные сущности (люди/орги) из тел заметок через LLM.",
    ),
) -> None:
    """Спроецировать vault в граф + векторный индекс. В vault ничего не пишется."""
    settings = get_settings()
    vault_path = vault or settings.vault_path
    if not vault_path:
        _err("Не задан путь к vault. Используйте --vault PATH или CB_VAULT_PATH в .env.")
        raise typer.Exit(2)

    # Экстрактор сущностей включается флагом ИЛИ настройкой; LLM собираем из конфига.
    want_entities = extract_entities or settings.extract_entities
    entity_llm = None
    # Арбитраж спорных слияний (merge_decisions/create_merge_candidate) живёт в control
    # plane → platform_orchestrator (M2). Здесь движок памяти предлагает кандидатов, но не
    # персистит их в approvals; merge_decisions=None — без авто-применения прошлых решений.
    if want_entities:
        from platform_memory.retrieval.llm import build_llm

        settings = settings.model_copy_with(extract_entities=True)
        entity_llm = build_llm(settings)

    try:
        stats = ingest_vault(
            settings,
            vault_path,
            reset=reset,
            log=typer.echo,
            entity_llm=entity_llm,
            merge_decisions=None,
        )
    except Exception as exc:  # noqa: BLE001
        _err(f"Ошибка ingest: {exc}")
        raise typer.Exit(1) from exc

    # Спорные слияния только репортим (персист в approvals — M2/platform_orchestrator).
    if want_entities and stats.merge_candidates:
        typer.echo(
            f"Кандидатов на арбитраж слияния (не персистятся в Фазе 1): "
            f"{len(stats.merge_candidates)}"
        )

    typer.secho("\n=== Итог ingest ===", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Заметок обработано : {stats.files}")
    typer.echo(f"Узлов             : {stats.nodes_total}")
    typer.echo(f"Рёбер             : {stats.edges_total} (записано: {stats.edges_ok})")
    typer.echo(f"Чанков            : {stats.chunks}")
    typer.echo(f"Неразрешённых ссылок: {stats.edges_unresolved}")
    if want_entities:
        typer.echo(
            f"Сущностей (люди/орги): {stats.entities_extracted} "
            f"(упоминаний: {stats.entity_mentions}, "
            f"на арбитраж: {stats.entity_merge_candidates})"
        )
    typer.echo("\nУзлы по типам:")
    for typ, cnt in sorted(stats.nodes_by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        typer.echo(f"  {typ:<16} {cnt}")
    typer.echo("\nРёбра по типам:")
    for typ, cnt in sorted(stats.edges_by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        typer.echo(f"  {typ:<16} {cnt}")


@app.command()
def communities(
    resolution: float = typer.Option(
        1.0, "--resolution", help=">1 — мельче сообщества, <1 — крупнее."
    ),
    label: bool = typer.Option(
        False, "--label", help="Проставить LLM-метки сообществ (community_name)."
    ),
) -> None:
    """Разметить сообщества графа (Leiden/Louvain) и записать их на узлы AGE."""
    settings = get_settings()
    summary_llm = None
    if label:
        from platform_memory.retrieval.llm import build_llm

        summary_llm = build_llm(settings)

    conn = dbmod.connect(settings)
    try:
        store = GraphStore(conn, settings.graph_name)
        res = assign_communities(store, resolution=resolution, llm=summary_llm)
    except Exception as exc:  # noqa: BLE001
        _err(f"Ошибка разметки сообществ: {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    typer.secho("\n=== Сообщества ===", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Сообществ        : {res.community_count}")
    typer.echo(f"Узлов размечено  : {res.nodes_assigned}")
    typer.echo(f"С LLM-меткой      : {res.labeled}")
    if res.names:
        typer.echo("\nМетки:")
        for cid, name in sorted(res.names.items()):
            typer.echo(f"  #{cid:<3} {name}")


@app.command()
def query(
    question: str = typer.Argument(..., help="Вопрос на естественном языке."),
    k: int = typer.Option(8, "-k", "--top-k", help="Сколько чанков извлекать."),
    hops: int = typer.Option(1, "--hops", help="Глубина расширения по графу (1–2)."),
    show_context: bool = typer.Option(False, "--context", help="Показать собранный контекст."),
) -> None:
    """Задать вопрос: вектор top-k + расширение по графу -> ответ с цитатами."""
    settings = get_settings()
    try:
        answer = run_query(settings, question, k=k, hops=hops)
    except Exception as exc:  # noqa: BLE001
        _err(f"Ошибка query: {exc}")
        raise typer.Exit(1) from exc

    typer.secho("\n=== Ответ ===", fg=typer.colors.CYAN, bold=True)
    typer.echo(answer.text)

    typer.secho("\n=== Источники ===", fg=typer.colors.CYAN, bold=True)
    if not answer.sources:
        typer.echo("(нет)")
    for s in answer.sources:
        nk = f" [{s.node_key}]" if s.node_key else ""
        typer.echo(f"  • {s.source_path}{nk}  {s.title}")

    typer.secho(
        f"\n(найдено фрагментов: {answer.hits}, соседей по графу: {answer.neighbors})",
        fg=typer.colors.BRIGHT_BLACK,
    )
    if show_context:
        typer.secho("\n=== Контекст ===", fg=typer.colors.CYAN, bold=True)
        typer.echo(answer.context)


@app.command()
def stats() -> None:
    """Статистика графа: узлы по типам, рёбра по типам, число чанков."""
    settings = get_settings()
    try:
        conn = dbmod.connect(settings)
    except Exception as exc:  # noqa: BLE001
        _err(f"Не удалось подключиться к БД: {exc}")
        raise typer.Exit(1) from exc
    try:
        graph = GraphStore(conn, settings.graph_name)
        index = VectorIndex(conn, settings.chunks_table, settings.embedding_dim)
        nodes = graph.count_nodes_by_type()
        edges = graph.count_edges_by_type()
        chunks = index.count_chunks() if index.table_exists() else 0
    finally:
        conn.close()

    typer.secho("=== Узлы по типам ===", fg=typer.colors.CYAN, bold=True)
    for typ, cnt in sorted(nodes.items(), key=lambda kv: (-kv[1], kv[0])):
        typer.echo(f"  {typ:<16} {cnt}")
    typer.echo(f"  {'ИТОГО':<16} {sum(nodes.values())}")

    typer.secho("\n=== Рёбра по типам ===", fg=typer.colors.CYAN, bold=True)
    for typ, cnt in sorted(edges.items(), key=lambda kv: (-kv[1], kv[0])):
        typer.echo(f"  {typ:<16} {cnt}")
    typer.echo(f"  {'ИТОГО':<16} {sum(edges.values())}")

    typer.secho(f"\nЧанков в индексе: {chunks}", fg=typer.colors.GREEN)


# --- Context Memory Engine (ADR-016) ---


@app.command()
def observe(
    payload: str = typer.Argument(
        ..., help='Observation как JSON: {"kind": …, "content": …, "source": {…}, …}'
    ),
    namespace: str = typer.Option("", "--namespace", help="KB записи (пусто — дефолтная)."),
) -> None:
    """Принять одно наблюдение (идемпотентно по source identity)."""
    from platform_memory.observations.ingest import retain_observation
    from platform_memory.observations.store import observation_from_json

    settings = get_settings()
    try:
        obs = observation_from_json(payload, namespace=namespace)
        result = retain_observation(settings, obs, namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        _err(f"Ошибка observe: {exc}")
        raise typer.Exit(1) from exc
    dup = " (дубликат)" if result["duplicate"] else ""
    typer.secho(
        f"OK: {result['observation_id']} status={result['status']}{dup}", fg=typer.colors.GREEN
    )


@app.command()
def observations(
    namespace: str = typer.Option("", "--namespace", help="KB (пусто — дефолтная)."),
    kind: str = typer.Option("", "--kind", help="Фильтр по kind."),
    status: str = typer.Option("", "--status", help="Фильтр по статусу обработки."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Свежие наблюдения и свод статусов обработки (аудит ingestion state)."""
    from platform_memory.observations.store import ObservationStore

    settings = get_settings()
    try:
        conn = dbmod.connect(settings)
    except Exception as exc:  # noqa: BLE001
        _err(f"Не удалось подключиться к БД: {exc}")
        raise typer.Exit(1) from exc
    try:
        store = ObservationStore(conn, settings.observations_table, settings.default_namespace)
        if not store.table_exists():
            typer.echo("Таблица наблюдений ещё не создана (cb init-db).")
            raise typer.Exit(0)
        nss = [namespace] if namespace else []
        records = store.list_recent(nss, kinds=[kind] if kind else (), status=status, limit=limit)
        statuses = store.counts_by_status(nss)
    finally:
        conn.close()
    typer.secho("=== Статусы обработки ===", fg=typer.colors.CYAN, bold=True)
    for st, cnt in sorted(statuses.items()):
        typer.echo(f"  {st:<22} {cnt}")
    typer.secho("\n=== Свежие наблюдения ===", fg=typer.colors.CYAN, bold=True)
    for rec in records:
        head = rec.occurred_at or rec.ingested_at[:19]
        typer.echo(f"  {rec.observation_id}  [{rec.kind or '-'}] {head}  {rec.content[:60]!r}")


@app.command()
def context(
    query_text: str = typer.Argument(..., help="Запрос для компиляции контекста."),
    namespace: str = typer.Option("", "--namespace", help="KB (пусто — дефолтная)."),
    scope: list[str] = typer.Option([], "--scope", help="Scope-фильтр 'type:id' (повторяемо)."),
    anchor: list[str] = typer.Option([], "--anchor", help="Якорь graph expansion (повторяемо)."),
    max_tokens: int = typer.Option(0, "--max-tokens", help="Бюджет (0 — дефолт конфига)."),
    as_json: bool = typer.Option(False, "--json", help="Вывести ContextPack как JSON."),
) -> None:
    """Собрать bounded ContextPack (без LLM-синтеза) — отладка retrieval."""
    import json as jsonlib

    from platform_memory.context import build_context

    settings = get_settings()
    try:
        pack = build_context(
            settings,
            {
                "query": query_text,
                "scopes": scope,
                "anchors": anchor,
                "max_tokens": max_tokens,
                "namespaces": [namespace] if namespace else [],
            },
        )
    except Exception as exc:  # noqa: BLE001
        _err(f"Ошибка context: {exc}")
        raise typer.Exit(1) from exc
    if as_json:
        typer.echo(jsonlib.dumps(pack.to_payload(), ensure_ascii=False, indent=1))
        return
    typer.echo(pack.to_text())
    typer.secho(
        f"\n(≈{pack.token_estimate} токенов; кандидатов {pack.stats.get('candidates')}, "
        f"отброшено {len(pack.budget.get('dropped', []))}; trace {pack.trace_id})",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def trace(
    trace_id: str = typer.Argument(..., help="Идентификатор трейса компиляции (ctx-…)."),
    namespace: str = typer.Option("", "--namespace", help="KB (пусто — дефолтная)."),
) -> None:
    """Показать трейс компиляции контекста: каналы, ranking, budget-решения."""
    import json as jsonlib

    from platform_memory.context.trace import ContextTraceStore

    settings = get_settings()
    try:
        conn = dbmod.connect(settings)
    except Exception as exc:  # noqa: BLE001
        _err(f"Не удалось подключиться к БД: {exc}")
        raise typer.Exit(1) from exc
    try:
        store = ContextTraceStore(conn, settings.context_traces_table, settings.default_namespace)
        found = (
            store.get(trace_id, namespaces=[namespace] if namespace else [])
            if store.table_exists()
            else None
        )
    finally:
        conn.close()
    if found is None:
        _err(f"Трейс не найден: {trace_id}")
        raise typer.Exit(1)
    typer.echo(jsonlib.dumps(found, ensure_ascii=False, indent=1))


@app.command()
def consolidate(
    namespace: str = typer.Option("", "--namespace", help="KB (пусто — дефолтная)."),
    limit: int = typer.Option(500, "--limit", help="Максимум наблюдений за прогон."),
) -> None:
    """Redrive необработанных наблюдений (идемпотентно)."""
    from platform_memory.observations.consolidate import consolidate as run_consolidate

    settings = get_settings()
    try:
        report = run_consolidate(settings, namespace=namespace, limit=limit)
    except Exception as exc:  # noqa: BLE001
        _err(f"Ошибка consolidate: {exc}")
        raise typer.Exit(1) from exc
    typer.secho(
        f"OK: обработано {report['processed']}, статусы {report['statuses']}",
        fg=typer.colors.GREEN,
    )


def main() -> None:
    """Точка входа CLI: запустить Typer-приложение."""
    app()


if __name__ == "__main__":
    sys.exit(app())
