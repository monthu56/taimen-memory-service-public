"""Канал ``resolve`` Context Compiler: детерминированное разрешение идентификаторов в узлы.

MEM-ADR-020, амендмент «канал resolve»; TAI-ADR-0042 п.4 («сначала детерминированное
разрешение»). LLM не участвует, вектор — тоже: найденное здесь — ``evidence: resolved``.
Тем же поиском по журналу (``match_candidates``) разрешает якоря типизированный обход
(``context/typed.py``, амендменты «typed-контекст разрешает якоря через
resolve_candidates» и «пакетное разрешение якорей»): пачкой, отдельно в каждом namespace.

Кандидаты — якоря запроса и идентификаторы, извлечённые из ``query``: общим
извлекателем (``core.identifiers``) и ``idPatterns`` видов доменных пакетов namespace
(шаблоны приходят данными пакета, домен в код не зашит). Кандидат, целиком входящий в
другой (``ADR-0062`` внутри ``CP-ADR-0062``, путь внутри ``POST <путь>``), отбрасывается:
разрешается более точная форма.

Порядок разрешения кандидата — до первого успешного шага:

1. ``natural_key`` — точный ключ сущности;
2. ``alias`` — псевдоним ключа (``aliases`` версии: формы ключа по шаблонам ``aliases``
   вида и явные псевдонимы снимка — так ``ADR-0035`` находит ``*-0035`` всех рядов,
   ``CP-ADR-0061`` — ``CP-0061``, путь с исходными именами параметров — эндпоинт);
3. ``normalized`` — те же ключ/псевдоним для нормализованной формы: параметры шаблона
   ``{name}`` приводятся к ``{}`` (``POST /goals/{goal_id}`` -> ``POST /goals/{}``);
4. ``suffix`` — ключ оканчивается кандидатом (или его нормализованной формой) по
   границе разделителя `` ``/``.``/``:``: путь без метода — все методы
   (``<METHOD> <путь>``), имя метода — ``<repo>:<module>.<Class>.<method>``, имя
   таблицы — ``<repo>:<table>``. Кандидат короче ``MIN_SUFFIX_LEN`` суффиксом не ищется.

Поиск — по журналу версий сверки (``SnapshotLedger``), только по индексам: ключ
(``key_idx``), GIN по ``aliases``, B-tree по перевёрнутому ключу для суффикса; без
полного скана. Кандидатов — не больше ``MAX_CANDIDATES``, разрешённых сущностей — не
больше ``MAX_RESOLVED`` (суффиксом — не больше ``MAX_PER_SUFFIX`` на кандидата). Лимит
раздаётся в два прохода: сначала точные совпадения (шаги 1–3) всех кандидатов, затем
суффиксные на остаток — неоднозначные имена не вытесняют точные ключи.

Сущность, которую держат несколько источников, разрешается в сведение их версий
(MEM-ADR-022, ``domain/merge.py``), а не в самую позднюю: ``rank`` — приоритет
источников по ``sourcePriority`` видов. Разрешённой не по точному ключу сущности
дочитываются все её версии — псевдоним или суффикс мог найтись не у каждого источника.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from platform_memory.core.identifiers import extract_identifiers
from platform_memory.core.kinds import KindCatalog
from platform_memory.domain.merge import SourceRank, merge_by_entity

MAX_CANDIDATES = 40
MAX_RESOLVED = 20
MAX_PER_SUFFIX = 10
MIN_SUFFIX_LEN = 4
MAX_CANDIDATE_LEN = 300
# Разделители, по границе которых кандидат может быть суффиксом ключа.
SUFFIX_SEPARATORS = (" ", ".", ":")
# Методы, разрешающие «точно»: ключ, его псевдоним или нормализованная форма.
EXACT_METHODS = frozenset({"natural_key", "alias", "normalized"})

_PLACEHOLDER = re.compile(r"\{[^{}\s]*\}")


def normalize_placeholders(value: str) -> str:
    """Параметры шаблона ``{name}`` -> ``{}`` (ключ хранит параметры безымянными)."""
    return _PLACEHOLDER.sub("{}", value)


@dataclass(slots=True)
class Candidate:
    """Кандидат якоря: строка из запроса/якорей и необязательный вид (из якоря)."""

    value: str
    kind: str = ""
    origin: str = "query"  # anchor | query

    @property
    def forms(self) -> list[str]:
        norm = normalize_placeholders(self.value)
        return [self.value] if norm == self.value else [self.value, norm]


def _anchor_candidates(anchors: Iterable[Any]) -> list[Candidate]:
    out: list[Candidate] = []
    for ref in anchors:
        if isinstance(ref, str) and ref.strip():
            out.append(Candidate(ref.strip(), origin="anchor"))
        elif isinstance(ref, dict):
            if ref.get("key"):
                out.append(Candidate(str(ref["key"]).strip(), origin="anchor"))
                continue
            etype, eid = str(ref.get("type", "")).strip(), str(ref.get("id", "")).strip()
            if eid:
                out.append(Candidate(eid, kind=etype, origin="anchor"))
    return out


def extract_candidates(
    query: str,
    anchors: Iterable[Any] = (),
    catalogs: Iterable[KindCatalog] = (),
    *,
    limit: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """Кандидаты разрешения: якоря, затем идентификаторы ``query`` в порядке появления."""
    found: dict[str, int] = {}  # токен -> позиция первого появления

    def _note(token: str) -> None:
        token = token.strip()
        if not token or len(token) > MAX_CANDIDATE_LEN:
            return
        pos = query.find(token)
        found.setdefault(token, pos if pos >= 0 else len(query))

    for catalog in catalogs:
        for _kind, token in catalog.extract_ids(query):
            _note(token)
    for token in extract_identifiers(query, limit=limit):
        _note(token)
    from_query = [t for t, _ in sorted(found.items(), key=lambda kv: (kv[1], -len(kv[0])))]

    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for cand in [*_anchor_candidates(anchors), *(Candidate(t) for t in from_query)]:
        ident = (cand.value, cand.kind)
        if ident in seen or len(cand.value) > MAX_CANDIDATE_LEN:
            continue
        seen.add(ident)
        out.append(cand)
    # Кандидат, целиком входящий в другой кандидат запроса, — неточная часть его.
    values = [c.value for c in out]
    out = [
        c
        for c in out
        if c.origin == "anchor" or not any(c.value != v and c.value in v for v in values)
    ]
    return out[:limit]


class EntityLookup(Protocol):
    """Индексный поиск версий сущностей (реализация — ``SnapshotLedger``)."""

    def entities_by_keys(self, namespaces, keys, *, as_of, allowed_scopes, limit): ...

    def entities_by_aliases(self, namespaces, aliases, *, as_of, allowed_scopes, limit): ...

    def entities_by_suffixes(
        self, namespaces, suffixes, *, as_of, allowed_scopes, per_suffix, per_namespace, kinds
    ): ...


@dataclass(slots=True)
class Resolved:
    """Сущность, в которую разрешён кандидат, — актуальная версия из журнала."""

    namespace: str
    kind: str
    key: str
    method: str
    input: str
    form: str
    row: dict[str, Any] = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        return self.method in EXACT_METHODS


def _kind_names(catalogs: Sequence[KindCatalog], kind: str) -> set[str] | None:
    if not kind:
        return None
    names = {kind}
    for catalog in catalogs:
        canon = catalog.canonical(kind)
        spec = catalog.kinds.get(canon)
        names |= {canon, *(spec.kind_aliases if spec else ())}
    return names


def _merged(
    rows: Iterable[dict[str, Any]], rank: SourceRank | None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Одна строка на сущность (namespace, вид, ключ): сведение её версий (MEM-ADR-022)."""
    return merge_by_entity(rows, rank)


