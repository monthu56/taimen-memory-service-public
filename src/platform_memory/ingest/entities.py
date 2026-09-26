"""Извлечение свободных сущностей (люди/орги) из тел заметок и их fuzzy-резолюция.

Замыкает [[fuzzy_resolver]] (Фаза A) и [[llm_safety]] (Фаза B) в живой пайплайн:
тело заметки → LLM-экстрактор → недоверенный JSON проходит границу доверия
(``validate_llm_fragment`` → ``sanitize_llm_fragment``) → ``Node`` (person/org) →
``resolve_entities`` дедуплицирует «Иван Петров»/«И. Петров» из разных источников.

Узлы-документы (проекции vault) сюда не подаются — у них уже есть авторитетный natural_key.
Свободные сущности линкуются к заметке-источнику ребром ``MENTIONS`` и переживают
дедуп с приоритетом natural-key (ИНН/email). Спорные слияния отдаются на арбитраж.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from platform_memory.core.llm_safety import (
    neutralize_prompt_injection,
    sanitize_llm_fragment,
    validate_llm_fragment,
)
from platform_memory.core.models import Edge, Node, Provenance
from platform_memory.ingest.cache import ExtractionCache
from platform_memory.ingest.fuzzy_resolver import (
    MergeCandidate,
    apply_merges,
    resolve_entities,
)


class EntityLLM(Protocol):
    """Минимальный контракт LLM для экстрактора (совпадает с retrieval.LLM)."""

    def answer(self, system: str, user: str) -> str:
        """Сгенерировать ответ по system/user-сообщениям."""
        ...


_SYSTEM = (
    "Ты извлекаешь ИМЕНОВАННЫЕ сущности (людей и организации) из текста на русском или "
    "английском. Верни СТРОГО JSON-объект без пояснений:\n"
    '{"nodes": [{"id": "<slug>", "label": "<имя>", "type": "person|org", '
    '"email": "<если есть>", "inn": "<если есть>"}], "edges": []}\n'
    "Правила: только реальные именованные люди/организации (не понятия, не предложения, "
    "не должности без имени). id — короткий латинский slug без пробелов и '/'. "
    'Если сущностей нет — верни {"nodes": [], "edges": []}.'
)

_ENTITY_TYPES = {"person", "org"}
# Свойства, которые экстрактор может выдать как авторитетную идентичность сущности.
_IDENTITY_FIELDS = ("email", "inn", "ogrn", "phone")


@dataclass(slots=True)
class EntityResult:
    """Итог извлечения+резолюции сущностей по набору заметок."""

    nodes: list[Node] = field(default_factory=list)  # дедуплицированные entity-узлы
    edges: list[Edge] = field(default_factory=list)  # MENTIONS-рёбра заметка → сущность
    candidates: list[MergeCandidate] = field(default_factory=list)  # спорные слияния
    errors: list[str] = field(default_factory=list)  # отвергнутые границей доверия фрагменты


def _parse_json_object(raw: str) -> dict | None:
    """Толерантно вытащить JSON-объект из ответа LLM (срезает ```-ограждение/прозу)."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def extract_entities(text: str, *, llm: EntityLLM, max_chars: int = 8000) -> dict:
    """Извлечь фрагмент {nodes, edges} из текста через LLM, пропустив через границу доверия.

    Вход в LLM предварительно обезвреживается (anti-prompt-injection): тело заметки —
    недоверенный контент и не должно перехватывать инструкции экстрактора. Выход
    валидируется и санитизируется; невалидный фрагмент отбрасывается (fail closed).
    """
    safe_input = neutralize_prompt_injection(text[:max_chars])
    raw = llm.answer(_SYSTEM, safe_input)
    fragment = _parse_json_object(raw)
    if fragment is None:
        return {"nodes": [], "edges": [], "errors": ["LLM не вернул валидный JSON-объект"]}
    errors = validate_llm_fragment(fragment)
    if errors:
        return {"nodes": [], "edges": [], "errors": errors}
    sanitize_llm_fragment(fragment)
    return fragment


def _fragment_to_nodes(
    fragment: dict, *, source_path: str, run_token: str | None
) -> tuple[list[Node], list[str]]:
    """Превратить узлы фрагмента в entity-``Node`` (person/org). Вернуть (узлы, их ключи)."""
    nodes: list[Node] = []
    keys: list[str] = []
    for n in fragment.get("nodes", []):
        if not isinstance(n, dict):
            continue
        etype = str(n.get("type", "")).lower()
        nid = str(n.get("id", "")).strip()
        label = str(n.get("label", "")).strip()
        if etype not in _ENTITY_TYPES or not nid or not label:
            continue
        nk = f"{etype}:{nid}"
        props: dict[str, object] = {}
        for fld in _IDENTITY_FIELDS:
            val = n.get(fld)
            if isinstance(val, str) and val.strip():
                props[fld] = val.strip()
        nodes.append(
            Node(
                type=etype,
                natural_key=nk,
                title=label,
                properties=props,
                provenance=Provenance(source_path=source_path, confidence=0.7, last_seen=run_token),
            )
        )
        keys.append(nk)
    return nodes, keys


def resolve_document_entities(
    docs: list[tuple[str, str, str]],
    *,
    llm: EntityLLM,
    run_token: str | None = None,
    auto_threshold: float = 95.0,
    cache: ExtractionCache | None = None,
    forced_merges: list[tuple[str, str]] | None = None,
    blocked_pairs: set[frozenset[str]] | None = None,
) -> EntityResult:
    """Извлечь сущности из заметок и дедуплицировать их fuzzy-резолвером.

    ``docs`` — список ``(doc_natural_key, source_path, body)``. Для каждой заметки
    извлекаются сущности и создаётся ребро ``MENTIONS`` (заметка → сущность). Затем по
    всем сущностям прогоняется ``resolve_entities``: авто-слияния применяются (рёбра
    перенаправляются на «выжившего»), спорные — возвращаются как кандидаты на арбитраж.
    ``cache`` (``ExtractionCache``) пропускает LLM-извлечение для неизменённых тел заметок.
    """
    result = EntityResult()
    raw_nodes: dict[str, Node] = {}
    mention_edges: list[Edge] = []

    for doc_key, source_path, body in docs:
        if not body or not body.strip():
            continue
        fragment = cache.get_fragment(body) if cache is not None else None
        if fragment is None:
            fragment = extract_entities(body, llm=llm)
            if cache is not None and not fragment.get("errors"):
                cache.put_fragment(body, fragment)
        if fragment.get("errors"):
            result.errors.extend(str(e) for e in fragment["errors"])
            continue
        nodes, keys = _fragment_to_nodes(fragment, source_path=source_path, run_token=run_token)
        for node in nodes:
            # Первое вхождение задаёт сущность; идентичность-свойства можно дополнить.
            existing = raw_nodes.get(node.natural_key)
            if existing is None:
                raw_nodes[node.natural_key] = node
            else:
                existing.properties.update(node.properties)
        for nk in keys:
            mention_edges.append(
                Edge(type="MENTIONS", src=doc_key, dst=nk, source_path=source_path)
            )

    entity_nodes = list(raw_nodes.values())
    if not entity_nodes:
        return result

    resolution = resolve_entities(
        entity_nodes,
        auto_threshold=auto_threshold,
        forced_merges=forced_merges,
        blocked_pairs=blocked_pairs,
    )
    merged_nodes, merged_edges = apply_merges(entity_nodes, mention_edges, resolution.merges)
    result.nodes = merged_nodes
    result.edges = merged_edges
    result.candidates = resolution.candidates
    return result
