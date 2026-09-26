"""Модели данных Company Brain: узлы, рёбра, чанки и происхождение (provenance)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Provenance:
    """Происхождение факта: откуда взят, насколько уверены, когда видели последний раз."""

    source_path: str  # относительный путь к файлу-источнику в vault
    source_id: str | None = None  # natural id из frontmatter (BIZ-/T-/M-/Q-), если есть
    confidence: float = 1.0  # 1.0 — спроецировано напрямую из структурированного источника
    last_seen: str | None = None  # ISO-дата последнего прогона ingest


@dataclass(slots=True)
class Node:
    """Узел графа: типизированная сущность с natural_key, заголовком, свойствами и provenance."""

    type: str  # entity_type (decision, task, ...) или синтетический (question, project)
    natural_key: str  # стабильный ключ: BIZ-010 / T-001 / относительный путь / project:...
    title: str
    properties: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None
    origin: str = "vault"  # "vault" (проекция из vault) | "agent" (write-back агента)
    namespace: str = ""  # единица изоляции (ADR-054); пусто -> default стора


@dataclass(slots=True)
class Edge:
    """Ребро графа между двумя natural_key."""

    type: str  # LINKS_TO, SUPERSEDED_BY, PART_OF_PROJECT, ...
    src: str  # natural_key источника
    dst: str  # natural_key цели
    properties: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None  # где объявлена связь
    origin: str = "vault"  # "vault" | "agent"
    namespace: str = ""  # namespace ОБОИХ концов ребра (v1: рёбра не пересекают namespaces)


@dataclass(slots=True)
class Chunk:
    """Текстовый фрагмент тела документа для векторного индекса."""

    node_key: str  # natural_key узла-владельца
    text: str  # отображаемый текст фрагмента
    source_path: str  # путь к файлу-источнику (для цитат)
    title: str = ""  # заголовок узла (для контекста цитаты)
    heading: str = ""  # путь по заголовкам внутри документа
    order: int = 0  # порядковый номер фрагмента в документе
    namespace: str = ""  # единица изоляции (ADR-054); пусто -> default стора
    meta: dict[str, Any] = field(default_factory=dict)  # фильтруемые теги (например, collection)

    def embedding_input(self) -> str:
        """Текст, который реально уходит в эмбеддинг (обогащённый заголовком/секцией)."""
        header = " — ".join(p for p in (self.title, self.heading) if p)
        return f"{header}\n{self.text}" if header else self.text
