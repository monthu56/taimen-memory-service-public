"""Модели Context Compiler: ContextRequest -> ContextPack (ADR-016 §18–19).

ContextPack — структурированный, machine-readable результат с provenance у
каждого значимого элемента; потребитель сам решает, как превратить его в
промпт (``to_text()`` — готовый рендер по умолчанию).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Секции пака (порядок = порядок в выдаче и приоритет бюджета).
SECTION_CURRENT = "current"
SECTION_FACTS = "relevant_facts"
SECTION_EPISODES = "episodes"
SECTION_DOCUMENTS = "documents"
SECTION_ENTITIES = "related_entities"
SECTION_OBSERVATIONS = "recent_observations"
SECTION_ORDER = (
    SECTION_CURRENT,
    SECTION_FACTS,
    SECTION_EPISODES,
    SECTION_DOCUMENTS,
    SECTION_ENTITIES,
    SECTION_OBSERVATIONS,
)

# Retrieval-стратегии (§17): advanced-подсказка; дефолт выбирает компилятор.
STRATEGIES = frozenset({"semantic", "exact", "graph", "hybrid", "context", "briefing"})


def _optional_list(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    return [str(x) for x in raw]


@dataclass(slots=True)
class ContextRequest:
    """Запрос на компиляцию контекста.

    ``ephemeral_context`` — authoritative-состояние вызывающего приложения:
    попадает в секцию ``current`` этой компиляции и НЕ сохраняется движком.
    """

    query: str = ""
    subject: dict[str, str] | None = None  # {type, id} — за кого собираем контекст
    scopes: list[Any] = field(default_factory=list)  # "type:id" | {type,id}
    anchors: list[Any] = field(default_factory=list)  # сущности-якоря graph expansion
    ephemeral_context: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 0  # 0 -> CB_CONTEXT_DEFAULT_MAX_TOKENS
    strategy: str = ""  # "" -> hybrid (выбор компилятора)
    namespaces: list[str] = field(default_factory=list)
    as_of: str = ""  # история на момент времени (ISO-8601 UTC); пусто -> сейчас
    k: int = 8  # целевой размер каждого retrieval-канала
    # Разрешённые scopes видимости вызывающего (MEM-ADR-019): None — без ограничения
    # (локальный режим, статический ключ); задаёт HTTP-слой, не клиент.
    allowed_scopes: list[str] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ContextRequest:
        """Собрать запрос из входного JSON (HTTP/MCP/CLI), camelCase поддержан."""
        budget = payload.get("budget") or {}
        max_tokens = int(
            payload.get("max_tokens", payload.get("maxTokens", budget.get("tokens", 0))) or 0
        )
        strategy = str(payload.get("strategy", "") or "")
        if strategy and strategy not in STRATEGIES:
            raise ValueError(f"Неизвестная strategy {strategy!r}: {sorted(STRATEGIES)}")
        return cls(
            query=str(payload.get("query", "") or ""),
            subject=payload.get("subject"),
            scopes=list(payload.get("scopes") or []),
            anchors=list(payload.get("anchors") or []),
            ephemeral_context=dict(
                payload.get("ephemeral_context", payload.get("ephemeralContext", {})) or {}
            ),
            max_tokens=max_tokens,
            strategy=strategy,
            namespaces=list(payload.get("namespaces") or []),
            as_of=str(payload.get("as_of", payload.get("asOf", "")) or ""),
            k=int(payload.get("k", 8) or 8),
            allowed_scopes=_optional_list(
                payload.get("allowed_scopes", payload.get("allowedScopes"))
            ),
        )


@dataclass(slots=True)
class ContextItem:
    """Единица контекста с provenance и объяснимыми сигналами ранжирования."""

    kind: str  # fact | episode | document | entity | observation | current
    id: str  # стабильный id: chunk:<n> | fact-… | obs-… | node:<natural_key>
    text: str = ""
    title: str = ""
    source_path: str = ""
    score: float = 0.0
    signals: dict[str, Any] = field(default_factory=dict)  # почему включён (§23, §25)
    provenance: dict[str, Any] = field(default_factory=dict)  # цепочка до источника
    conflict: bool = False
    token_estimate: int = 0

    def to_payload(self) -> dict[str, Any]:
        """JSON-представление для API."""
        out: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "text": self.text,
            "title": self.title,
            "source_path": self.source_path,
            "score": round(self.score, 6),
            "signals": self.signals,
            "provenance": self.provenance,
            "token_estimate": self.token_estimate,
        }
        if self.conflict:
            out["conflict"] = True
        return out


@dataclass(slots=True)
class ContextSection:
    """Логическая секция пака."""

    kind: str
    items: list[ContextItem] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "items": [i.to_payload() for i in self.items]}


@dataclass(slots=True)
class ContextPack:
    """Итог компиляции: детерминированный, бюджетированный, с trace."""

    query: str = ""
    sections: list[ContextSection] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    token_estimate: int = 0
    budget: dict[str, Any] = field(default_factory=dict)  # {max_tokens, dropped, truncated}
    conflicts: list[str] = field(default_factory=list)  # id конфликтующих фактов
    trace_id: str = ""
    stats: dict[str, Any] = field(default_factory=dict)  # счётчики каналов

    def to_payload(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "sections": [s.to_payload() for s in self.sections],
            "sources": self.sources,
            "token_estimate": self.token_estimate,
            "budget": self.budget,
            "conflicts": self.conflicts,
            "trace_id": self.trace_id,
            "stats": self.stats,
        }

    def to_text(self) -> str:
        """Рендер по умолчанию для прямой вставки в LLM-контекст."""
        titles = {
            SECTION_CURRENT: "Текущее состояние (от вызывающего приложения)",
            SECTION_FACTS: "Релевантные факты",
            SECTION_EPISODES: "Эпизоды",
            SECTION_DOCUMENTS: "Документы",
            SECTION_ENTITIES: "Связанные сущности",
            SECTION_OBSERVATIONS: "Свежие наблюдения",
        }
        parts: list[str] = []
        for section in self.sections:
            if not section.items:
                continue
            parts.append(f"# {titles.get(section.kind, section.kind)}\n")
            for item in section.items:
                head = f"## [{item.id}]" + (f" {item.title}" if item.title else "")
                if item.conflict:
                    head += " ⚠ конфликтующие версии"
                body = item.text
                src = f"Источник: {item.source_path}" if item.source_path else ""
                parts.append("\n".join(p for p in (head, src, "", body, "") if p is not None))
        return "\n".join(parts)
