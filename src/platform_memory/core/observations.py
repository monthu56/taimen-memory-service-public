"""Observation — immutable-свидетельство внешнего источника (ADR-016).

Observation — первичная форма памяти Context Memory Engine: запись того, что
источник сообщил или наблюдал. Сырой факт-свидетельство, НЕ интерпретация:
интерпретации (факты, эпизоды) выводятся из наблюдений и всегда хранят
provenance до observation_id.

Идентичность и идемпотентность:
- source identity = (system, stream, external_id): повторный ingest того же
  внешнего события не создаёт дубликат;
- без external_id дедуп идёт по content_hash канонической формы;
- observation_id детерминирован (sha256 от identity) — клиент может вычислить
  его заранее и безопасно повторять доставку.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from platform_memory.core.scopes import MAX_ITEM_SCOPES, resolve_scopes

# Статусы обработки observation (постобработка не теряет сырую запись).
STATUS_RECEIVED = "received"
STATUS_PROCESSED = "processed"
STATUS_PARTIAL = "partially_processed"
STATUS_FAILED = "failed"
STATUS_REDACTED = "redacted"
STATUSES = frozenset(
    {STATUS_RECEIVED, STATUS_PROCESSED, STATUS_PARTIAL, STATUS_FAILED, STATUS_REDACTED}
)

# Класс свидетельства: прямое утверждение источника сильнее LLM-вывода.
EVIDENCE_ASSERTED = "asserted"  # структурированное утверждение источника
EVIDENCE_EXTRACTED = "extracted"  # извлечено детерминированным правилом
EVIDENCE_INFERRED = "inferred"  # выведено LLM из unstructured-контента
EVIDENCE_DERIVED = "derived"  # производное consolidation (сводка, слияние)
EVIDENCE_CLASSES = frozenset(
    {EVIDENCE_ASSERTED, EVIDENCE_EXTRACTED, EVIDENCE_INFERRED, EVIDENCE_DERIVED}
)

# Ограничения полей source identity / kind (без путей и управляющих символов).
_IDENT_RE = re.compile(r"^[^\s\x00-\x1f]{1,256}$")
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

MAX_CONTENT_BYTES = 1 * 1024 * 1024  # 1 MiB человекочитаемого контента
MAX_DATA_BYTES = 1 * 1024 * 1024  # 1 MiB structured payload
MAX_ASSERTIONS = 200  # structured assertions на одно observation


def _utc_iso(value: str) -> str:
    """Нормализовать метку времени в ISO-8601 UTC (суффикс Z -> +00:00 -> Z).

    Строки ISO-8601 UTC сравнимы лексикографически — на этом стоит вся
    temporal-фильтрация (ADR-016 §4).
    """
    text = value.strip()
    if not text:
        return ""
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def entity_ref_key(ref: Any) -> str:
    """natural_key сущности по ссылке: строка как есть либо dict {type,id[,key]}."""
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    if isinstance(ref, dict):
        if ref.get("key"):
            return str(ref["key"])
        etype, eid = str(ref.get("type", "")).strip(), str(ref.get("id", "")).strip()
        if etype and eid:
            return f"{etype}:{eid}"
    raise ValueError(f"Некорректная ссылка на сущность: {ref!r}")


def entity_ref_type(ref: Any, default: str = "entity") -> str:
    """Тип сущности по ссылке (для placeholder-узлов)."""
    if isinstance(ref, dict) and ref.get("type"):
        return str(ref["type"])
    if isinstance(ref, str) and ":" in ref:
        return ref.split(":", 1)[0]
    return default


def _ref(value: Any) -> dict[str, str] | None:
    """Нормализовать ссылку на участника: {"type","id"} либо None."""
    if value is None:
        return None
    if isinstance(value, dict) and value.get("id"):
        return {"type": str(value.get("type", "")), "id": str(value["id"])}
    raise ValueError(f"Ссылка actor/subject должна быть dict {{type,id}}: {value!r}")


@dataclass(slots=True)
class Observation:
    """Наблюдение внешнего источника в канонической форме (после validate())."""

    namespace: str = ""
    source_system: str = ""
    source_stream: str = ""
    external_id: str = ""
    kind: str = ""
    occurred_at: str = ""  # event time, ISO-8601 UTC ("" = неизвестно)
    actor: dict[str, str] | None = None
    subject: dict[str, str] | None = None
    scopes: list[str] = field(default_factory=list)  # канонические "type:id"
    content: str = ""  # человекочитаемый текст
    data: dict[str, Any] = field(default_factory=dict)  # structured payload
    assertions: list[dict[str, Any]] = field(default_factory=list)  # см. ingest
    provenance: dict[str, Any] = field(default_factory=dict)  # {uri, …}

    def canonicalize(self) -> Observation:
        """Валидировать и привести к канонической форме (in-place); вернуть self."""
        for name in ("source_system", "source_stream", "external_id"):
            value = getattr(self, name)
            if value and not _IDENT_RE.match(str(value)):
                raise ValueError(f"Некорректное поле {name}: {str(value)[:64]!r}")
            setattr(self, name, str(value or ""))
        if self.external_id and not self.source_system:
            raise ValueError("external_id без source.system не образует source identity")
        if self.kind and not _KIND_RE.match(self.kind):
            raise ValueError(f"Некорректный kind {self.kind!r}: [a-z0-9][a-z0-9._-]{{0,127}}")
        self.occurred_at = _utc_iso(self.occurred_at)
        self.actor = _ref(self.actor)
        self.subject = _ref(self.subject)
        self.scopes = resolve_scopes(self.scopes, limit=MAX_ITEM_SCOPES)
        if not isinstance(self.content, str):
            raise ValueError("content должен быть строкой")
        if len(self.content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError(f"content больше {MAX_CONTENT_BYTES} байт")
        if not isinstance(self.data, dict):
            raise ValueError("data должен быть объектом")
        if len(json.dumps(self.data, ensure_ascii=False).encode("utf-8")) > MAX_DATA_BYTES:
            raise ValueError(f"data больше {MAX_DATA_BYTES} байт")
        if not isinstance(self.assertions, list) or len(self.assertions) > MAX_ASSERTIONS:
            raise ValueError(f"assertions: список не длиннее {MAX_ASSERTIONS}")
        if not isinstance(self.provenance, dict):
            raise ValueError("provenance должен быть объектом")
        if not (self.content or self.data or self.assertions):
            raise ValueError("Пустое observation: нужен content, data или assertions")
        return self

    # --- идентичность ---

    def content_hash(self) -> str:
        """sha256 канонической формы содержимого (дедуп при отсутствии external_id)."""
        canon = json.dumps(
            {
                "kind": self.kind,
                "occurred_at": self.occurred_at,
                "actor": self.actor,
                "subject": self.subject,
                "scopes": sorted(self.scopes),
                "content": self.content,
                "data": self.data,
                "assertions": self.assertions,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def observation_id(self) -> str:
        """Детерминированный публичный id: от source identity либо от content_hash."""
        if self.external_id:
            identity = f"{self.source_system}|{self.source_stream}|{self.external_id}"
        else:
            identity = f"content|{self.content_hash()}"
        digest = hashlib.sha256(f"{self.namespace}|{identity}".encode()).hexdigest()
        return f"obs-{digest[:24]}"

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, namespace: str = "") -> Observation:
        """Собрать Observation из входного JSON (HTTP/MCP/CLI) и канонизировать.

        Принимает как плоские поля, так и вложенный ``source: {system, stream,
        externalId|external_id}``; scopes — список строк "type:id" или dict {type,id}.
        """
        source = payload.get("source") or {}
        return cls(
            namespace=namespace,
            source_system=str(source.get("system", payload.get("source_system", "")) or ""),
            source_stream=str(source.get("stream", payload.get("source_stream", "")) or ""),
            external_id=str(
                source.get("externalId", source.get("external_id", payload.get("external_id", "")))
                or ""
            ),
            kind=str(payload.get("kind", "") or ""),
            occurred_at=str(payload.get("occurredAt", payload.get("occurred_at", "")) or ""),
            actor=payload.get("actor"),
            subject=payload.get("subject"),
            scopes=list(payload.get("scopes") or []),
            content=str(payload.get("content", "") or ""),
            data=dict(payload.get("data") or {}),
            assertions=list(payload.get("assertions") or []),
            provenance=dict(payload.get("provenance") or {}),
        ).canonicalize()
