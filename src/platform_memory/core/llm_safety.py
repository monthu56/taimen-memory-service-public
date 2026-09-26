# Адаптировано из graphify (semantic_cleanup.py и security.py,
# https://github.com/safishamsi/graphify), MIT License. Copyright (c) 2026 Safi Shamsi.
# See THIRD_PARTY.md for the full license text.
#
# Перенесено: валидация недоверенного LLM-JSON (лимиты байт/узлов/рёбер, regex на ID),
# отсечение «предложений-как-узлов» (sanitize_*_fragment), санитизация строк/метаданных
# (sanitize_label/sanitize_metadata). Адаптировано под наш контракт извлечения (nodes/edges
# без hyperedges) и под Cyrillic-эвристики прозы.
# Добавлено (оригинал Company Brain): neutralize_prompt_injection / wrap_untrusted —
# anti-prompt-injection перед склейкой недоверенного контента в контекст модели (T-073).
"""Граница доверия к LLM: валидация недоверенного JSON-извлечения и санитизация текста.

Любой контент, попавший в граф или в контекст модели из недоверенного источника (ответ
LLM-экстрактора, тело письма/транскрипта, агентский write-back), должен пройти через этот
модуль. Две задачи:

1. **Валидация JSON-извлечения** (``validate_llm_fragment`` / ``sanitize_llm_fragment``):
   жёсткие лимиты на размер/число узлов/рёбер и regex на ID не дают взбесившемуся или
   зловредному ответу исчерпать память или протащить ID с инъекцией; sanitize убирает
   «узлы-предложения» (текст-обоснование, который LLM ошибочно вынес в отдельный узел).

2. **Anti-prompt-injection** (``neutralize_prompt_injection`` / ``wrap_untrusted``): перед
   склейкой извлечённого текста в промпт глушим управляющие/zero-width/bidi-символы и
   нейтрализуем типовые фразы-инъекции («ignore previous instructions», «ты теперь…»,
   role-теги), чтобы недоверенный текст не перехватил инструкции модели.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import Any

# ── валидация недоверенного LLM-JSON ─────────────────────────────────────────────

MAX_FRAGMENT_BYTES = 25 * 1024 * 1024
MAX_FRAGMENT_NODES = 10_000
MAX_FRAGMENT_EDGES = 100_000
MAX_ID_LENGTH = 256

# ID извлечённой сущности: буквы (вкл. Unicode/Cyrillic), цифры и ._:- ; без '/','\\','..',
# управляющих символов — чтобы ID нельзя было использовать как вектор обхода путей/инъекции.
_ID_RE = re.compile(r"^[\w.:\-]+$", re.UNICODE)


def validate_llm_fragment(fragment: object) -> list[str]:
    """Вернуть список ошибок валидации недоверенного фрагмента извлечения (пусто = ок).

    Параметр — ``object`` (а не ``dict``): на вход может прийти произвольный
    десериализованный JSON, и первая же проверка отсекает всё, что не объект.
    """
    if not isinstance(fragment, dict):
        return ["fragment must be a JSON object"]

    errors: list[str] = []
    try:
        payload = json.dumps(fragment, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return [f"fragment is not JSON-serializable: {exc}"]

    if len(payload) > MAX_FRAGMENT_BYTES:
        errors.append(f"payload is {len(payload)} bytes; max is {MAX_FRAGMENT_BYTES}")

    nodes = fragment.get("nodes", [])
    edges = fragment.get("edges", [])
    if not isinstance(nodes, list):
        errors.append("nodes must be a list")
        nodes = []
    elif len(nodes) > MAX_FRAGMENT_NODES:
        errors.append(f"nodes has {len(nodes)} entries; max is {MAX_FRAGMENT_NODES}")

    if not isinstance(edges, list):
        errors.append("edges must be a list")
        edges = []
    elif len(edges) > MAX_FRAGMENT_EDGES:
        errors.append(f"edges has {len(edges)} entries; max is {MAX_FRAGMENT_EDGES}")

    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"nodes[{i}] must be an object")
            continue
        _validate_id(errors, f"nodes[{i}].id", node.get("id"))

    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f"edges[{i}] must be an object")
            continue
        _validate_id(errors, f"edges[{i}].source", edge.get("source"))
        _validate_id(errors, f"edges[{i}].target", edge.get("target"))

    return errors


def _validate_id(errors: list[str], field: str, value: object) -> None:
    if not isinstance(value, str):
        errors.append(f"{field} must be a string")
        return
    if not value:
        errors.append(f"{field} must not be empty")
        return
    if len(value) > MAX_ID_LENGTH:
        errors.append(f"{field} is {len(value)} chars; max is {MAX_ID_LENGTH}")
    if "/" in value or "\\" in value or ".." in value:
        errors.append(f"{field} must not contain path separators or '..'")
    if not _ID_RE.fullmatch(value):
        errors.append(f"{field} contains unsupported characters")


# ── отсечение «предложений-как-узлов» ───────────────────────────────────────────

# Метки длиннее стольких символов ИЛИ из стольких слов — кандидаты на «текст-обоснование»,
# а не имя сущности.
_RATIONALE_MIN_CHARS = 80
_RATIONALE_MIN_WORDS = 8


def _is_sentence_like(label: str) -> bool:
    """True, если метка похожа на прозу/обоснование, а не на имя сущности."""
    if not label:
        return False
    label = label.strip()
    if len(label) < _RATIONALE_MIN_CHARS and len(label.split()) < _RATIONALE_MIN_WORDS:
        return False
    return bool(re.search(r"[.!?:;]", label))


def sanitize_llm_fragment(fragment: dict) -> dict:
    """Почистить фрагмент извлечения in-place.

    1. Узлы без ``id`` отбрасываются (на них нельзя сослаться — вероятная галлюцинация).
    2. Узлы-предложения, источающие ребро ``rationale_for``, превращаются в атрибут
       ``rationale`` на узле-цели, а сам узел и его рёбра удаляются.
    3. Рёбра, ссылающиеся на удалённые узлы, отбрасываются.

    Возвращает тот же dict для удобства.
    """
    nodes: list[dict] = fragment.get("nodes", []) or []
    edges: list[dict] = fragment.get("edges", []) or []

    node_by_id: dict[str, dict] = {n["id"]: n for n in nodes if isinstance(n, dict) and n.get("id")}

    rationale_sources: set[str] = {
        e.get("source", "")
        for e in edges
        if isinstance(e, dict) and e.get("relation") == "rationale_for" and e.get("source")
    }

    remove_ids: set[str] = set()
    keep_nodes: list[dict] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = n.get("id", "")
        if not nid:
            continue  # узел без id — выкидываем
        if nid in rationale_sources and _is_sentence_like(n.get("label", "")):
            remove_ids.add(nid)
            continue
        keep_nodes.append(n)

    # Перенос текста-обоснования в атрибут узла-цели.
    for e in edges:
        if not isinstance(e, dict) or e.get("relation") != "rationale_for":
            continue
        src, tgt = e.get("source", ""), e.get("target")
        if src not in remove_ids or tgt not in node_by_id or tgt in remove_ids:
            continue
        text = str(node_by_id[src].get("label", "")).strip()
        target = node_by_id[tgt]
        existing = target.get("rationale", "")
        target["rationale"] = f"{existing}\n\n{text}".strip() if existing else text

    keep_edges = [
        e
        for e in edges
        if isinstance(e, dict)
        and e.get("source", "") not in remove_ids
        and e.get("target", "") not in remove_ids
    ]

    fragment["nodes"] = keep_nodes
    fragment["edges"] = keep_edges
    return fragment


# ── санитизация строк/метаданных ────────────────────────────────────────────────

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_LABEL_LEN = 256
_METADATA_MAX_VALUE_LEN = 512
_METADATA_MAX_LIST_ITEMS = 50


def sanitize_label(text: str | None) -> str:
    """Срезать управляющие символы и ограничить длину метки."""
    if text is None:
        return ""
    text = _CONTROL_CHAR_RE.sub("", str(text))
    return text[:_MAX_LABEL_LEN] if len(text) > _MAX_LABEL_LEN else text


def _sanitize_metadata_string(value: object) -> str:
    text = _CONTROL_CHAR_RE.sub("", str(value))
    text = html.escape(text, quote=True)
    return text[:_METADATA_MAX_VALUE_LEN] if len(text) > _METADATA_MAX_VALUE_LEN else text


def _sanitize_metadata_value(value: object) -> object:
    if isinstance(value, bool):  # bool — подкласс int, проверяем первым
        return value
    if isinstance(value, str):
        return _sanitize_metadata_string(value)
    if isinstance(value, dict):
        return sanitize_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata_value(item) for item in value[:_METADATA_MAX_LIST_ITEMS]]
    if isinstance(value, (int, float)) or value is None:
        return value
    return _sanitize_metadata_string(value)


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, object]:
    """Рекурсивно почистить метаданные перед экспортом: control-чары, HTML-escape, лимиты."""
    if metadata is None:
        return {}
    result: dict[str, object] = {}
    for key, value in metadata.items():
        clean_key = _sanitize_metadata_string(key)
        if clean_key:
            result[clean_key] = _sanitize_metadata_value(value)
    return result


# ── anti-prompt-injection (оригинал Company Brain) ──────────────────────────────

# Невидимые/двунаправленные символы, которыми прячут инъекции: zero-width, bidi-override,
# BOM. Управляющие чистит _CONTROL_CHAR_RE; здесь — то, что вне ASCII-control.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")

# Высокосигнальные фразы-инъекции (EN + RU). Совпадение глушится плейсхолдером —
# легитимный текст почти никогда не содержит этих конструкций дословно.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"ignore\s+(all\s+|the\s+)?(previous|prior|above|preceding|earlier)\s+"
        r"(instructions?|prompts?|context|messages?)",
        r"disregard\s+(all\s+|the\s+)?(previous|prior|above|preceding|earlier)\s+\w+",
        r"forget\s+(everything|all|(the\s+)?(previous|above)\s+\w+)",
        r"(you\s+are\s+now|from\s+now\s+on\s+you|act\s+as|pretend\s+to\s+be)\b",
        r"new\s+(instructions?|rules?|task)\s*:",
        r"(system|developer)\s+prompt\b",
        r"<\s*/?\s*(system|assistant|user|developer|im_start|im_end)\s*>",
        r"^\s*(system|assistant|user|developer)\s*:",
        # RU
        r"игнорир\w*\s+(все\s+|вс[ея]\s+)?(предыдущ\w+|вышеуказанн\w+|ранее\s+данн\w+)\s+"
        r"(инструкц\w+|указани\w+|контекст\w*)",
        r"забуд\w+\s+(все\s+|вс[ея]\s+)?(предыдущ\w+\s+)?(инструкц\w+|указани\w+|правил\w+)",
        r"(ты\s+теперь|отныне\s+ты|представь[,\s]+что\s+ты|веди\s+себя\s+как)\b",
        r"нов\w+\s+(инструкц\w+|правил\w+|задани\w+)\s*:",
        r"систем\w+\s+промпт\w*",
    )
]

_NEUTRALIZED = "⟦инструкция нейтрализована⟧"


def neutralize_prompt_injection(text: str | None) -> str:
    """Обезвредить недоверенный текст перед склейкой в контекст модели.

    Глушит невидимые/bidi-символы и нейтрализует типовые фразы-инъекции, заменяя их
    плейсхолдером. Управляющие символы (кроме ``\\t\\n\\r``) тоже срезаются.
    """
    if not text:
        return ""
    text = _INVISIBLE_RE.sub("", str(text))
    text = _CONTROL_CHAR_RE.sub("", text)
    for pat in _INJECTION_PATTERNS:
        text = pat.sub(_NEUTRALIZED, text)
    return text


def wrap_untrusted(text: str | None, *, label: str = "НЕДОВЕРЕННЫЙ КОНТЕНТ") -> str:
    """Завернуть недоверенный текст в явные сентинелы, экранируя их вхождения внутри.

    Делает границу недоверенного блока однозначной для модели и не даёт контенту
    «выйти» из блока, подделав закрывающий сентинел. Текст предварительно проходит
    ``neutralize_prompt_injection``.
    """
    clean = neutralize_prompt_injection(text)
    open_tag, close_tag = f"<<{label}>>", f"<</{label}>>"
    clean = clean.replace("<<", "‹‹").replace(">>", "››")
    return f"{open_tag}\n{clean}\n{close_tag}"
