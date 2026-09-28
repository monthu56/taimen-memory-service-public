"""Схемы OpenAPI маршрутов ядра /api/memory/* (MEM-ADR-020, амендмент 2026-09-28).

Модели двух видов:

- **новые поля запроса** (``dryRun``, ``expectedState``, ``where``, ``scope``/``namespace``
  пакета) валидируются pydantic — это новый контракт;
- **прежние поля** (``kinds``, ``relations``, ``anchors``, ``traverse``…) объявлены через
  ``_documented(T)``: схема ``T`` попадает в OpenAPI, а значение проходит как есть и
  валидируется движком, как раньше. Коды ошибок прежнего входа (``400``/``422``) не
  меняются.

Модели ответов — только документация (``responses=`` маршрута): выдачу они не
фильтруют, поэтому аддитивные поля движка доходят до потребителя без правки схем.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainValidator

# Имя атрибута в where и searchable — как ключ атрибутов сущности.
ATTR_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
MAX_WHERE_CLAUSES = 20
MAX_SEARCHABLE_FIELDS = 20

WhereOp = Literal["eq", "in", "prefix", "lte", "gte", "exists"]


def _as_is(value: Any) -> Any:
    return value


def _documented(schema_type: Any) -> Any:
    """Поле с документированной схемой ``schema_type`` без валидации pydantic."""
    return Annotated[Any, PlainValidator(_as_is, json_schema_input_type=schema_type)]


class _Doc(BaseModel):
    """База схем: имена полей — как в JSON контракта (camelCase там, где он есть)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --- доменный пакет: searchable у вида и пакеты арендатора ---


class SearchableSpec(BaseModel):
    """Поиск по смыслу по сущностям вида: текст эмбеддинга — title и эти атрибуты.

    Если у вида объявлены ``attributes.properties``, каждое поле должно быть среди них
    (иначе 400 при регистрации). Вид без ``searchable`` не индексируется.
    """

    fields: list[Annotated[str, Field(pattern=ATTR_NAME_PATTERN)]] = Field(
        min_length=1,
        max_length=MAX_SEARCHABLE_FIELDS,
        description="Имена атрибутов, чьи значения входят в текст эмбеддинга сущности.",
    )


class KindSpecIn(_Doc):
    """Вид сущности пакета."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    natural_key: dict[str, Any] | str | None = Field(
        default=None,
        alias="naturalKey",
        description="JSON Schema строки ключа или шаблон с плейсхолдерами <name> / {name}.",
    )
    aliases: list[str] | None = Field(default=None, description="Шаблоны форм ключа.")
    kind_aliases: list[str] | None = Field(
        default=None, alias="kindAliases", description="Синонимы имени вида."
    )
    id_patterns: list[str] | None = Field(
        default=None, alias="idPatterns", description="Регулярные выражения идентификаторов."
    )
    attributes: dict[str, Any] | None = Field(
        default=None, description="JSON Schema атрибутов (подмножество)."
    )
    searchable: SearchableSpec | None = Field(
        default=None,
        description="Индексировать сущности вида для поиска по смыслу (амендмент 2026-09-28).",
    )


class RelationSpecIn(_Doc):
    """Связь пакета."""

    relation: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    from_kinds: list[str] | None = Field(default=None, alias="fromKinds")
    to_kinds: list[str] | None = Field(default=None, alias="toKinds")
    temporal: bool = True
    cardinality: Literal["one", "many"] = "many"


class PackIn(_Doc):
    """Доменный пакет: {name, version, kinds[], relations[]} (валидирует core.kinds).

    ``version`` — строка или число (``version: 1`` из YAML пакета приводится к ``"1"``).
    ``scope: tenant`` + ``namespace`` — пакет арендатора, видимый только в namespace
    владельца и под ним (амендмент 2026-09-28); без ``scope`` — общий пакет.
    """

    name: str
    version: str | int | float
    description: str | None = None
    kinds: _documented(list[KindSpecIn] | None) = None
    relations: _documented(list[RelationSpecIn] | None) = None
    scope: Literal["common", "tenant"] | None = Field(
        default=None,
        description="common (по умолчанию) — общий пакет (service scope); tenant — пакет "
        "арендатора (право записи в namespace-владелец).",
    )
    namespace: str | None = Field(
        default=None,
        description="Namespace-владелец пакета арендатора: пакет виден в нём и в "
        "namespace с префиксом '<владелец>:'.",
    )


class PackRegistered(_Doc):
    """Ответ регистрации: created (201) или unchanged (200)."""

    status: Literal["created", "unchanged"]
    pack: dict[str, Any]


class PackageSummary(_Doc):
    """Элемент списка пакетов."""

    name: str
    versions: list[str]
    latest: str
    builtin: bool
    scope: Literal["common", "tenant"] = Field(
        default="common", description="Отсутствует у ответов до K007 — значит common."
    )
    namespace: str | None = Field(default=None, description="Владелец пакета арендатора.")
    ref: str | None = Field(
        default=None, description="Ссылка для packages namespace: name или tenant:<name>."
    )


class PackageList(_Doc):
    packages: list[PackageSummary]


# --- сверка снимка: dryRun, expectedState, stateToken ---


class NodeKey(_Doc):
    kind: str
    key: str


class ReconcileChanges(_Doc):
    """Ключи узлов, тронутых сверкой (амендмент 2026-09-27)."""

    opened: list[NodeKey] = []
    changed: list[NodeKey] = []
    closed: list[NodeKey] = []
    limit: int = 0
    truncated: bool = False


class KeyConflict(_Doc):
    """Сущность снимка, открытая в namespace версией другого (source, scope)."""

    kind: str
    key: str
    source: str
    scope: str = ""


class ReconcileConflicts(_Doc):
    items: list[KeyConflict] = []
    limit: int = 0
    truncated: bool = False


class ReconcileIn(_Doc):
    """Документ снимка как есть (формат SNAPSHOT.md пакета) + namespace и scopes записи.

    Поля снимка (``pack, source, scope, snapshotId, observedAt, entities, relations``)
    валидирует ``domain.reconcile.parse_snapshot``. ``scope`` здесь — строка снимка
    (часть источника), а не scope namespace: namespace передаётся полем ``namespace``
    или query-параметром ``?namespace=``. ``scopes`` — scopes видимости (MEM-ADR-019),
    которые ядро выводит из задачи. ``dryRun`` и ``expectedState`` — предпросмотр и
    применение по состоянию (амендмент 2026-09-28); в документ снимка не входят.
    """

    namespace: str | None = None
    scopes: list[str] | None = None
    dry_run: bool = Field(
        default=False,
        alias="dryRun",
        description="Построить план сверки без записи: счётчики, changes, conflicts, stateToken.",
    )
    expected_state: str | None = Field(
        default=None,
        alias="expectedState",
        max_length=128,
        description="stateToken плана: применить, только если состояние пары "
        "(source, scope) с тех пор не менялось; иначе 409 snapshot_stale.",
    )


class ReconcileResult(_Doc):
    """Ответ сверки: счётчики, ключи тронутых узлов, состояние пары (source, scope)."""

    source: str
    scope: str = ""
    namespace: str
    snapshot_id: str
    observed_at: str
    pack: str | None = None
    opened: int
    closed: int
    unchanged: int
    superseded: int
    entities: dict[str, int]
    relations: dict[str, int]
    changes: ReconcileChanges
    duplicate: bool
    dry_run: bool = Field(default=False, alias="dryRun")
    state_token: str | None = Field(
        default=None,
        alias="stateToken",
        description="Отпечаток открытых версий пары (source, scope): после записи — новое "
        "состояние, у dryRun — состояние, на котором построен план.",
    )
    conflicts: ReconcileConflicts | None = None
    search_index: dict[str, int] | None = Field(
        default=None,
        description="Синхронизация индекса сущностей (виды с searchable): indexed, "
        "updated, removed, unchanged. Нет, если индекса в namespace нет.",
    )


class SnapshotStaleDetail(BaseModel):
    code: Literal["snapshot_stale"]
    message: str
    expected_state: str = Field(alias="expectedState")
    state_token: str = Field(alias="stateToken", description="Текущее состояние пары.")


class SnapshotStaleError(BaseModel):
    """409: expectedState не совпал с текущим состоянием пары (source, scope)."""

    detail: SnapshotStaleDetail


# --- типизированный обход: where ---


class WhereClause(BaseModel):
    """Условие на атрибут сущности (версия на as_of); условия списка — по «И».

    ``prefix`` — по сегментам через точку: ``62.01`` совпадает с ``62.01.11``, но не с
    ``62.011``. ``lte``/``gte`` — число или дата ISO 8601, включительно. ``exists`` —
    ``value: bool`` (по умолчанию true). Атрибут-список выполняет условие, если его
    выполняет хоть один элемент.
    """

    attr: str = Field(pattern=ATTR_NAME_PATTERN)
    op: WhereOp
    value: Any = None


class AnchorIn(_Doc):
    kind: str | None = None
    value: str


class TraverseStepIn(_Doc):
    relation: str
    direction: Literal["in", "out", "both"] = "out"
    depth: int = Field(default=1, ge=1, le=5)
    limit: int = Field(default=20, ge=1, le=200)
    from_: Literal["anchors", "previous"] = Field(default="anchors", alias="from")
    where: list[WhereClause] | None = Field(
        default=None,
        max_length=MAX_WHERE_CLAUSES,
        description="Фильтр сущностей, достигнутых этим шагом.",
    )


class TypedContextIn(_Doc):
    """Типизированный обход: якоря -> связи с direction/depth/limit на as_of."""

    anchors: _documented(list[AnchorIn | str]) = None
    traverse: _documented(list[TraverseStepIn] | None) = None
    as_of: _documented(str | None) = None
    allow_semantic: _documented(bool) = None
    semantic_k: _documented(int) = None
    scope: _documented(dict[str, Any] | None) = None
    where: list[WhereClause] | None = Field(
        default=None,
        max_length=MAX_WHERE_CLAUSES,
        description="Фильтр кандидатов в якоря (всех шагов разрешения, включая смысловой).",
    )


class AnchorMatch(_Doc):
    """Сущность, в которую разрешён якорь."""

    natural_key: str
    kind: str
    namespace: str | None = None
    method: Literal["natural_key", "alias", "id_pattern", "normalized", "suffix", "semantic"]
    evidence: Literal["asserted", "extracted", "inferred"]
    score: float | None = Field(
        default=None, description="Косинусная близость смыслового кандидата, 0..1 (K005)."
    )
    matched_on: Literal["entity", "chunk"] | None = Field(
        default=None,
        alias="matchedOn",
        description="Смысловой кандидат найден по индексу сущностей или по фрагменту (K005).",
    )


class AnchorResolution(_Doc):
    input: dict[str, Any]
    resolved: list[AnchorMatch]
    matched_by: str | None = Field(default=None, alias="matchedBy")
    ambiguous: bool | None = None
    candidates: list[dict[str, Any]] | None = None
    truncated: bool | None = None
    filtered: int | None = Field(
        default=None, description="Сколько кандидатов в якорь отброшено where (K006)."
    )


class TypedContextResult(_Doc):
    """Ответ типизированного обхода (схема ключевых полей; прочие — как в INTEGRATION.md)."""

    as_of: str | None = None
    namespaces: list[str] = []
    anchors: list[AnchorResolution]
    unresolved: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    used: dict[str, Any]
    sources: list[dict[str, Any]] = []
    stats: dict[str, Any] | None = None
    trace_id: str


class EntitiesQueryIn(_Doc):
    """Перечень сущностей видов с фильтрами на as_of (журнал сверки снимков)."""

    kinds: list[Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]] = Field(
        min_length=1, max_length=20, description="Виды сущностей; неизвестный вид — пусто."
    )
    where: list[WhereClause] | None = Field(
        default=None,
        max_length=MAX_WHERE_CLAUSES,
        description="Условия на атрибуты версии на as_of (по «И»).",
    )
    as_of: str | None = Field(
        default=None, alias="asOf", description="Момент ISO 8601; пусто — открытые сейчас."
    )
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = Field(default=None, description="nextCursor предыдущей страницы.")
    namespaces: list[str] | None = Field(
        default=None, description="Namespaces чтения (то же, что scope.namespaces)."
    )
    scope: dict[str, Any] | None = None


class EntityItem(_Doc):
    """Сущность перечня — версия, действующая на as_of."""

    kind: str
    key: str
    namespace: str
    title: str
    attributes: dict[str, Any]
    source: str = Field(description="Источник снимка, держащий версию.")
    scope: str = Field(description="Scope источника снимка.")
    snapshot_id: str
    source_path: str = Field(description="Цитата: путь/ссылка на источник версии.")
    valid_from: str | None = None
    valid_to: str | None = None


class EntitiesQueryResult(_Doc):
    """Страница перечня в порядке (kind, key, namespace); конец — nextCursor: null."""

    items: list[EntityItem]
    next_cursor: str | None = Field(alias="nextCursor")
    as_of: str | None = None
    namespaces: list[str] = []
    stats: dict[str, Any] | None = None


# --- ошибки ---


class NotImplementedDetail(BaseModel):
    code: Literal["not_implemented"]
    feature: str
    message: str


class NotImplementedResponse(BaseModel):
    """501: поле контракта объявлено, но ещё не исполняется (амендмент 2026-09-28)."""

    detail: NotImplementedDetail


class PackScopeConflictDetail(BaseModel):
    code: Literal["pack_name_conflict", "kind_conflict", "relation_conflict"]
    message: str


class PackScopeConflict(BaseModel):
    """409: имя пакета арендатора, его вида или связи совпало с общим пакетом (K007).

    409 иммутабельности версии по-прежнему несёт ``detail``-строку.
    """

    detail: PackScopeConflictDetail | str
