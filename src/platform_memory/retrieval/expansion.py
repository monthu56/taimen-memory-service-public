# Адаптировано из graphify/serve.py (https://github.com/safishamsi/graphify), MIT License.
# Copyright (c) 2026 Safi Shamsi. See THIRD_PARTY.md for the full license text.
#
# Перенесены: _pick_seeds (порог разрыва скоров), _bfs/_dfs (обход по глубине с throttling
# хабов), _subgraph_to_text (сериализация подграфа под токен-бюджет с цитатами на source_file).
# НЕ перенесено: лексический IDF-скоринг узлов (_query_terms/_score_nodes) и код-специфичные
# context-фильтры — у нас seed-узлы даёт pgvector (vector-first), а не лексика.
# Санитизация полей при сериализации идёт через нашу границу доверия (llm_safety).
"""Расширение по графу и сериализация подграфа под токен-бюджет (для retrieval).

Seed-узлы приходят из векторного поиска (pgvector). ``pick_seeds`` отсекает «шумные»
seed'ы по разрыву скоров; ``bfs``/``dfs`` расширяют от seed'ов на заданную глубину с
троттлингом хабов; ``subgraph_to_text`` рендерит результат под бюджет токенов с цитатами
на файлы-источники. Поля узлов/рёбер чистятся через [[llm_safety]] перед склейкой в контекст.
"""

from __future__ import annotations

import networkx as nx

from platform_memory.core.llm_safety import neutralize_prompt_injection, sanitize_label


def pick_seeds(
    scored: list[tuple[float, str]],
    max_k: int = 3,
    gap_ratio: float = 0.2,
) -> list[str]:
    """Выбрать seed-узлы, останавливаясь, когда скор падает слишком сильно ниже топа.

    ``scored`` — пары (скор, node_key), отсортированные по убыванию (как выдаёт pgvector).
    Не даёт слабым попаданиям красть seed-слоты у доминирующего: если топ = 0.9, а
    следующий = 0.1, при gap_ratio=0.2 второй отсекается (0.1 < 0.9*0.2).
    """
    if not scored:
        return []
    top_score = scored[0][0]
    seeds: list[str] = []
    for score, nid in scored[:max_k]:
        if seeds and score < top_score * gap_ratio:
            break
        seeds.append(nid)
    return seeds


def _hub_threshold(G: nx.Graph) -> int:
    """Порог степени хаба: p99 распределения степеней, но не ниже 50."""
    degrees = [G.degree(n) for n in G.nodes()]
    if not degrees:
        return 50
    degrees_sorted = sorted(degrees)
    p99_idx = int(len(degrees_sorted) * 0.99)
    return max(50, degrees_sorted[min(p99_idx, len(degrees_sorted) - 1)])


def bfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    """Обход в ширину от seed'ов на ``depth`` шагов; хабы (высокая степень) не транзитятся."""
    hub_threshold = _hub_threshold(G)
    seed_set = set(start_nodes)
    visited: set[str] = set(start_nodes)
    frontier = set(start_nodes)
    edges_seen: list[tuple] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for n in frontier:
            if n not in seed_set and G.degree(n) >= hub_threshold:
                continue  # не расширяемся через хаб (кроме seed'а)
            for neighbor in G.neighbors(n):
                if neighbor not in visited:
                    next_frontier.add(neighbor)
                    edges_seen.append((n, neighbor))
        visited.update(next_frontier)
        frontier = next_frontier
    return visited, edges_seen


def dfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    """Обход в глубину от seed'ов до глубины ``depth``; хабы не транзитятся."""
    hub_threshold = _hub_threshold(G)
    seed_set = set(start_nodes)
    visited: set[str] = set()
    edges_seen: list[tuple] = []
    stack = [(n, 0) for n in reversed(start_nodes)]
    while stack:
        node, d = stack.pop()
        if node in visited or d > depth:
            continue
        visited.add(node)
        if node not in seed_set and G.degree(node) >= hub_threshold:
            continue
        for neighbor in G.neighbors(node):
            if neighbor not in visited:
                stack.append((neighbor, d + 1))
                edges_seen.append((node, neighbor))
    return visited, edges_seen


def subgraph_to_text(
    G: nx.Graph,
    nodes: set[str],
    edges: list[tuple],
    token_budget: int = 2000,
    *,
    seeds: list[str] | None = None,
) -> str:
    """Сериализовать подграф в текст, обрезая по бюджету токенов (~3 символа/токен).

    seed-узлы рендерятся первыми (queried-сущности наверху), далее — по убыванию степени.
    Каждое поле проходит через sanitize_label + neutralize_prompt_injection: узлы могут
    нести недоверенный текст (письма/транскрипты), который иначе протащил бы ANSI/инъекцию
    в контекст модели. Цитаты на source_file сохраняются.
    """
    char_budget = token_budget * 3
    seed_set = set(seeds or [])
    ordered = [n for n in (seeds or []) if n in nodes] + sorted(
        nodes - seed_set, key=lambda n: G.degree(n), reverse=True
    )

    def clean(value: object) -> str:
        return neutralize_prompt_injection(sanitize_label(str(value)))

    lines: list[str] = []
    for nid in ordered:
        d = G.nodes[nid]
        src = clean(d.get("source_file", ""))
        community = clean(str(d.get("community_name") or d.get("community", "")))
        lines.append(
            f"NODE [{clean(d.get('type', 'entity'))}] {clean(d.get('label', nid))} "
            f"(ключ={clean(nid)}) [источник={src} community={community}]"
        )
    for u, v in edges:
        if u in nodes and v in nodes:
            d = G.get_edge_data(u, v) or {}
            lines.append(
                f"EDGE {clean(G.nodes[u].get('label', u))} "
                f"--{clean(d.get('relation', ''))}--> "
                f"{clean(G.nodes[v].get('label', v))}"
            )

    output = "\n".join(lines)
    if len(output) > char_budget:
        cut_at = output[:char_budget].rfind("\n")
        cut_at = cut_at if cut_at > 0 else char_budget
        total_nodes = sum(1 for ln in lines if ln.startswith("NODE "))
        shown_nodes = output[:cut_at].count("\nNODE ") + (1 if output.startswith("NODE ") else 0)
        cut_count = total_nodes - shown_nodes
        output = (
            output[:cut_at]
            + f"\n… (обрезано — ещё {cut_count} узлов отсечено бюджетом ~{token_budget} токенов; "
            f"сузьте запрос или используйте get_node по конкретной сущности)"
        )
    return output


def expand_subgraph(
    G: nx.Graph,
    seeds: list[str],
    *,
    mode: str = "bfs",
    depth: int = 2,
    token_budget: int = 2000,
) -> tuple[set[str], list[tuple], str]:
    """Расширить подграф от seed'ов и сериализовать его. Возвращает (узлы, рёбра, текст).

    Seed'ы, отсутствующие в графе, отбрасываются. Пустой набор seed'ов → пустой результат.
    """
    present = [s for s in seeds if s in G]
    if not present:
        return set(), [], ""
    nodes, edges = dfs(G, present, depth) if mode == "dfs" else bfs(G, present, depth)
    header = (
        f"Обход: {mode.upper()} глубина={depth} | "
        f"seed: {[G.nodes[n].get('label', n) for n in present]} | "
        f"узлов: {len(nodes)}\n\n"
    )
    return nodes, edges, header + subgraph_to_text(G, nodes, edges, token_budget, seeds=present)
