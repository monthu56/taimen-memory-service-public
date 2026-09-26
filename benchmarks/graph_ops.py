"""GraphBenchOps — seam для сравнения графовых движков (ADR-016 §10, ТЗ §43).

Протокол фиксирует операции, по которым будущий альтернативный движок
сравнивается с AGE БЕЗ изменения Context API: реализуйте протокол для
кандидата и прогоните тот же bench.py. Это НЕ runtime-абстракция хранилища
(ADR-015 закрыл premature abstraction) — только интерфейс бенчмарка.
"""

from __future__ import annotations

from typing import Any, Protocol

from platform_memory.core.config import Settings
from platform_memory.graph.facts import FactStore
from platform_memory.graph.store import GraphStore


class GraphBenchOps(Protocol):
    """Операции, по которым сравниваются графовые движки."""

    def entity_lookup(self, natural_key: str) -> dict[str, Any] | None: ...

    def one_hop(self, natural_key: str) -> list[dict[str, Any]]: ...

    def two_hop(self, natural_key: str) -> list[dict[str, Any]]: ...

    def neighborhood(self, keys: list[str], limit: int) -> list[dict[str, Any]]: ...

    def temporal_filter(self, subject: str, as_of: str) -> list[dict[str, Any]]: ...

    def shortest_path(self, src: str, dst: str) -> list[str]: ...


class AgeBenchOps:
    """Реализация GraphBenchOps поверх текущего движка (AGE)."""

    def __init__(self, settings: Settings, conn):
        self.graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        self.facts = FactStore(self.graph)

    def entity_lookup(self, natural_key: str) -> dict[str, Any] | None:
        return self.graph.get_node(natural_key)

    def one_hop(self, natural_key: str) -> list[dict[str, Any]]:
        return self.graph.expand_neighbors([natural_key], hops=1)

    def two_hop(self, natural_key: str) -> list[dict[str, Any]]:
        return self.graph.expand_neighbors([natural_key], hops=2)

    def neighborhood(self, keys: list[str], limit: int) -> list[dict[str, Any]]:
        return self.graph.expand_neighbors(keys, hops=1, limit=limit)

    def temporal_filter(self, subject: str, as_of: str) -> list[dict[str, Any]]:
        return self.facts.facts(subject=subject, as_of=as_of, include_closed=True)

    def shortest_path(self, src: str, dst: str) -> list[str]:
        import networkx as nx

        from platform_memory.graph import to_networkx

        G = to_networkx(self.graph).to_undirected(as_view=True)
        try:
            return nx.shortest_path(G, src, dst)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []
