# Адаптировано из graphify/dedup.py (https://github.com/safishamsi/graphify), MIT License.
# Copyright (c) 2026 Safi Shamsi. See THIRD_PARTY.md for the full license text.
#
# Перенесён алгоритм и эвристики (нормализация, энтропийный гейт, MinHash/LSH-блокинг,
# Jaro-Winkler, variant/short-label-гарды, union-find, буст по общей community), но
# переписан под наши модели (`Node`) и под политику Company Brain:
#   * natural keys (BIZ-/T-/Q-/M-id, ИНН, email, телефон) — авторитетная идентичность
#     и имеют приоритет: две по-разному заякоренные сущности НИКОГДА не сливаются;
#   * fuzzy-слой лишь «добивает» свободные сущности (люди/орги из транскриптов и писем);
#   * спорные слияния отдаются как кандидаты на арбитраж, а не авто-мержатся
#     (в graphify этот зазор закрывал LLM-tiebreaker — у нас это решает человек/политика).
"""Fuzzy entity resolution поверх natural-key резолвера ([[resolver]]).

`resolver.py` разрешает *ссылки* (LinkRef → natural_key существующего узла) по
стабильным ручкам. Этот модуль разрешает *идентичность сущностей*: какие свободные
узлы (без авторитетного natural-id) на самом деле один и тот же человек/организация,
по-разному названные в разных источниках.

Пайплайн (как в graphify): точная нормализация → энтропийный гейт → MinHash/LSH-блокинг
→ верификация Jaro-Winkler → буст за общую community → union-find. Поверх — гард
авторитетной идентичности: узлы с конфликтующими natural-ключами не объединяются ни
авто, ни в кандидаты.

Узлы-документы (проекции vault) сюда подавать НЕ нужно — у них уже есть авторитетный
natural_key (путь/id), и сходство заголовков не означает тождества. Слой предназначен
для свободных entity-узлов (person/org/…), которые появляются при извлечении сущностей
из текста.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz.distance import DamerauLevenshtein, JaroWinkler

from platform_memory.core.models import Node
from platform_memory.core.ontology import is_natural_id
from platform_memory.ingest._minhash import MinHash, MinHashLSH

# ── константы ───────────────────────────────────────────────────────────────────

_ENTROPY_THRESHOLD = 2.5
_LSH_THRESHOLD = 0.7
_AUTO_THRESHOLD = 95.0  # JW * 100: уверенный авто-мерж свободных сущностей
_ARBITRATION_LOW = 75.0  # [low, auto) → кандидат на арбитраж
_COMMUNITY_BOOST = 5.0  # бонус к скору, если оба узла в одной community
_NUM_PERM = 128

# Свойства frontmatter/extract, несущие авторитетную идентичность сущности.
_IDENTITY_PROP_KEYS = ("inn", "ogrn", "email", "phone", "telegram", "username")


# ── нормализация и гейты (перенесено из graphify) ───────────────────────────────


def _norm(label: str | None) -> str:
    """Lowercase + схлопывание не-буквенно-цифровых серий в пробел (Unicode-aware)."""
    if not isinstance(label, str):
        label = "" if label is None else str(label)
    label = unicodedata.normalize("NFKC", label)
    return re.sub(r"[\W_]+", " ", label.casefold(), flags=re.UNICODE).strip()


def _entropy(label: str) -> float:
    """Энтропия Шеннона (бит/символ) нормализованной метки."""
    s = _norm(label)
    if not s:
        return 0.0
    freq: dict[str, int] = defaultdict(int)
    for ch in s:
        freq[ch] += 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _shingles(text: str, k: int = 3) -> set[str]:
    """k-граммные символьные шинглы текста."""
    if len(text) < k:
        return {text}
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _make_minhash(text: str, num_perm: int = _NUM_PERM) -> MinHash:
    # Убираем пробелы, чтобы «оао сбербанк» и «оаосбербанк» делили шинглы.
    m = MinHash(num_perm=num_perm)
    for shingle in _shingles(text.replace(" ", "")):
        m.update(shingle.encode("utf-8"))
    return m


# Метки, чей последний токен — суффикс версии/варианта: цифры (опц. + буквы)
# или 2+ буквы; основа обязана заканчиваться буквой, чтобы обычные слова не ловились.
_VARIANT_SUFFIX = re.compile(r"^(.*[a-z])([0-9]+[a-z]*|[a-z]{2,})$")


def _is_variant_pair(a: str, b: str) -> bool:
    """True, если a и b — sibling-варианты (одна основа, разный суффикс)."""
    if a == b:
        return False
    if max(len(a), len(b)) >= 12:
        return False
    ma, mb = _VARIANT_SUFFIX.match(a), _VARIANT_SUFFIX.match(b)
    if not (ma and mb):
        return False
    return ma.group(1) == mb.group(1) and ma.group(2) != mb.group(2)


def _short_label_blocked(a: str, b: str, jw_score: float) -> bool:
    """Блокировать fuzzy-мерж коротких меток, кроме равнодлинной однобуквенной замены.

    Вставки/удаления на коротких строках дают высокий Jaro-Winkler из-за префиксного
    бонуса, но почти никогда не означают дубликат — это сокращения/варианты.
    """
    if max(len(a), len(b)) >= 12:
        return False
    if jw_score >= 97.0 and len(a) == len(b) and DamerauLevenshtein.distance(a, b) <= 1:
        return False
    return True


# ── авторитетная идентичность ───────────────────────────────────────────────────


def identity_keys(node: Node) -> set[str]:
    """Множество авторитетных идентификаторов узла (natural-id + ИНН/email/телефон/…).

    Пустое множество → «свободная» сущность (кандидат на fuzzy-резолюцию). Непустое —
    «заякоренная»: её идентичность задана структурно и не выводится из похожести имени.
    """
    keys: set[str] = set()
    nk = node.natural_key.strip()
    if is_natural_id(nk):
        keys.add(nk.casefold())
    for prop in _IDENTITY_PROP_KEYS:
        val = node.properties.get(prop)
        if isinstance(val, str) and val.strip():
            keys.add(f"{prop}:{val.strip().casefold()}")
        elif isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, str) and item.strip():
                    keys.add(f"{prop}:{item.strip().casefold()}")
    return keys


# ── union-find с гардом авторитетной идентичности ───────────────────────────────


class _GuardedUF:
    """Union-find, который отказывается сливать компоненты с конфликтующей идентичностью.

    У каждого корня хранится объединение авторитетных идентификаторов его членов. Две
    компоненты, у которых обе идентичности непустые и НЕ пересекаются, считаются разными
    реальными сущностями — их union запрещён (natural keys имеют приоритет).
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._ids: dict[str, set[str]] = {}

    def add(self, x: str, ids: set[str]) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._ids[x] = set(ids)

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        self._ids.setdefault(x, set())
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def can_union(self, x: str, y: str) -> bool:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return True
        ix, iy = self._ids[rx], self._ids[ry]
        if ix and iy and ix.isdisjoint(iy):
            return False
        return True

    def union(self, x: str, y: str) -> bool:
        if not self.can_union(x, y):
            return False
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[ry] = rx
            self._ids[rx] |= self._ids[ry]
        return True

    def components(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            groups[self.find(x)].append(x)
        return dict(groups)


# ── результат ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class MergeCandidate:
    """Спорное слияние двух сущностей — на арбитраж человеку/политике (не авто-мерж)."""

    survivor: str  # предпочтительный «выживающий» natural_key
    duplicate: str  # natural_key второй сущности
    score: float  # 0..100 (Jaro-Winkler * 100, с учётом community-буста)
    reason: str  # "fuzzy" | "fuzzy+community"


@dataclass(slots=True)
class ResolutionResult:
    """Итог fuzzy-резолюции: авто-слияния и кандидаты на арбитраж."""

    merges: dict[str, str] = field(default_factory=dict)  # duplicate_key -> survivor_key
    candidates: list[MergeCandidate] = field(default_factory=list)
    groups: list[list[str]] = field(default_factory=list)  # компоненты авто-слияний (>1)

    @property
    def merged_count(self) -> int:
        """Сколько узлов будет поглощено авто-слияниями."""
        return len(self.merges)


# ── выбор «выживающего» ─────────────────────────────────────────────────────────


def _pick_survivor(nodes: list[Node]) -> Node:
    """Канонический выживающий: заякоренный > natural-id > более короткий ключ."""
    if not nodes:
        raise ValueError("Cannot pick survivor from empty list")

    def _score(n: Node) -> tuple[int, int, int]:
        anchored = 0 if identity_keys(n) else 1
        natural = 0 if is_natural_id(n.natural_key) else 1
        return (anchored, natural, len(n.natural_key))

    return min(nodes, key=_score)


# ── основной вход ───────────────────────────────────────────────────────────────


def resolve_entities(
    nodes: list[Node],
    *,
    communities: dict[str, int] | None = None,
    auto_threshold: float = _AUTO_THRESHOLD,
    arbitration_low: float = _ARBITRATION_LOW,
    forced_merges: list[tuple[str, str]] | None = None,
    blocked_pairs: set[frozenset[str]] | None = None,
) -> ResolutionResult:
    """Найти дубликаты среди свободных сущностей по нечёткому сходству имён.

    Args:
        nodes: список `Node` (entity-узлы person/org/…; документы сюда подавать не нужно).
        communities: отображение natural_key -> community_id (из cluster()); даёт буст
            к скору для пар в одной community.
        auto_threshold: скор ≥ него (или точное совпадение нормы) → авто-слияние.
        arbitration_low: [arbitration_low, auto_threshold) → кандидат на арбитраж.
        forced_merges: пары (a, b), одобренные арбитром — сливаются безусловно (решение
            человека важнее эвристик, перекрывает даже гард идентичности).
        blocked_pairs: пары frozenset({a, b}), отклонённые арбитром — никогда не сливаются
            и не попадают в кандидаты повторно (петля обратной связи арбитража).

    Returns:
        ResolutionResult с авто-слияниями (remap duplicate→survivor) и кандидатами.
        Узлы с конфликтующей авторитетной идентичностью не сливаются и не попадают в
        кандидаты.
    """
    communities = communities or {}
    blocked_pairs = blocked_pairs or set()

    # Дедуп по natural_key: оставляем первое вхождение.
    seen: dict[str, Node] = {}
    for node in nodes:
        if node.natural_key and node.natural_key not in seen:
            seen[node.natural_key] = node
    unique = list(seen.values())
    if len(unique) <= 1:
        return ResolutionResult()

    by_key = {n.natural_key: n for n in unique}
    norm_cache = {n.natural_key: _norm(n.title) for n in unique}

    uf = _GuardedUF()
    for node in unique:
        uf.add(node.natural_key, identity_keys(node))

    # ── проход −1: одобренные арбитром слияния (решение человека важнее эвристик) ─
    # Форсируем union напрямую через parent, минуя гард идентичности: арбитр явно сказал,
    # что это одна сущность.
    for a, b in forced_merges or []:
        if a in by_key and b in by_key:
            ra, rb = uf.find(a), uf.find(b)
            if ra != rb:
                uf._parent[rb] = ra  # noqa: SLF001 — намеренный форс поверх гарда
                uf._ids[ra] |= uf._ids[rb]

    # ── проход 0: общая авторитетная идентичность (natural keys имеют приоритет) ─
    # Узлы, делящие natural-id/ИНН/email/телефон — это одна сущность вне зависимости
    # от написания имени; сливаем их безусловно (это и есть natural-key резолюция).
    id_to_keys: dict[str, list[str]] = defaultdict(list)
    for node in unique:
        for ident in identity_keys(node):
            id_to_keys[ident].append(node.natural_key)
    for group in id_to_keys.values():
        base = group[0]
        for other in group[1:]:
            uf.union(base, other)

    # ── проход 1: точная нормализация заголовка ───────────────────────────────
    norm_to_keys: dict[str, list[str]] = defaultdict(list)
    for node in unique:
        key = norm_cache[node.natural_key]
        if key:
            norm_to_keys[key].append(node.natural_key)
    for group in norm_to_keys.values():
        if len(group) <= 1:
            continue
        base = group[0]
        for other in group[1:]:
            if frozenset({base, other}) in blocked_pairs:
                continue  # пара отклонена арбитром
            uf.union(base, other)  # гард сам отклонит конфликт идентичности

    # ── проход 2: MinHash/LSH-блокинг + Jaro-Winkler (только высокоэнтропийные) ─
    candidates: list[Node] = []
    for node in unique:
        key = norm_cache[node.natural_key]
        if key and _entropy(node.title) >= _ENTROPY_THRESHOLD:
            candidates.append(node)

    arbitration: list[MergeCandidate] = []
    if len(candidates) >= 2:
        lsh = MinHashLSH(threshold=_LSH_THRESHOLD, num_perm=_NUM_PERM)
        minhashes: dict[str, MinHash] = {}
        for node in candidates:
            nk = node.natural_key
            m = _make_minhash(norm_cache[nk])
            minhashes[nk] = m
            try:
                lsh.insert(nk, m)
            except ValueError:
                pass  # ключ уже вставлен

        for node in candidates:
            nk = node.natural_key
            norm_label = norm_cache[nk]
            for neighbor_id in lsh.query(minhashes[nk]):
                if neighbor_id == nk or uf.find(nk) == uf.find(neighbor_id):
                    continue
                neighbor = by_key.get(neighbor_id)
                if neighbor is None:
                    continue
                neighbor_norm = norm_cache[neighbor_id]
                score = JaroWinkler.normalized_similarity(norm_label, neighbor_norm) * 100

                if _is_variant_pair(norm_label, neighbor_norm):
                    continue
                if _short_label_blocked(norm_label, neighbor_norm, score):
                    continue
                # Префиксное расширение (parseConfig/parseConfigFile) почти никогда
                # не дубликат — блокируем независимо от скора.
                _lo, _hi = sorted((norm_label, neighbor_norm), key=len)
                if _hi.startswith(_lo) and _hi != _lo:
                    continue

                reason = "fuzzy"
                c1, c2 = communities.get(nk), communities.get(neighbor_id)
                if (
                    c1 is not None
                    and c2 is not None
                    and c1 == c2
                    and min(len(norm_label), len(neighbor_norm)) >= 12
                ):
                    score += _COMMUNITY_BOOST
                    reason = "fuzzy+community"

                # Конфликт авторитетной идентичности → не сливаем и не арбитрируем.
                if not uf.can_union(nk, neighbor_id):
                    continue
                # Пара отклонена арбитром → не сливаем и не предлагаем повторно.
                if frozenset({nk, neighbor_id}) in blocked_pairs:
                    continue

                if score >= auto_threshold:
                    uf.union(_pick_survivor([node, neighbor]).natural_key, nk)
                    uf.union(_pick_survivor([node, neighbor]).natural_key, neighbor_id)
                elif score >= arbitration_low:
                    surv = _pick_survivor([node, neighbor])
                    dup = neighbor if surv is node else node
                    arbitration.append(
                        MergeCandidate(
                            survivor=surv.natural_key,
                            duplicate=dup.natural_key,
                            score=round(score, 2),
                            reason=reason,
                        )
                    )

    # ── собрать remap из компонент union-find ─────────────────────────────────
    result = ResolutionResult()
    for members in uf.components().values():
        if len(members) <= 1:
            continue
        group_nodes = [by_key[m] for m in members if m in by_key]
        survivor = _pick_survivor(group_nodes)
        result.groups.append([n.natural_key for n in group_nodes])
        for node in group_nodes:
            if node.natural_key != survivor.natural_key:
                result.merges[node.natural_key] = survivor.natural_key

    # Кандидаты, чьи концы уже попали в одну авто-компоненту, отбрасываем.
    for cand in arbitration:
        if uf.find(cand.survivor) == uf.find(cand.duplicate):
            continue
        result.candidates.append(cand)

    return result


def apply_merges(
    nodes: list[Node],
    edges: list,
    merges: dict[str, str],
) -> tuple[list[Node], list]:
    """Применить remap авто-слияний: убрать поглощённые узлы, перенаправить рёбра.

    Рёбра, ставшие петлёй после перенаправления (src == dst), отбрасываются. Удобно
    для вызывающего, который сам решил применить `ResolutionResult.merges`.
    """
    if not merges:
        return nodes, edges
    out_nodes = [n for n in nodes if n.natural_key not in merges]
    out_edges = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        src = merges.get(edge.src, edge.src)
        dst = merges.get(edge.dst, edge.dst)
        if src == dst:
            continue
        key = (edge.type, src, dst)
        if key in seen:
            continue
        seen.add(key)
        edge.src, edge.dst = src, dst
        out_edges.append(edge)
    return out_nodes, out_edges
