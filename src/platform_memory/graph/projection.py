"""Проекция AGE-графа Company Brain в NetworkX для кластеризации и анализа.

cluster.py и analyze.py (вендорено из graphify) работают над ``networkx``-графом. Этот
модуль строит такую проекцию из нашего AGE-store: id узла = natural_key, атрибуты
``label`` (title), ``type``, ``source_file`` (source_path). Рёбра направленные
(тип связи в ``relation``). NetworkX-граф — эфемерная проекция только для чтения;
источник истины остаётся в AGE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from collections.abc import Sequence

    from platform_memory.graph.store import GraphStore


def to_networkx(
    store: GraphStore,
    *,
    directed: bool = True,
    namespaces: Sequence[str] | None = None,
) -> nx.DiGraph | nx.Graph:
    """Спроецировать граф из AGE в NetworkX (весь или в пределах namespaces).

    Атрибуты узла подгоняются под ожидания vendored-кода graphify (``label``,
    ``source_file``), плюс ``type`` и сырые ``props``. Рёбра несут ``relation``
    (тип ребра) и ``source_path``. ``namespaces=None`` — весь граф (offline-анализ).
    """
    G: nx.DiGraph | nx.Graph = nx.DiGraph() if directed else nx.Graph()

    for props in store.all_nodes(namespaces=namespaces):
        nk = props.get("natural_key")
        if not nk:
            continue
        attrs = {
            "label": props.get("title") or str(nk),
            "type": props.get("type") or "entity",
            "source_file": props.get("source_path") or "",
            "origin": props.get("origin") or "vault",
        }
        # Разметка сообществ (Phase 3), если уже проставлена — переносим в проекцию,
        # чтобы subgraph_to_text/analyze показывали тему группы.
        if isinstance(props.get("community"), int):
            attrs["community"] = props["community"]
        if props.get("community_name"):
            attrs["community_name"] = props["community_name"]
        G.add_node(str(nk), **attrs)

    for edge in store.all_edges(namespaces=namespaces):
        src, dst = edge.get("src"), edge.get("dst")
        if not src or not dst:
            continue
        src, dst = str(src), str(dst)
        # Рёбра могут ссылаться на узлы, не попавшие в выборку узлов — добавим заглушки,
        # чтобы networkx не уронил обход.
        if src not in G:
            G.add_node(src, label=src, type="entity", source_file="", origin="vault")
        if dst not in G:
            G.add_node(dst, label=dst, type="entity", source_file="", origin="vault")
        G.add_edge(
            src,
            dst,
            relation=edge.get("type") or "",
            source_file=edge.get("source_path") or "",
        )

    return G
