"""Типизированный обход в Context Compiler (MEM-ADR-020; TAI-ADR-0042 п.4–5).

Запрос::

    {"anchors": [{"kind": "issue", "value": "PROJ-42"}],
     "traverse": [{"relation": "blocks", "direction": "out", "depth": 2, "limit": 20}],
     "as_of": "2026-09-01T00:00:00Z", "namespaces": ["tenant:t1"],
     "allow_semantic": false}

1. Якоря разрешаются детерминированно, до первого успешного шага: точный
   ``natural_key`` (или ``<kind>:<value>`` — ключ сущности наблюдений), псевдоним ключа
   сущности (``aliases``: формы ключа по шаблонам ``aliases`` вида и явные псевдонимы
   снимка), идентификатор, извлечённый из значения по ``idPatterns`` видов,
   ``normalized`` (параметры ``{name}`` -> ``{}``) и ``suffix`` (ключ оканчивается
   значением по границе `` ``/``.``/``:``: путь без метода, файл без репозитория,
   таблица по имени). Журнал сверки опрашивается тем же ``match_candidates``, что и
   канал resolve (``context/resolve.py``); графовые запросы (по меткам видов) добирают
   сущности без журнала. Все якоря во всех namespaces разрешаются пачкой — число
   запросов не зависит от числа якорей (амендмент «пакетное разрешение якорей»).
   Способ разрешения якоря — ``matchedBy``; несколько сущностей по неточной форме —
   ``ambiguous`` и ``candidates`` (якорь разрешён во все, выбор не делается); шагу,
   разрешившему якорь, подошло больше сущностей, чем отдано (суффиксу — больше
   ``MAX_PER_SUFFIX`` допустимого вида в namespace, точному шагу журнала — больше
   ``MAX_RESOLVED``), — ``truncated``. Семантический добор (векторный поиск) —
   только при явном ``allow_semantic`` и помечается ``evidence: inferred``: индекс
   сущностей видов с ``searchable`` (кандидат — сам узел сущности) вместе с
   фрагментами документов; у кандидата — ``score`` (косинусная близость 0..1) и
   ``matchedOn: entity | chunk`` (MEM-ADR-020, амендмент 2026-09-28).
2. Обход идёт только по рёбрам заданной связи, валидным на ``as_of`` (пусто —
   действующие сейчас); закрытые факты не проходятся. ``direction`` — in|out|both,
   ``depth`` — число шагов по связи, ``limit`` — максимум новых сущностей шага.
   Шаг начинается от якорей (``from: anchors``, по умолчанию) или от сущностей,
   достигнутых предыдущим шагом (``from: previous``). Уровень шага — запрос рёбер по
   graphid всего фронта (валидность рёбер — в SQL до лимита чтения; рёбер больше
   лимита — ``truncated`` у шага в ``stats.steps``) и запрос узлов на других концах.
3. Сущность видна на ``as_of``, если у неё есть версия в журнале снимков, валидная
   на этот момент (атрибуты — из этой версии), либо — для сущностей без журнала —
   если её собственный интервал ``valid_from/valid_to`` содержит момент.
4. Фильтры ``where`` (``context/where.py``) проверяют атрибуты этой версии.
   ``where`` верхнего уровня отбрасывает кандидатов в якоря на каждом шаге
   разрешения, включая смысловой: шаг без прошедших кандидатов считается неуспешным,
   и разрешение идёт дальше; число отброшенных — ``filtered`` якоря.
   ``traverse[i].where`` отбрасывает сущности, достигнутые шагом: они не входят в
   выдачу, не продолжают обход и не дают фактов шага (``filtered`` в ``stats.steps``).

Ответ — пакет с разделами по видам, фактами, списком использованных сущностей,
фактов и id снимков (evidence задачи в Control Plane) и trace_id. LLM не участвует.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import psycopg

from platform_memory.core import db as dbmod
from platform_memory.core import timing
from platform_memory.core.config import Settings
from platform_memory.core.kinds import KindCatalog
from platform_memory.core.llm_safety import neutralize_prompt_injection
from platform_memory.core.namespaces import resolve_namespaces
from platform_memory.core.ontology import sanitize_label
from platform_memory.core.scopes import MAX_ALLOWED_SCOPES, is_visible, resolve_scopes
from platform_memory.context.resolve import MAX_RESOLVED, Candidate, Match, match_candidates
from platform_memory.context.trace import ContextTraceStore
from platform_memory.context.where import WhereClause, parse_where
from platform_memory.context.where import matches as where_matches
from platform_memory.domain.reconcile import SnapshotLedger, valid_at
from platform_memory.domain.registry import open_registry
from platform_memory.domain.searchable import entity_index
from platform_memory.graph.facts import normalize_ts
from platform_memory.graph.store import GraphStore
from platform_memory.index import VectorIndex, build_embedder
from platform_memory.index.embeddings import Embedder

DIRECTIONS = frozenset({"in", "out", "both"})
MAX_ANCHORS = 20
MAX_STEPS = 10
MAX_DEPTH = 5
MAX_STEP_LIMIT = 200
MAX_SEMANTIC_K = 10

# Разрешение якоря -> класс свидетельства (ADR-016 §11).
EVIDENCE_BY_METHOD = {
    "natural_key": "asserted",
    "alias": "asserted",
    "id_pattern": "extracted",
    "normalized": "asserted",
    "suffix": "extracted",
    "semantic": "inferred",
}
METHOD_ORDER = ("natural_key", "alias", "id_pattern", "normalized", "suffix", "semantic")
# Неточные шаги: несколько совпадений — неоднозначность, а не набор якорей.
AMBIGUOUS_METHODS = frozenset({"normalized", "suffix"})
# Не больше узлов на псевдоним графа (сущности без журнала) на пару якорь × namespace.
MAX_GRAPH_ALIASES = 20
# Служебные ключи props, не являющиеся атрибутами сущности.
_SERVICE_PROPS = frozenset({"aliases", "snapshot", "scopes", "provenance"})


@dataclass(slots=True)
class AnchorRef:
    value: str
    kind: str = ""


@dataclass(slots=True)
class TraverseStep:
    relation: str
    direction: str = "out"
    depth: int = 1
    limit: int = 20
    start: str = "anchors"  # anchors | previous
    where: list[WhereClause] = field(default_factory=list)


@dataclass(slots=True)
class TypedContextRequest:
    anchors: list[AnchorRef] = field(default_factory=list)
    traverse: list[TraverseStep] = field(default_factory=list)
    as_of: str = ""
    namespaces: list[str] = field(default_factory=list)
    allow_semantic: bool = False
    semantic_k: int = 3
    # Фильтр кандидатов в якоря (все шаги разрешения).
    where: list[WhereClause] = field(default_factory=list)
    # Разрешённые scopes видимости (MEM-ADR-019) — задаёт HTTP-слой, не клиент.
    allowed_scopes: list[str] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TypedContextRequest:
        """Собрать и провалидировать запрос из JSON (camelCase поддержан)."""
        raw_anchors = payload.get("anchors") or []
        if not isinstance(raw_anchors, list) or not raw_anchors:
            raise ValueError("anchors: нужен непустой список {kind?, value}")
        if len(raw_anchors) > MAX_ANCHORS:
            raise ValueError(f"anchors: не больше {MAX_ANCHORS}")
        anchors: list[AnchorRef] = []
        for i, raw in enumerate(raw_anchors):
            if isinstance(raw, str):
                raw = {"value": raw}
            if not isinstance(raw, dict) or not str(raw.get("value", "") or "").strip():
                raise ValueError(f"anchors[{i}]: нужен объект с непустым value")
            anchors.append(
                AnchorRef(value=str(raw["value"]).strip(), kind=str(raw.get("kind", "") or ""))
            )
        raw_steps = payload.get("traverse") or []
        if not isinstance(raw_steps, list) or len(raw_steps) > MAX_STEPS:
            raise ValueError(f"traverse: список до {MAX_STEPS} шагов")
        steps: list[TraverseStep] = []
        for i, raw in enumerate(raw_steps):
            if not isinstance(raw, dict) or not str(raw.get("relation", "") or "").strip():
                raise ValueError(f"traverse[{i}]: нужен relation")
            direction = str(raw.get("direction", "out") or "out")
            if direction not in DIRECTIONS:
                raise ValueError(f"traverse[{i}].direction: одно из {sorted(DIRECTIONS)}")
            depth = int(raw.get("depth", 1) or 1)
            limit = int(raw.get("limit", 20) or 20)
            if not 1 <= depth <= MAX_DEPTH:
                raise ValueError(f"traverse[{i}].depth: 1..{MAX_DEPTH}")
            if not 1 <= limit <= MAX_STEP_LIMIT:
                raise ValueError(f"traverse[{i}].limit: 1..{MAX_STEP_LIMIT}")
            start = str(raw.get("from", "anchors") or "anchors")
            if start not in ("anchors", "previous"):
                raise ValueError(f"traverse[{i}].from: anchors|previous")
            steps.append(
                TraverseStep(
                    relation=str(raw["relation"]).strip().lower(),
                    direction=direction,
                    depth=depth,
                    limit=limit,
                    start=start,
                    where=parse_where(raw.get("where"), f"traverse[{i}].where"),
                )
            )
        as_of = str(payload.get("as_of", payload.get("asOf", "")) or "")
        allowed = payload.get("allowed_scopes", payload.get("allowedScopes"))
        return cls(
            anchors=anchors,
            traverse=steps,
            as_of=(normalize_ts(as_of) or "") if as_of else "",
            namespaces=list(payload.get("namespaces") or []),
            allow_semantic=bool(payload.get("allow_semantic", payload.get("allowSemantic", False))),
            semantic_k=max(
                1,
                min(
                    MAX_SEMANTIC_K,
                    int(payload.get("semantic_k", payload.get("semanticK", 3)) or 3),
                ),
            ),
            where=parse_where(payload.get("where")),
            allowed_scopes=[str(s) for s in allowed] if allowed is not None else None,
        )


def _ident(node: dict[str, Any]) -> tuple[str, str, str]:
    """Идентичность сущности в журнале снимков: (namespace, вид, ключ)."""
    return (
        str(node.get("namespace", "")),
        str(node.get("type") or "entity"),
        str(node.get("natural_key", "")),
    )


def _unique(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одна запись на сущность (журнал и граф могут вернуть один узел дважды)."""
    return list({id(s): s for s in states}.values())


