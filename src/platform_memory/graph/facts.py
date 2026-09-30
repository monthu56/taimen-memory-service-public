"""FactStore — temporal-факты с supersession поверх графа AGE (ADR-016 §4).

Факт — ребро с уникальным ``fact_id``: несколько рёбер одного типа между одной
парой узлов сосуществуют, различаясь validity-интервалами. Историю не удаляем:
supersession закрывает ``valid_to`` старого факта и связывает его с новым через
``superseded_by``. Времена — строки ISO-8601 UTC (лексикографически сравнимы).

Классы свидетельства (``evidence``): asserted > extracted > inferred > derived —
ranking/audit-сигнал, не математическая истина (ADR-016 §11).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from typing import Any

import psycopg

from platform_memory.core import db as dbmod
from platform_memory.core.db import agtype
from platform_memory.core.models import Node, Provenance
from platform_memory.core.namespaces import resolve_namespace, resolve_namespaces
from platform_memory.core.observations import EVIDENCE_ASSERTED, EVIDENCE_CLASSES
from platform_memory.core.ontology import sanitize_label
from platform_memory.core.scopes import (
    ForeignObjectError,
    check_write_scopes,
    is_visible,
    merge_scopes,
)
from platform_memory.graph.store import GraphStore

# Ранг силы свидетельства для ranking/конфликтов (больше — сильнее).
EVIDENCE_RANK = {"asserted": 3, "extracted": 2, "inferred": 1, "derived": 1}


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_ts(value: str | None) -> str | None:
    """Нормализовать метку времени факта в ISO-8601 UTC с точностью до секунды.

    Лексикографическое сравнение временных строк корректно ТОЛЬКО в одном
    формате: смещение ``+03:00`` или микросекунды ломают порядок (``.5Z`` < ``Z``).
    Невалидная строка -> ValueError (тихая порча temporal-фильтров хуже отказа).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def fact_id_for(namespace: str, subject: str, predicate: str, obj: str, valid_from: str) -> str:
    """Детерминированный id факта: повторная доставка того же утверждения не дублирует."""
    raw = f"{namespace}|{subject}|{predicate}|{obj}|{valid_from}"
    return f"fact-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


class FactStore:
    """Temporal-факты: assert/invalidate/supersede и выборки с ``as_of``."""

    def __init__(self, graph: GraphStore):
        """Работает поверх GraphStore (то же соединение, граф и default-namespace)."""
        self.store = graph
        self.conn = graph.conn
        self.graph = graph.graph

    def _ns(self, namespace: str) -> str:
        return resolve_namespace(namespace, self.store.default_namespace)

    # --- узлы-концы ---

    def ensure_entity(
        self,
        natural_key: str,
        *,
        entity_type: str = "entity",
        title: str = "",
        properties: dict[str, Any] | None = None,
        namespace: str = "",
        source_path: str = "",
        scopes: Sequence[str] = (),
        check_kind: bool = True,
        allowed_scopes: Sequence[str] | None = None,
    ) -> str:
        """Идемпотентно создать узел-сущность (origin='agent'); вернуть natural_key.

        Проекция assertions не должна зависеть от порядка: конец факта создаётся
        placeholder'ом, если entity-assertion ещё не приезжал. Вид проверяется
        kind-guard'ом стора (строгий режим namespace, MEM-ADR-020); ``check_kind=False``
        — для контентных узлов (текст наблюдения), которые не являются сущностями.
        Существующий узел вне видимости ``allowed_scopes`` не перезаписывается
        (:class:`ForeignObjectError`), scopes видимого сливаются (MEM-ADR-019).
        """
        ns = self._ns(namespace)
        if check_kind and self.store.kind_guard is not None:
            entity_type = self.store.kind_guard(
                ns, entity_type or "entity", natural_key, properties
            )
        props = dict(properties or {})
        if scopes:
            props["scopes"] = list(scopes)
        self.store.upsert_node(
            Node(
                type=entity_type or "entity",
                natural_key=natural_key,
                title=title or natural_key,
                properties=props,
                origin="agent",
                namespace=ns,
                provenance=Provenance(
                    source_path=source_path or "memory:observation",
                    confidence=1.0,
                    last_seen=_now_iso(),
                ),
            ),
            guard_scopes=True,
            allowed_scopes=allowed_scopes,
        )
        return natural_key

    # --- запись фактов ---

    def assert_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        *,
        namespace: str = "",
        observed_at: str = "",
        valid_from: str = "",
        valid_to: str | None = None,
        evidence: str = EVIDENCE_ASSERTED,
        confidence: float = 1.0,
        observation_id: str = "",
        supersedes: str = "",
        scopes: Sequence[str] = (),
        source_path: str = "",
        allowed_scopes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Записать temporal-факт; вернуть {fact_id, reinforced, superseded}.

        Идемпотентно по (namespace, subject, predicate, obj, valid_from): повтор
        того же утверждения усиливает факт (добавляет observation_id, поднимает
        confidence до максимума), а не дублирует ребро. ``supersedes`` в той же
        транзакции закрывает валидность старого факта. Историю не удаляет.

        ``allowed_scopes`` — видимость пишущего (None — без ограничения, MEM-ADR-019):
        конец факта или существующий факт вне неё — :class:`ForeignObjectError`, scopes
        видимости записи — только из неё; scopes повторённого факта сливаются
        (:func:`merge_scopes`), а не заменяются. Невидимый ``supersedes`` не
        закрывается — как отсутствующий.
        """
        if evidence not in EVIDENCE_CLASSES:
            raise ValueError(f"Неизвестный класс свидетельства {evidence!r}")
        ns = self._ns(namespace)
        # Temporal-поля нормализуются к единому формату ДО вычисления fact_id:
        # '13:00+03:00' и '10:00Z' — одно утверждение, id обязан совпасть.
        observed_at = normalize_ts(observed_at) or ""
        valid_from = normalize_ts(valid_from) or ""
        valid_to = normalize_ts(valid_to)
        etype = sanitize_label(predicate).upper()
        fid = fact_id_for(ns, subject, predicate, obj, valid_from)
        confidence = max(0.0, min(1.0, float(confidence)))

        check_write_scopes(scopes, allowed_scopes)
        params: dict[str, Any] = {
            "src": subject,
            "dst": obj,
            "ns": ns,
            "fid": fid,
            "observed": observed_at or _now_iso(),
            "vf": valid_from,
            "vt": valid_to,
            "ev": evidence,
            "conf": confidence,
            "obs": [observation_id] if observation_id else [],
            "sup": supersedes or None,
            "sp": source_path or "memory:observation",
        }
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (a {{natural_key: $src, namespace: $ns}}), "
            f"(b {{natural_key: $dst, namespace: $ns}}) "
            f"MERGE (a)-[r:{etype} {{fact_id: $fid}}]->(b) "
            f"SET r.namespace=$ns, r.observed_at=$observed, r.valid_from=$vf, r.valid_to=$vt, "
            f"r.evidence=$ev, r.confidence=$conf, r.observation_ids=$obs, r.supersedes=$sup, "
            f"r.source_path=$sp, r.origin='agent' "
            f"RETURN id(r) $$, %s) AS (id agtype)"
        )
        superseded = False
        with self.conn.transaction():
            # Advisory-лок сериализует конкурентные MERGE того же факта (гонки AGE).
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{self.graph}:fact:{fid}",),
                )
            for key in (subject, obj):
                if not self.store.key_visible(key, ns, allowed_scopes):
                    raise ForeignObjectError(key)
            # Существующий факт читается под локом: проверка и слияние без гонки.
            existing = self.get_fact(fid, namespaces=[ns])
            if existing:
                if not is_visible(_fact_scopes(existing), allowed_scopes):
                    raise ForeignObjectError(fid)
                # Reinforcement: тот же факт из нового наблюдения (ADR-016 §26).
                obs_ids = list(existing.get("observation_ids") or [])
                if observation_id and observation_id not in obs_ids:
                    obs_ids.append(observation_id)
                params["obs"] = obs_ids
                params["conf"] = max(float(existing.get("confidence") or 0.0), confidence)
                scopes = merge_scopes(_fact_scopes(existing), scopes)
            if scopes:
                params["scopes"] = list(scopes)
                scope_sql = sql.replace("r.origin='agent' ", "r.origin='agent', r.scopes=$scopes ")
            else:
                scope_sql = sql
            with self.conn.cursor() as cur:
                cur.execute(scope_sql, (agtype(params),))
                row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"Концы факта не найдены в namespace {ns!r}: {subject!r} -> {obj!r}"
                )
            if (
                supersedes
                and supersedes != fid
                and self._fact_visible(supersedes, ns, allowed_scopes)
            ):
                superseded = self._close_fact(
                    supersedes,
                    ns,
                    valid_to=valid_from or params["observed"],
                    superseded_by=fid,
                )
        return {"fact_id": fid, "reinforced": bool(existing), "superseded": superseded}

    def _fact_visible(self, fact_id: str, ns: str, allowed_scopes: Sequence[str] | None) -> bool:
        """Виден ли факт ``allowed_scopes`` (нет факта — True: закрывать нечего)."""
        if allowed_scopes is None:
            return True
        fact = self.get_fact(fact_id, namespaces=[ns])
        return fact is None or is_visible(_fact_scopes(fact), allowed_scopes)

    # --- батчевые операции сверки снимков (MEM-ADR-020) ---

    def create_facts(
        self,
        relation: str,
        from_kind: str,
        to_kind: str,
        rows: Sequence[dict[str, Any]],
        *,
        namespace: str,
        observed_at: str,
    ) -> set[str]:
        """Создать рёбра-факты одной связи между узлами заданных видов (UNWIND).

        ``rows`` — ``{fid, s, d, vf, sup, sp, a, src, scope, sid, scopes}``: концы,
        fact_id, ``valid_from``, supersedes, source_path, атрибуты связи, снимок-источник
        и scopes видимости (MEM-ADR-019).
        fact_id у сверки уникален для версии (включает источник и снимок), поэтому
        ребро создаётся (CREATE), а не сливается: закрытое ребро не переоткрывается.
        Вернуть множество созданных fact_id (конец не найден — ребра нет).
        """
        if not rows:
            return set()
        ns = self._ns(namespace)
        etype = sanitize_label(relation).upper()
        la, lb = sanitize_label(from_kind), sanitize_label(to_kind)
        self.store.ensure_label_index(etype, edge=True)
        params: dict[str, Any] = {
            "rows": list(rows),
            "ns": ns,
            "obs": normalize_ts(observed_at) or _now_iso(),
        }
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"UNWIND $rows AS row "
            f"MATCH (a:{la} {{natural_key: row.s, namespace: $ns}}), "
            f"(b:{lb} {{natural_key: row.d, namespace: $ns}}) "
            f"CREATE (a)-[r:{etype} {{fact_id: row.fid}}]->(b) "
            f"SET r.namespace=$ns, r.observed_at=$obs, r.valid_from=row.vf, "
            f"r.evidence='asserted', r.confidence=1.0, r.observation_ids=[], "
            f"r.supersedes=row.sup, r.source_path=row.sp, r.origin='agent', "
            f"r.attributes=row.a, r.snapshot_source=row.src, r.snapshot_scope=row.scope, "
            f"r.snapshot_id=row.sid, r.scopes=row.scopes "
            f"RETURN r.fact_id $$, %s) AS (fid agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            got = cur.fetchall()
        return {str(dbmod.parse_agtype(r[0])) for r in got}

    def close_facts(self, relation: str, rows: Sequence[dict[str, Any]], *, namespace: str) -> int:
        """Закрыть рёбра-факты одной связи (UNWIND): ``rows`` — ``{fid, vt, nxt}``."""
        if not rows:
            return 0
        etype = sanitize_label(relation).upper()
        params: dict[str, Any] = {"rows": list(rows), "ns": self._ns(namespace)}
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"UNWIND $rows AS row "
            f"MATCH ()-[r:{etype} {{fact_id: row.fid, namespace: $ns}}]->() "
            f"SET r.valid_to = row.vt, r.superseded_by = row.nxt "
            f"RETURN count(r) $$, %s) AS (c agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            row = cur.fetchone()
        return int(dbmod.parse_agtype(row[0]) or 0) if row else 0

    def _close_fact(
        self, fact_id: str, ns: str, *, valid_to: str, superseded_by: str | None = None
    ) -> bool:
        """Закрыть validity-интервал факта (не удаляя его). True — если факт найден."""
        params: dict[str, Any] = {
            "fid": fact_id,
            "ns": ns,
            "vt": normalize_ts(valid_to) or _now_iso(),
            "by": superseded_by,
        }
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH ()-[r {{fact_id: $fid, namespace: $ns}}]->() "
            f"SET r.valid_to = $vt, r.superseded_by = $by "
            f"RETURN id(r) $$, %s) AS (id agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            return cur.fetchone() is not None

    def invalidate_fact(
        self,
        fact_id: str,
        *,
        namespace: str = "",
        valid_to: str = "",
        observation_id: str = "",
    ) -> bool:
        """Закрыть факт без замены («Alice больше не работает над X»). История остаётся."""
        ns = self._ns(namespace)
        closed = self._close_fact(fact_id, ns, valid_to=valid_to or _now_iso())
        if closed and observation_id:
            existing = self.get_fact(fact_id, namespaces=[ns]) or {}
            obs_ids = list(existing.get("observation_ids") or [])
            if observation_id not in obs_ids:
                obs_ids.append(observation_id)
                self._set_observation_ids(fact_id, ns, obs_ids)
        return closed

    def _set_observation_ids(self, fact_id: str, ns: str, observation_ids: list[str]) -> None:
        params: dict[str, Any] = {"fid": fact_id, "ns": ns, "obs": observation_ids}
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM cypher('{self.graph}', $$ "
                f"MATCH ()-[r {{fact_id: $fid, namespace: $ns}}]->() "
                f"SET r.observation_ids = $obs RETURN id(r) $$, %s) AS (id agtype)",
                (agtype(params),),
            )

    def scrub_derived_nodes(
        self, observation_id: str, namespace: str = "", *, purge: bool = False
    ) -> int:
        """Зачистить узлы, порождённые наблюдением (props.observation_id == oid).

        Содержимое наблюдения не должно переживать его redact в производном
        узле (ADR-016 §8): props.content затирается, узел помечается
        ``redacted``; при ``purge=True`` узел удаляется целиком (DETACH DELETE).
        Вернуть число затронутых узлов.
        """
        ns = self._ns(namespace)
        params: dict[str, Any] = {"oid": observation_id, "ns": ns}
        action = (
            "DETACH DELETE n" if purge else "SET n.props = {observation_id: $oid, redacted: true}"
        )
        count_sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (n {{namespace: $ns}}) WHERE n.props.observation_id = $oid "
            f"RETURN count(n) $$, %s) AS (c agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(count_sql, (agtype(params),))
            affected = int(dbmod.parse_agtype(cur.fetchone()[0]) or 0)
            if affected:
                cur.execute(
                    f"SELECT * FROM cypher('{self.graph}', $$ "
                    f"MATCH (n {{namespace: $ns}}) WHERE n.props.observation_id = $oid "
                    f"{action} $$, %s) AS (v agtype)",
                    (agtype(params),),
                )
        return affected

    def mark_evidence_lost(self, observation_id: str, namespace: str = "") -> int:
        """Отразить удаление наблюдения на derived-фактах (ADR-016 §8).

        Из ``observation_ids`` каждого факта вычёркивается id; факт, оставшийся
        без свидетельств, получает ``evidence_lost=true`` (retrieval по умолчанию
        его не отдаёт; из графа не удаляется — аудит). Вернуть число фактов,
        потерявших последнее свидетельство.
        """
        ns = self._ns(namespace)
        affected = self.facts_for_observation(observation_id, namespaces=[ns])
        lost = 0
        for fact in affected:
            obs_ids = [o for o in (fact.get("observation_ids") or []) if o != observation_id]
            fid = str(fact.get("fact_id"))
            with self.conn.transaction():
                self._set_observation_ids(fid, ns, obs_ids)
                if not obs_ids:
                    params: dict[str, Any] = {"fid": fid, "ns": ns}
                    with self.conn.cursor() as cur:
                        cur.execute(
                            f"SELECT * FROM cypher('{self.graph}', $$ "
                            f"MATCH ()-[r {{fact_id: $fid, namespace: $ns}}]->() "
                            f"SET r.evidence_lost = true RETURN id(r) $$, %s) AS (id agtype)",
                            (agtype(params),),
                        )
                    lost += 1
        return lost

    # --- чтение ---

    _RETURN = "RETURN a.natural_key, b.natural_key, type(r), properties(r), a.title, b.title "

    def _rows_to_facts(self, rows: list[tuple]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            props = dbmod.parse_agtype(row[3])
            if not isinstance(props, dict):
                props = {}
            fact = {
                # AGE не хранит null-свойства (SET r.x = null удаляет ключ) —
                # нормализуем опциональные поля, чтобы потребитель видел None.
                "valid_to": None,
                "supersedes": None,
                "superseded_by": None,
                "subject": dbmod.parse_agtype(row[0]),
                "object": dbmod.parse_agtype(row[1]),
                "predicate": dbmod.parse_agtype(row[2]),
                "subject_title": dbmod.parse_agtype(row[4]),
                "object_title": dbmod.parse_agtype(row[5]),
                **props,
            }
            out.append(fact)
        return out

    def get_fact(self, fact_id: str, namespaces: Sequence[str] = ()) -> dict[str, Any] | None:
        """Вернуть факт по fact_id в области видимости, либо None."""
        nss = resolve_namespaces(namespaces, self.store.default_namespace)
        params: dict[str, Any] = {"fid": fact_id, "nss": nss}
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (a)-[r {{fact_id: $fid}}]->(b) WHERE r.namespace IN $nss "
            f"{self._RETURN}$$, %s) AS (s agtype, o agtype, p agtype, r agtype, st agtype, "
            f"ot agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            rows = cur.fetchall()
        facts = self._rows_to_facts(rows)
        return facts[0] if facts else None

    def facts(
        self,
        *,
        subject: str = "",
        predicate: str = "",
        obj: str = "",
        namespaces: Sequence[str] = (),
        as_of: str = "",
        include_closed: bool = False,
        include_evidence_lost: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Выборка фактов вокруг узла с temporal-фильтром.

        ``as_of`` — история на момент времени (valid_from <= t < valid_to);
        пусто — действующие сейчас факты. ``include_closed=True`` отдаёт и
        закрытые интервалы (полная история). Факты с ``evidence_lost`` по
        умолчанию скрыты (ADR-016 §8).
        """
        nss = resolve_namespaces(namespaces, self.store.default_namespace)
        match = (
            f"MATCH (a)-[r:{sanitize_label(predicate).upper()}]->(b)"
            if predicate
            else ("MATCH (a)-[r]->(b)")
        )
        conds = ["r.fact_id IS NOT NULL", "r.namespace IN $nss"]
        params: dict[str, Any] = {"nss": nss}
        if subject:
            conds.append("a.natural_key = $subj")
            params["subj"] = subject
        if obj:
            conds.append("b.natural_key = $obj")
            params["obj"] = obj
        if not include_evidence_lost:
            conds.append("(r.evidence_lost IS NULL OR r.evidence_lost = false)")
        if as_of:
            params["t"] = normalize_ts(as_of)
            conds.append("(r.valid_from IS NULL OR r.valid_from = '' OR r.valid_from <= $t)")
            conds.append("(r.valid_to IS NULL OR r.valid_to > $t)")
        elif not include_closed:
            conds.append("(r.valid_to IS NULL)")
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"{match} WHERE {' AND '.join(conds)} "
            f"{self._RETURN}$$, %s) AS (s agtype, o agtype, p agtype, r agtype, st agtype, "
            f"ot agtype) LIMIT {int(limit)}"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            rows = cur.fetchall()
        return self._rows_to_facts(rows)

    def facts_for_observation(
        self, observation_id: str, namespaces: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Все факты, среди свидетельств которых есть данное наблюдение."""
        nss = resolve_namespaces(namespaces, self.store.default_namespace)
        params: dict[str, Any] = {"oid": observation_id, "nss": nss}
        sql = (
            f"SELECT * FROM cypher('{self.graph}', $$ "
            f"MATCH (a)-[r]->(b) "
            f"WHERE r.fact_id IS NOT NULL AND r.namespace IN $nss "
            f"AND $oid IN r.observation_ids "
            f"{self._RETURN}$$, %s) AS (s agtype, o agtype, p agtype, r agtype, st agtype, "
            f"ot agtype)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (agtype(params),))
            rows = cur.fetchall()
        return self._rows_to_facts(rows)

    @staticmethod
    def find_conflicts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Пометить конфликты в выборке фактов (чистая функция, БД не нужна).

        Конфликт: два факта одного (subject, predicate) с разными объектами и
        пересекающимися validity-интервалами, ни один не superseded другим.
        Помеченные факты получают ``conflict: true`` и ``conflicts_with`` —
        retrieval знает о конфликте, разрешение остаётся потребителю.
        """
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for fact in facts:
            key = (str(fact.get("subject")), str(fact.get("predicate")))
            groups.setdefault(key, []).append(fact)

        def _overlap(x: dict[str, Any], y: dict[str, Any]) -> bool:
            xf, xt = str(x.get("valid_from") or ""), x.get("valid_to")
            yf, yt = str(y.get("valid_from") or ""), y.get("valid_to")
            xt_s = str(xt) if xt else "￿"  # открытый интервал > любой даты
            yt_s = str(yt) if yt else "￿"
            return xf < yt_s and yf < xt_s

        for group in groups.values():
            if len(group) < 2:
                continue
            for i, x in enumerate(group):
                for y in group[i + 1 :]:
                    if str(x.get("object")) == str(y.get("object")):
                        continue
                    if x.get("superseded_by") == y.get("fact_id"):
                        continue
                    if y.get("superseded_by") == x.get("fact_id"):
                        continue
                    if _overlap(x, y):
                        for fact, other in ((x, y), (y, x)):
                            fact["conflict"] = True
                            lst = fact.setdefault("conflicts_with", [])
                            fid = other.get("fact_id")
                            if fid and fid not in lst:
                                lst.append(fid)
        return facts


def _fact_scopes(fact: dict[str, Any]) -> list[str]:
    """Scopes факта (свойство ребра ``scopes``; нет или не список — пусто)."""
    raw = fact.get("scopes")
    return [str(s) for s in raw] if isinstance(raw, list | tuple) else []


def _connect_stores(settings) -> tuple[psycopg.Connection, GraphStore, FactStore]:
    """Служебная фабрика для CLI/скриптов: соединение + графовые сторы."""
    conn = dbmod.connect(settings, autocommit=True)
    graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
    return conn, graph, FactStore(graph)