KindNames = Callable[[str, str | None], set[str] | None]


@dataclass(slots=True)
class Match:
    """Совпадения кандидата: строки журнала первого успешного шага разрешения."""

    hits: list[tuple[dict[str, Any], str, str]] = field(
        default_factory=list
    )  # строка, метод, форма
    # Суффиксу подошло больше ``per_suffix`` сущностей — отдана только часть.
    truncated: bool = False


def match_candidates(
    lookup: EntityLookup,
    candidates: Sequence[Candidate],
    namespaces: Sequence[str],
    *,
    kind_names: KindNames,
    as_of: str = "",
    allowed_scopes: Sequence[str] | None = None,
    limit: int = MAX_RESOLVED * 10,
    per_suffix: int = MAX_PER_SUFFIX,
    per_namespace: bool = False,
    rank: SourceRank | None = None,
) -> dict[tuple[int, str | None], Match]:
    """Совпадения кандидатов в журнале — не больше трёх запросов на всех кандидатов.

    Ключи и псевдонимы всех форм всех кандидатов во всех namespaces — по одному
    запросу, суффиксы кандидатов без точного совпадения — одним (если такие есть).
    Результат — по ``(номер кандидата, namespace)``; ``per_namespace=False`` — namespace
    ``None``: шаги разрешения идут до первого успешного по всем namespaces сразу (канал
    resolve). ``per_namespace=True`` — отдельно в каждом namespace, и лимит суффикса —
    на namespace (typed-контекст: якорь разрешается в каждой базе независимо).
    ``kind_names(вид, namespace)`` — допустимые виды строк (None — любой). Строка
    совпадения — сведение версий сущности (``rank`` — приоритет источников).
    """
    out: dict[tuple[int, str | None], Match] = {}
    if not candidates or not namespaces:
        return out
    groups: list[str | None] = list(namespaces) if per_namespace else [None]
    forms = list(dict.fromkeys(f for c in candidates for f in c.forms))
    kw = {"as_of": as_of, "allowed_scopes": allowed_scopes}
    by_key = _merged(lookup.entities_by_keys(namespaces, forms, limit=limit, **kw), rank)
    by_alias = _merged(lookup.entities_by_aliases(namespaces, forms, limit=limit, **kw), rank)

    def _match(form: str, ns: str | None) -> tuple[list[dict[str, Any]], str]:
        rows = [
            r
            for (rns, _, key), r in sorted(by_key.items())
            if key == form and (ns is None or rns == ns)
        ]
        if rows:
            return rows, "natural_key"
        rows = [
            r
            for (rns, _, _), r in sorted(by_alias.items())
            if form in (r["payload"].get("aliases") or []) and (ns is None or rns == ns)
        ]
        return rows, "alias"

    pending: list[tuple[int, str | None]] = []
    for i, cand in enumerate(candidates):
        for group in groups:
            names = kind_names(cand.kind, group)
            hits: list[tuple[dict[str, Any], str, str]] = []
            for n, form in enumerate(cand.forms):
                rows, method = _match(form, group)
                rows = [r for r in rows if names is None or r["kind"] in names]
                if rows:
                    method = method if n == 0 else "normalized"
                    hits = [(r, method, form) for r in rows]
                    break
            out[(i, group)] = Match(hits)
            if not hits and len(cand.value) >= MIN_SUFFIX_LEN:
                pending.append((i, group))

    if pending:
        waiting = set(pending)
        suffixes: list[tuple[int, str, str]] = []  # (кандидат, форма, суффикс)
        for i in sorted({i for i, _ in pending}):
            for form in candidates[i].forms:
                for sep in SUFFIX_SEPARATORS:
                    suffixes.append((i, form, sep + form))
        suffix_nss = sorted({g for _, g in pending if g is not None}) or list(namespaces)

        def _union(i: int) -> set[str] | None:
            """Виды кандидата во всех ожидающих namespaces (None — любой)."""
            union: set[str] = set()
            for g in {g for j, g in pending if j == i}:
                names = kind_names(candidates[i].kind, g)
                if names is None:
                    return None
                union |= names
            return union

        kinds_of = {i: _union(i) for i in {i for i, _ in pending}}
        # На строку больше лимита: так видно, что суффиксу подошло больше, чем отдано.
        # Виды — фильтром в запросе, до лимита: чужие виды не вытесняют нужные.
        got = lookup.entities_by_suffixes(
            suffix_nss,
            [s for _, _, s in suffixes],
            per_suffix=per_suffix + 1,
            per_namespace=per_namespace,
            kinds=[kinds_of[i] for i, _, _ in suffixes],
            **kw,
        )
        per_unit: dict[tuple[int, str | None], list[tuple[dict[str, Any], str, str]]] = {}
        # (суффикс, группа) -> сущности допустимых видов: признак усечения.
        seen: dict[tuple[int, str | None], set[tuple[str, str, str]]] = {}
        for idx, row in got:
            i, form, _ = suffixes[idx]
            unit = (i, row["namespace"] if per_namespace else None)
            if unit not in waiting:
                continue
            names = kind_names(candidates[i].kind, unit[1])
            if names is not None and row["kind"] not in names:
                continue
            per_unit.setdefault(unit, []).append((row, "suffix", form))
            ident = (row["namespace"], row["kind"], row["key"])
            seen.setdefault((idx, unit[1]), set()).add(ident)
        for unit, hits in per_unit.items():
            i, group = unit
            latest = _merged((r for r, _, _ in hits), rank)
            form_of = {(r["namespace"], r["kind"], r["key"]): f for r, _, f in hits}
            chosen = [(latest[k], "suffix", form_of[k]) for k in sorted(latest)]
            # Усечено — какому-то суффиксу подошло больше ``per_suffix`` разных
            # сущностей нужного вида (запрос отдаёт на строку больше лимита) или всего
            # по формам кандидата их больше ``per_suffix``.
            cut = any(
                len(idents) > per_suffix
                for (idx, g), idents in seen.items()
                if g == group and suffixes[idx][0] == i
            )
            out[unit] = Match(chosen[:per_suffix], truncated=cut or len(chosen) > per_suffix)
    return out


def resolve_candidates(
    lookup: EntityLookup,
    candidates: Sequence[Candidate],
    namespaces: Sequence[str],
    *,
    catalogs: Sequence[KindCatalog] = (),
    as_of: str = "",
    allowed_scopes: Sequence[str] | None = None,
    max_resolved: int = MAX_RESOLVED,
    per_suffix: int = MAX_PER_SUFFIX,
    rank: SourceRank | None = None,
) -> tuple[list[Resolved], list[dict[str, Any]]]:
    """Разрешить кандидатов в сущности; вернуть (разрешённые, отчёт по кандидатам).

    Не больше трёх запросов к журналу (``match_candidates``) и ещё одного — за всеми
    версиями сущностей, разрешённых не по точному ключу. ``truncated`` в отчёте
    кандидата — исчерпан ``max_resolved``. ``rank`` — приоритет источников сведения.
    """
    if not candidates or not namespaces:
        return [], []
    found = match_candidates(
        lookup,
        candidates,
        namespaces,
        kind_names=lambda kind, _ns: _kind_names(catalogs, kind),
        as_of=as_of,
        allowed_scopes=allowed_scopes,
        limit=max_resolved * 10,
        per_suffix=per_suffix,
        rank=rank,
    )
    matches = [(cand, found[(i, None)]) for i, cand in enumerate(candidates)]

    # Два прохода: сначала точные совпадения (ключ, псевдоним, нормализованная форма)
    # всех кандидатов, затем суффиксные — на остаток лимита. Иначе неоднозначные имена
    # в начале текста (по MAX_PER_SUFFIX узлов на каждое) вытесняют точные ключи.
    resolved: list[Resolved] = []
    taken: set[tuple[str, str, str]] = set()
    report: list[dict[str, Any]] = []
    for cand, _ in matches:
        entry: dict[str, Any] = {"input": cand.value, "resolved": []}
        if cand.kind:
            entry["kind"] = cand.kind
        report.append(entry)
    for exact_pass in (True, False):
        for (cand, match), entry in zip(matches, report, strict=True):
            for row, method, form in match.hits:
                if (method in EXACT_METHODS) != exact_pass:
                    continue
                ident = (row["namespace"], row["kind"], row["key"])
                if ident in taken:
                    continue
                if len(resolved) >= max_resolved:
                    entry["truncated"] = True
                    break
                taken.add(ident)
                resolved.append(
                    Resolved(
                        namespace=row["namespace"],
                        kind=row["kind"],
                        key=row["key"],
                        method=method,
                        input=cand.value,
                        form=form,
                        row=row,
                    )
                )
                entry["resolved"].append(
                    {
                        "namespace": row["namespace"],
                        "kind": row["kind"],
                        "key": row["key"],
                        "method": method,
                    }
                )
    _complete(lookup, resolved, rank, as_of=as_of, allowed_scopes=allowed_scopes)
    return resolved, report


def _complete(
    lookup: EntityLookup,
    resolved: list[Resolved],
    rank: SourceRank | None,
    *,
    as_of: str,
    allowed_scopes: Sequence[str] | None,
) -> None:
    """Дочитать все версии сущностей, разрешённых не по ключу, и пересвести их строки."""
    partial = [r for r in resolved if r.method != "natural_key"]
    if not partial:
        return
    rows = lookup.entities_by_keys(
        sorted({r.namespace for r in partial}),
        sorted({r.key for r in partial}),
        as_of=as_of,
        allowed_scopes=allowed_scopes,
        limit=MAX_RESOLVED * 10 * len(partial),
    )
    full = _merged(rows, rank)
    for r in partial:
        row = full.get((r.namespace, r.kind, r.key))
        if row is not None:
            r.row = row


def entity_text(resolved: Resolved) -> tuple[str, str]:
    """(title, text) сущности для пакета: заголовок и ключ с вид и атрибутами."""
    payload = resolved.row.get("payload") or {}
    title = str(payload.get("title") or resolved.key)
    head = (
        f"{title} ({resolved.kind} {resolved.key})"
        if title != resolved.key
        else (f"{title} ({resolved.kind})")
    )
    attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    parts = [
        f"{k}={v}"
        for k, v in sorted(attrs.items())
        if isinstance(v, str | int | float | bool) and str(v).strip()
    ]
    text = head + ("; " + ", ".join(parts) if parts else "")
    return title, text[:1000]


def source_path_of(resolved: Resolved) -> str:
    """Цитата версии: ``repo@sha:path:line`` из снимка, иначе ссылка на сам снимок."""
    payload = resolved.row.get("payload") or {}
    return str(
        payload.get("source_path")
        or f"snapshot:{resolved.row.get('source', '')}/{resolved.row.get('snapshot_id', '')}"
    )