def _scopes(props: dict[str, Any]) -> list[str]:
    raw = props.get("scopes")
    return [str(s) for s in raw] if isinstance(raw, list) else []


def _match(score: float | None, matched_on: str) -> dict[str, Any]:
    """Поля смыслового разрешения якоря: ``score`` (0..1, если известна) и ``matchedOn``."""
    out: dict[str, Any] = {"matchedOn": matched_on}
    if score is not None:
        out["score"] = round(max(0.0, min(1.0, float(score))), 6)
    return out


def _summarize(out: dict[str, Any], *, truncated: bool) -> None:
    """``matchedBy``, ``ambiguous``/``candidates`` и ``truncated`` якоря по разрешённому."""
    if not out["resolved"]:
        return
    # Способ разрешения якоря — самый точный из шагов по всем namespaces.
    methods = {r["method"] for r in out["resolved"]}
    out["matchedBy"] = next(m for m in METHOD_ORDER if m in methods)
    inexact = [r for r in out["resolved"] if r["method"] in AMBIGUOUS_METHODS]
    if len(inexact) > 1:
        # Неточная форма подошла к нескольким сущностям: якорь разрешён во все, выбор
        # не делается — кандидаты отдаются вызывающему. Точные совпадения других
        # namespaces в неоднозначность не входят.
        out["ambiguous"] = True
        out["candidates"] = [
            {"namespace": r["namespace"], "kind": r["kind"], "natural_key": r["natural_key"]}
            for r in inexact
        ]
    if truncated:
        # Шагу разрешения подошло больше сущностей, чем отдано: суффиксу — больше
        # MAX_PER_SUFFIX на namespace, точному шагу журнала — больше MAX_RESOLVED.
        out["truncated"] = True


class _Compiler:
    """Состояние одной компиляции: сущности, факты, журнал версий."""

    def __init__(
        self,
        graph: GraphStore,
        ledger: SnapshotLedger | None,
        catalogs: dict[str, KindCatalog],
        req: TypedContextRequest,
        allowed: list[str] | None,
    ):
        self.graph = graph
        self.ledger = ledger
        self.catalogs = catalogs
        self.req = req
        self.allowed = allowed
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        # Видимые на as_of сущности, включая не прошедшие where (в выдачу не входят).
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self.facts: dict[str, dict[str, Any]] = {}
        self._versions: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        # (namespace, ключ) -> graphid вершин: старт шагов обхода без поиска по свойствам.
        self.gids: dict[tuple[str, str], set[int]] = {}
        self._labels: dict[int, tuple[str, str]] | None = None
        # Шаги, у которых на уровне BFS рёбер оказалось больше лимита чтения.
        self.edges_truncated: set[int] = set()
        # Шаг -> число сущностей, отброшенных его where.
        self.filtered: dict[int, int] = {}
        # Фазы компиляции (stats.phases_ms); versions — внутри anchors/traverse.
        self.phases = timing.Phases()

    # --- состояние сущности на as_of ---

    def _load_versions(self, nodes: list[dict[str, Any]]) -> None:
        if self.ledger is None:
            return
        missing = {_ident(n) for n in nodes} - set(self._versions)
        if not missing:
            return
        with self.phases("versions"):
            got = self.ledger.entity_versions(
                sorted({ns for ns, _, _ in missing}), sorted({k for _, _, k in missing})
            )
        for key in missing:
            self._versions[key] = got.get(key, [])

    def entity_state(self, node: dict[str, Any]) -> dict[str, Any] | None:
        """Запись сущности на as_of либо None (не видна: закрыта, вне scope, не существовала)."""
        props = node.get("props") if isinstance(node.get("props"), dict) else {}
        if not is_visible(_scopes(props), self.allowed):
            return None
        ns, kind, key = _ident(node)
        versions = self._versions.get((ns, kind, key)) or []
        as_of = self.req.as_of
        snapshot = props.get("snapshot") if isinstance(props.get("snapshot"), dict) else None
        if versions:
            live = [v for v in versions if valid_at(v["valid_from"], v["valid_to"], as_of)]
            if not live:
                return None
            version = live[-1]
            payload = version["payload"]
            attributes = dict(payload.get("attributes") or {})
            aliases = list(payload.get("aliases") or [])
            provenance = payload.get("provenance")
            title = str(payload.get("title") or node.get("title") or key)
            snapshot = {
                "source": version["source"],
                "scope": version["scope"],
                "snapshot_id": version["snapshot_id"],
            }
            valid_from, valid_to = version["valid_from"], version["valid_to"]
            # Цитата — источник той версии, что действовала на as_of.
            source_path = str(
                payload.get("source_path")
                or f"snapshot:{version['source']}/{version['snapshot_id']}"
            )
        else:
            valid_from = node.get("valid_from")
            valid_to = node.get("valid_to")
            if not valid_at(valid_from, valid_to, as_of):
                return None
            attributes = {k: v for k, v in props.items() if k not in _SERVICE_PROPS}
            aliases = [str(a) for a in props.get("aliases") or []]
            provenance = props.get("provenance")
            title = str(node.get("title") or key)
            source_path = str(node.get("source_path") or "")
        return {
            "natural_key": key,
            "kind": kind,
            "namespace": ns,
            "title": neutralize_prompt_injection(title),
            "attributes": attributes,
            "aliases": aliases,
            "provenance": provenance if isinstance(provenance, dict) else None,
            "source_path": source_path,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "snapshot": snapshot,
            "reached_via": [],
        }

    def _state(self, node: dict[str, Any]) -> dict[str, Any] | None:
        """Запись сущности на as_of (одна на (namespace, ключ)); в выдачу не добавляет."""
        ident = (str(node.get("namespace", "")), str(node.get("natural_key", "")))
        state = self._states.get(ident)
        if state is None:
            state = self.entity_state(node)
            if state is not None:
                self._states[ident] = state
        return state

    def _admit(self, state: dict[str, Any]) -> None:
        self.entities.setdefault((state["namespace"], state["natural_key"]), state)

    # --- якоря ---

    def _kind_names(self, catalog: KindCatalog, kind: str) -> set[str]:
        canon = catalog.canonical(kind)
        spec = catalog.kinds.get(canon)
        return {canon, kind, *(spec.kind_aliases if spec else ())}

    def _filter_kind(
        self, nodes: list[dict[str, Any]], names: set[str] | None
    ) -> list[dict[str, Any]]:
        if names is None:
            return nodes
        return [n for n in nodes if str(n.get("type") or "entity") in names]

    def _graph_labels(self) -> dict[int, tuple[str, str]]:
        if self._labels is None:
            self._labels = self.graph.graph_labels()
        return self._labels

    def _labels_for(self, names: set[str] | None) -> list[str]:
        """Метки узлов видов ``names``; без вида — все метки вершин графа."""
        if names is not None:
            return sorted({sanitize_label(n) for n in names})
        return sorted(name for name, kind in self._graph_labels().values() if kind == "v")

    def _remember(self, nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if node.get("_gid") is not None:
                ident = (str(node.get("namespace", "")), str(node.get("natural_key", "")))
                self.gids.setdefault(ident, set()).add(int(node["_gid"]))

    def _lookup(
        self,
        keys: dict[str, set[str]],
        aliases: dict[str, set[str]] | None = None,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """Узлы графа (namespace, ключ) -> узлы: один запрос и одна загрузка версий."""
        if not keys and not aliases:
            return {}
        nodes = self.graph.lookup_nodes(keys, aliases, self.req.namespaces)
        self._remember(nodes)
        self._load_versions(nodes)
        out: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for node in nodes:
            ident = (str(node.get("namespace", "")), str(node.get("natural_key", "")))
            out.setdefault(ident, []).append(node)
        return out

    def resolve_anchor(self, anchor: AnchorRef, embedder_factory) -> dict[str, Any]:
        return self.resolve_anchors([anchor], embedder_factory)[0]

    def resolve_anchors(self, anchors: list[AnchorRef], embedder_factory) -> list[dict[str, Any]]:
        """Разрешить все якоря во всех namespaces пакетно.

        Шаги разрешения идут до первого успешного — отдельно для каждой пары якорь ×
        namespace, как прежде; данные шагов берутся пачками на все пары сразу:

        1. граф: точные ключи якорей (и ``<kind>:<value>``) и идентификаторы из
           ``idPatterns`` — один запрос по меткам видов якорей;
        2. для пар без точного ключа — журнал (``match_candidates``: не больше трёх
           запросов на все якоря и namespaces), затем узлы графа найденных журналом
           сущностей и псевдонимы узлов без журнала — один запрос.

        Версии журнала для видимости на ``as_of`` — одной выборкой после каждого шага.
        """
        nss = self.req.namespaces
        names: dict[tuple[int, str], set[str] | None] = {}
        keys: dict[str, set[str]] = {}
        exact: dict[tuple[int, str], set[str]] = {}
        by_pattern: dict[tuple[int, str], list[tuple[set[str], list[str]]]] = {}
        for i, anchor in enumerate(anchors):
            for ns in nss:
                catalog = self.catalogs[ns]
                kinds = self._kind_names(catalog, anchor.kind) if anchor.kind else None
                names[(i, ns)] = kinds
                forms = {anchor.value, *(f"{k}:{anchor.value}" for k in sorted(kinds or ()))}
                exact[(i, ns)] = forms
                for label in self._labels_for(kinds):
                    keys.setdefault(label, set()).update(forms)
                canon = catalog.canonical(anchor.kind) if anchor.kind else ""
                for kind, token in catalog.extract_ids(anchor.value, [canon] if canon else []):
                    token_kinds = self._kind_names(catalog, kind)
                    token_keys = [token, *(f"{k}:{token}" for k in token_kinds)]
                    by_pattern.setdefault((i, ns), []).append((token_kinds, token_keys))
                    for label in self._labels_for(token_kinds):
                        keys.setdefault(label, set()).update(token_keys)
        first = self._lookup(keys)

        def _nodes(ns: str, wanted: Iterable[str], found) -> list[dict[str, Any]]:
            return [n for key in sorted(set(wanted)) for n in found.get((ns, key), [])]

        resolved: dict[tuple[int, str], list[dict[str, Any]]] = {}
        truncated: set[int] = set()
        # Якорь -> кандидаты (namespace, ключ), отброшенные where.
        dropped: dict[int, set[tuple[str, str]]] = {}
        where = self.req.where

        def _try(i: int, ns: str, method: str, nodes: list[dict[str, Any]]) -> bool:
            nodes = self._filter_kind(nodes, names[(i, ns)])
            # Смысловой кандидат несёт ``_match`` (score, matchedOn); первый — лучший.
            matched: dict[int, dict[str, Any]] = {}
            states: list[dict[str, Any]] = []
            for node in nodes:
                st = self._state(node)
                if st is None:
                    continue
                states.append(st)
                if isinstance(node.get("_match"), dict):
                    matched.setdefault(id(st), node["_match"])
            states = _unique(states)
            # where отсекает и кандидатов, найденных по смыслу (K006 поверх K005).
            hits = [st for st in states if where_matches(st["attributes"], where)]
            passed = {id(st) for st in hits}
            dropped.setdefault(i, set()).update(
                (st["namespace"], st["natural_key"]) for st in states if id(st) not in passed
            )
            if not hits:
                return False
            evidence = EVIDENCE_BY_METHOD[method]
            for state in hits:
                self._admit(state)
                state["anchor"] = True
                state.setdefault("evidence", evidence)
                state["reached_via"].append({"anchor": anchors[i].value, "method": method})
                resolved.setdefault((i, ns), []).append(
                    {
                        "natural_key": state["natural_key"],
                        "namespace": state["namespace"],
                        "kind": state["kind"],
                        "method": method,
                        "evidence": evidence,
                        **matched.get(id(state), {}),
                    }
                )
            return True

        pending = [
            (i, ns)
            for i in range(len(anchors))
            for ns in nss
            if not _try(i, ns, "natural_key", _nodes(ns, exact[(i, ns)], first))
        ]

        matches: dict[tuple[int, str | None], Match] = {}
        second: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if pending:
            todo = sorted({i for i, _ in pending})
            if self.ledger is not None:
                # Журнал — одна реализация разрешения с каналом resolve.
                found = match_candidates(
                    self.ledger,
                    [
                        Candidate(anchors[i].value, kind=anchors[i].kind, origin="anchor")
                        for i in todo
                    ],
                    nss,
                    kind_names=lambda kind, ns: (
                        self._kind_names(self.catalogs[ns], kind) if kind else None
                    ),
                    as_of=self.req.as_of,
                    allowed_scopes=self.allowed,
                    limit=MAX_RESOLVED * 10 * len(todo) * len(nss),
                    per_namespace=True,
                )
                for (j, ns), match in found.items():
                    if len(match.hits) > MAX_RESOLVED:
                        match = Match(match.hits[:MAX_RESOLVED], truncated=True)
                    matches[(todo[j], ns)] = match
            ledger_keys: dict[str, set[str]] = {}
            for unit in pending:
                for row, _, _ in matches.get(unit, Match()).hits:
                    ledger_keys.setdefault(sanitize_label(row["kind"]), set()).add(row["key"])
            # Псевдонимы узлов графа — сущности без журнала (наблюдения, записи API).
            aliases: dict[str, set[str]] = {}
            for i, ns in pending:
                for label in self._labels_for(names[(i, ns)]):
                    aliases.setdefault(label, set()).add(anchors[i].value)
            second = self._lookup(ledger_keys, aliases)

        def _ledger(unit: tuple[int, str], method: str) -> list[dict[str, Any]]:
            """Узлы графа для сущностей журнала: удалённый через API узел не всплывает."""
            match = matches.get(unit, Match())
            wanted = {(row["kind"], row["key"]) for row, m, _ in match.hits if m == method}
            wanted_keys = sorted({k for _, k in wanted})
            nodes = _nodes(unit[1], wanted_keys, second)
            return [
                n for n in nodes if (str(n.get("type") or "entity"), n["natural_key"]) in wanted
            ]

        def _graph_alias(i: int, ns: str) -> list[dict[str, Any]]:
            value = anchors[i].value
            nodes = [
                n
                for (nns, _), group in sorted(second.items())
                if nns == ns
                for n in group
                if value in ((n.get("props") or {}).get("aliases") or [])
            ]
            return nodes[:MAX_GRAPH_ALIASES]

        for i, ns in pending:
            attempts: list[tuple[str, Any]] = [
                ("alias", lambda i=i, ns=ns: _ledger((i, ns), "alias") + _graph_alias(i, ns)),
                (
                    "id_pattern",
                    lambda i=i, ns=ns: [
                        n
                        for kinds, token_keys in by_pattern.get((i, ns), [])
                        for n in self._filter_kind(_nodes(ns, token_keys, first), kinds)
                    ],
                ),
                ("normalized", lambda i=i, ns=ns: _ledger((i, ns), "normalized")),
                ("suffix", lambda i=i, ns=ns: _ledger((i, ns), "suffix")),
            ]
            if self.req.allow_semantic:
                attempts.append(
                    (
                        "semantic",
                        lambda i=i, ns=ns: self._semantic(
                            embedder_factory, i, ns, anchors, names[(i, ns)]
                        ),
                    )
                )
            for method, fetch in attempts:
                if _try(i, ns, method, fetch()):
                    match = matches.get((i, ns), Match())
                    if match.truncated and match.hits and match.hits[0][1] == method:
                        truncated.add(i)
                    break

        outs: list[dict[str, Any]] = []
        for i, anchor in enumerate(anchors):
            out = {
                "input": {"kind": anchor.kind, "value": anchor.value},
                "resolved": [r for ns in nss for r in resolved.get((i, ns), [])],
            }
            _summarize(out, truncated=i in truncated)
            if where:
                out["filtered"] = len(dropped.get(i, ()))
            outs.append(out)
        return outs

    def _semantic(
        self,
        embedder_factory,
        i: int,
        ns: str,
        anchors: list[AnchorRef],
        kinds: set[str] | None,
    ) -> list[dict[str, Any]]:
        """Смысловые кандидаты якоря: сущности индекса и узлы фрагментов, лучшие первыми.

        ``embedder_factory(value, ns, kinds)`` -> ``(сущности, фрагменты)``: списки
        ``((kind | None, key), score | None)``. Сущность — узел этого вида и ключа
        (метка вида: без просмотра всех вершин), фрагмент — узел-владелец чанка.
        Кандидаты сливаются по ``score`` (у сущности при равенстве — приоритет),
        фильтруются по виду якоря и режутся до ``semantic_k`` разных узлов.
        """
        entities, chunks = embedder_factory(anchors[i].value, ns, kinds)
        by_label: dict[str, set[str]] = {}
        for (kind, key), _ in entities:
            by_label.setdefault(sanitize_label(kind), set()).add(key)
        found = self._lookup(by_label) if by_label else {}
        ranked: list[tuple[float, int, dict[str, Any]]] = []
        for (kind, key), score in entities:
            for node in found.get((ns, key), []):
                if str(node.get("type") or "entity") == kind:
                    ranked.append((score, 0, {**node, "_match": _match(score, "entity")}))
        chunk_nodes = self.graph.nodes_by_keys([key for (_, key), _ in chunks], [ns])
        self._remember(chunk_nodes)
        self._load_versions(chunk_nodes)
        best: dict[str, float] = {}
        for (_, key), score in chunks:
            best[key] = max(best.get(key, -1.0), -1.0 if score is None else score)
        for node in chunk_nodes:
            score = best.get(str(node.get("natural_key", "")), -1.0)
            match = _match(None if score < 0 else score, "chunk")
            ranked.append((score, 1, {**node, "_match": match}))
        ranked.sort(key=lambda r: (-r[0], r[1]))
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        # Вид якоря — до среза: фрагменты чужих видов не вытесняют нужные сущности.
        for node in self._filter_kind([r[2] for r in ranked], kinds):
            ident = (str(node.get("type") or "entity"), str(node.get("natural_key", "")))
            if ident in seen:
                continue
            seen.add(ident)
            out.append(node)
        return out[: self.req.semantic_k]

    # --- обход ---

    def traverse(self, step_no: int, step: TraverseStep, start: list[tuple[str, str]]):
        """Один шаг: BFS по связи до depth, не больше limit новых сущностей.

        Уровень BFS — один запрос рёбер на все вершины фронта всех namespaces и
        направлений, один — узлов на других концах, одна загрузка версий журнала.
        """
        visited = set(start)
        frontier = list(start)
        reached: list[tuple[str, str]] = []
        filtered: set[tuple[str, str]] = set()
        directions = ["out", "in"] if step.direction == "both" else [step.direction]
        for depth in range(1, step.depth + 1):
            if not frontier or len(reached) >= step.limit:
                break
            gid_of: dict[int, tuple[str, str]] = {}
            for ident in frontier:
                for gid in self.gids.get(ident, ()):
                    gid_of[gid] = ident
            rows, cut = self.graph.typed_neighbors(
                {gid: ns for gid, (ns, _) in gid_of.items()},
                step.relation,
                directions,
                labels=self._graph_labels(),
                as_of=self.req.as_of,
            )
            if cut:
                self.edges_truncated.add(step_no)
            self._remember([r[2] for r in rows])
            self._load_versions([r[2] for r in rows])
            # Порядок прежний: namespace, направление, (откуда, куда, id ребра); на
            # namespace и направление — не больше limit*5+50 рёбер.
            grouped: dict[tuple[str, str], list[tuple[str, str, dict, dict, int]]] = {}
            for src, direction, node, rel, rid in rows:
                ns, src_key = gid_of[src]
                grouped.setdefault((ns, direction), []).append(
                    (src_key, str(node.get("natural_key", "")), node, rel, rid)
                )
            next_frontier: list[tuple[str, str]] = []
            for ns in sorted({ns for ns, _ in grouped}):
                rel_spec = self.catalogs[ns].relation(step.relation)
                for direction in directions:
                    batch = sorted(
                        grouped.get((ns, direction), []), key=lambda r: (r[0], r[1], r[4])
                    )
                    for src_key, dst_key, node, rel, rid in batch[: step.limit * 5 + 50]:
                        if not is_visible(_scopes(rel), self.allowed):
                            continue
                        ident = (ns, dst_key)
                        is_new = ident not in visited
                        if is_new and len(reached) >= step.limit:
                            continue
                        state = self._state(node)
                        if state is None:
                            continue
                        if step.where and not where_matches(state["attributes"], step.where):
                            # Не прошла фильтр шага: ни выдачи, ни факта, ни обхода дальше.
                            filtered.add(ident)
                            continue
                        self._admit(state)
                        state.setdefault("evidence", "asserted")
                        self._record_fact(rel, rid, step.relation, src_key, dst_key, direction)
                        if rel_spec is not None:
                            self.facts[self._fact_id(rel, rid)]["temporal"] = rel_spec.temporal
                        if is_new:
                            visited.add(ident)
                            reached.append(ident)
                            next_frontier.append(ident)
                            state["reached_via"].append(
                                {
                                    "step": step_no,
                                    "relation": step.relation,
                                    "direction": direction,
                                    "depth": depth,
                                    "from": src_key,
                                }
                            )
            frontier = next_frontier
        self.filtered[step_no] = len(filtered)
        return reached

    @staticmethod
    def _fact_id(rel: dict[str, Any], rid: Any) -> str:
        return str(rel.get("fact_id") or f"edge:{rid}")

    def _record_fact(
        self,
        rel: dict[str, Any],
        rid: Any,
        relation: str,
        src_key: str,
        dst_key: str,
        direction: str,
    ) -> None:
        fid = self._fact_id(rel, rid)
        if fid in self.facts:
            return
        subject, obj = (src_key, dst_key) if direction == "out" else (dst_key, src_key)
        snapshot = None
        if rel.get("snapshot_id"):
            snapshot = {
                "source": str(rel.get("snapshot_source") or ""),
                "scope": str(rel.get("snapshot_scope") or ""),
                "snapshot_id": str(rel["snapshot_id"]),
            }
        self.facts[fid] = {
            "fact_id": fid,
            "relation": relation,
            "subject": subject,
            "object": obj,
            "valid_from": rel.get("valid_from") or None,
            "valid_to": rel.get("valid_to"),
            "evidence": rel.get("evidence") or "asserted",
            "confidence": rel.get("confidence"),
            "attributes": rel.get("attributes") if isinstance(rel.get("attributes"), dict) else {},
            "source_path": str(rel.get("source_path") or ""),
            "supersedes": rel.get("supersedes"),
            "snapshot": snapshot,
        }


def compile_typed_context(
    settings: Settings,
    request: TypedContextRequest | dict[str, Any],
    *,
    embedder: Embedder | None = None,
    conn: psycopg.Connection | None = None,
) -> dict[str, Any]:
    """Собрать типизированный пакет контекста (см. docstring модуля)."""
    req = TypedContextRequest.from_payload(request) if isinstance(request, dict) else request
    req.namespaces = resolve_namespaces(req.namespaces, settings.default_namespace)
    allowed = (
        None
        if req.allowed_scopes is None
        else resolve_scopes(req.allowed_scopes, limit=MAX_ALLOWED_SCOPES)
    )
    trace_id = f"ctx-{uuid.uuid4().hex[:16]}"
    t0 = time.monotonic()
    phases = timing.Phases()

    own_conn = conn is None
    with phases("connect"):
        conn = conn or dbmod.connect(settings, autocommit=True)
    try:
        with phases("setup"):
            graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
            dbmod.ensure_graph(conn, graph.graph)
            registry = open_registry(conn, settings)
            catalogs = {ns: registry.catalog_for(ns) for ns in req.namespaces}
            ledger = SnapshotLedger(conn, settings.snapshots_table)
            compiler = _Compiler(
                graph, ledger if ledger.table_exists() else None, catalogs, req, allowed
            )
        compiler.phases = phases

        state: dict[str, Any] = {"embedder": embedder, "index": None, "entities": None}
        vectors: dict[str, list[float]] = {}

        def _semantic(value: str, ns: str, kinds: set[str] | None):
            """Смысловой поиск якоря: индекс сущностей и фрагменты (см. _Compiler._semantic)."""
            if state["index"] is None:
                state["index"] = VectorIndex(
                    conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
                )
                ents = entity_index(conn, settings)
                state["entities"] = ents if ents.table_exists() else False
            index, ents = state["index"], state["entities"]
            has_chunks = index.table_exists()
            if not has_chunks and not ents:
                return [], []
            if value not in vectors:
                state["embedder"] = state["embedder"] or build_embedder(settings)
                vectors[value] = state["embedder"].embed_one(value)
            entity_hits = []
            if ents:
                searchable = catalogs[ns].searchable_kinds()
                wanted = set(searchable) if kinds is None else set(searchable) & kinds
                if wanted:
                    entity_hits = [
                        ((h.row.kind, h.row.natural_key), h.score)
                        for h in ents.search(
                            vectors[value],
                            req.semantic_k,
                            namespaces=[ns],
                            kinds=wanted,
                            allowed_scopes=allowed,
                        )
                    ]
            chunk_hits = []
            if has_chunks:
                found = index.search(
                    vectors[value], value, k=req.semantic_k, namespaces=[ns], allowed_scopes=allowed
                )
                sims = index.similarities(vectors[value], [h.chunk_id for h in found])
                chunk_hits = [((None, h.node_key), sims.get(h.chunk_id)) for h in found]
            return entity_hits, chunk_hits

        with phases("anchors"):
            anchors = compiler.resolve_anchors(req.anchors, _semantic)
        anchor_ids = [(r["namespace"], r["natural_key"]) for a in anchors for r in a["resolved"]]
        anchor_ids = list(dict.fromkeys(anchor_ids))

        steps_report: list[dict[str, Any]] = []
        previous = anchor_ids
        for i, step in enumerate(req.traverse):
            start = anchor_ids if step.start == "anchors" else previous
            with phases("traverse"):
                reached = compiler.traverse(i, step, start)
            report: dict[str, Any] = {
                "relation": step.relation,
                "direction": step.direction,
                "depth": step.depth,
                "limit": step.limit,
                "from": step.start,
                "reached": len(reached),
            }
            if step.where:
                report["filtered"] = compiler.filtered.get(i, 0)
            if i in compiler.edges_truncated:
                # Рёбер уровня больше лимита чтения — часть связей не рассмотрена.
                report["truncated"] = True
            steps_report.append(report)
            previous = reached

        assemble = time.perf_counter()
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for ent in compiler.entities.values():
            ent.setdefault("evidence", "asserted")
            ent.setdefault("anchor", False)
            by_kind.setdefault(ent["kind"], []).append(ent)
        sections = [
            {
                "kind": kind,
                "items": sorted(items, key=lambda e: (e["namespace"], e["natural_key"])),
            }
            for kind, items in sorted(by_kind.items())
        ]
        facts = sorted(compiler.facts.values(), key=lambda f: f["fact_id"])

        snapshots: list[dict[str, str]] = []
        for item in [*compiler.entities.values(), *facts]:
            snap = item.get("snapshot")
            if snap and snap not in snapshots:
                snapshots.append(snap)
        snapshots.sort(key=lambda s: (s["source"], s["snapshot_id"]))

        sources: list[dict[str, str]] = []
        seen_src: set[tuple[str, str]] = set()
        for section in sections:
            for ent in section["items"]:
                key = (ent["source_path"], ent["natural_key"])
                if ent["source_path"] and key not in seen_src:
                    seen_src.add(key)
                    sources.append(
                        {
                            "source_path": ent["source_path"],
                            "node_key": ent["natural_key"],
                            "title": ent["title"],
                        }
                    )

        pack = {
            "as_of": req.as_of,
            "namespaces": req.namespaces,
            "anchors": anchors,
            "unresolved": [a["input"] for a in anchors if not a["resolved"]],
            "sections": sections,
            "facts": facts,
            "used": {
                "entities": [
                    {"namespace": ns, "natural_key": key} for ns, key in compiler.entities
                ],
                "facts": [f["fact_id"] for f in facts],
                "snapshots": snapshots,
            },
            "sources": sources,
            "trace_id": trace_id,
            "stats": {
                "entities": len(compiler.entities),
                "facts": len(facts),
                "steps": steps_report,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            },
        }
        phases.add("assemble", time.perf_counter() - assemble)

        trace_t0 = time.perf_counter()
        try:
            traces = ContextTraceStore(
                conn, settings.context_traces_table, settings.default_namespace
            )
            traces.ensure_schema()
            traces.record(
                trace_id,
                req.namespaces[0],
                request={
                    "mode": "typed",
                    "anchors": [a["input"] for a in anchors],
                    "traverse": [
                        {**report, "where": [c.to_dict() for c in step.where]}
                        if step.where
                        else report
                        for report, step in zip(steps_report, req.traverse, strict=True)
                    ],
                    "as_of": req.as_of,
                    "namespaces": req.namespaces,
                    "allow_semantic": req.allow_semantic,
                    **({"where": [c.to_dict() for c in req.where]} if req.where else {}),
                },
                decisions={"anchors": anchors, "used": pack["used"], "stats": pack["stats"]},
            )
        except psycopg.Error:
            pack["stats"]["trace_error"] = True
        phases.add("trace", time.perf_counter() - trace_t0)
        # Фазы — в ответе (после записи трейса: его длительность тоже видна) и в
        # наборе HTTP-запроса (Server-Timing при CB_SERVER_TIMING).
        pack["stats"]["phases_ms"] = phases.rounded()
        timing.merge(phases, "typed.")
        return pack
    finally:
        if own_conn:
            conn.close()
