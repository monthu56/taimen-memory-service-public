"""Построить граф концептов над демо-KB: узлы concept + рёбра (статья)-[COVERS]->(концепт).

Запускается ВНУТРИ контейнера memory-service (нужны движок + доступ к БД):
    docker cp demo/build_graph.py <serve>:/tmp/ && \
    docker exec -e DEMO_NAMESPACE=sea-demo <serve> python /tmp/build_graph.py

Зачем: витрина «проверяемой памяти» — граф знаний, а не плоский RAG. После посева
статьи были разрозненными узлами (соседей 0). Здесь мы связываем их через общие
концепты: статьи, делящие концепт, оказываются в 2 хопах (article→concept→article),
и retrieval может доставать релевантные статьи ПО ГРАФУ, а не только по вектору.

Концепт-узлы создаются БЕЗ чанков (только в графе) — они не попадают в векторный
поиск и не засоряют выдачу статей. Идемпотентно (upsert): повторный запуск не плодит
дублей. Пишет только в demo-namespace.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from platform_memory.core import db as dbmod
from platform_memory.core.config import get_settings
from platform_memory.core.models import Edge, Node, Provenance
from platform_memory.graph import GraphStore

# Управляемый словарь концептов: (slug, имя, ТИТУЛЬНЫЕ ключевые слова). Ключевые слова
# подобраны так, чтобы попадать в ЗАГОЛОВКИ статей (тема), а не в случайные упоминания в
# теле — иначе граф зашумляется ложными связями (VPN↔reimbursement через слово «approval»).
# Пересечения намеренны и осмысленны: VPN и MFA делят authentication и remote-access.
CONCEPTS: list[tuple[str, str, list[str]]] = [
    (
        "authentication",
        "Authentication",
        [
            "vpn",
            "mfa",
            "multi-factor",
            "password",
            "single sign-on",
            "sso",
            "authentication",
            "security key",
        ],
    ),
    (
        "remote-access",
        "Remote access",
        ["vpn", "remote work", "wi-fi", "wifi", "guest network", "secure remote"],
    ),
    (
        "device-management",
        "Device management",
        ["laptop", "device", "provisioning", "device encryption", "printer", "hardware"],
    ),
    (
        "incident-response",
        "Incident response",
        [
            "incident",
            "severity",
            "sev1",
            "sev-1",
            "on-call",
            "escalate",
            "escalation",
            "postmortem",
            "outage",
        ],
    ),
    (
        "access-provisioning",
        "Access provisioning",
        [
            "access provisioning",
            "deprovisioning",
            "joiners",
            "movers",
            "leavers",
            "access request",
            "least privilege",
        ],
    ),
    (
        "data-governance",
        "Data governance",
        [
            "data retention",
            "retention schedule",
            "data classification",
            "data subject access",
            "dsar",
            "audit logging",
            "records",
        ],
    ),
    (
        "data-security",
        "Data security",
        [
            "encryption",
            "pii",
            "data breach",
            "breach notification",
            "minimization",
            "acceptable use",
            "phishing",
        ],
    ),
    (
        "expenses-travel",
        "Expenses & travel",
        ["expense", "reimburse", "corporate card", "travel", "per diem"],
    ),
    (
        "procurement",
        "Procurement",
        ["purchase order", "vendor", "invoice", "budget", "procurement"],
    ),
    (
        "onboarding",
        "Onboarding",
        [
            "onboarding",
            "first-day",
            "first day",
            "new hire",
            "benefits enrollment",
            "referral program",
        ],
    ),
    (
        "hr-policy",
        "HR policy",
        [
            "leave",
            "pto",
            "performance review",
            "code of conduct",
            "remote work allowance",
            "reimbursement",
        ],
    ),
    (
        "api",
        "API",
        ["api", "rate limit", "webhook", "pagination", "error code", "sdk", "sandbox", "changelog"],
    ),
    (
        "sales",
        "Sales",
        ["lead", "bant", "discount", "renewal", "objection", "crm", "business review", "trial"],
    ),
    (
        "support",
        "Support",
        ["refund", "sla", "status page", "known-issue", "on-call handoff", "support"],
    ),
]

# Веса: попадание в ЗАГОЛОВОК — сильный сигнал темы; в тело — слабый. Концепт назначается
# только при существенном присутствии (порог), максимум 3 на статью — граф остаётся читаемым.
_TITLE_W, _BODY_W, _MIN_W, _MAX_CONCEPTS = 3, 1, 3, 3


def assign_concepts(title: str, content: str) -> list[str]:
    """Назначить до 3 концептов по взвешенным совпадениям (заголовок весомее тела)."""
    tl, bl = title.lower(), content.lower()
    scored: list[tuple[int, str]] = []
    for slug, _name, keywords in CONCEPTS:
        w = 0
        for kw in keywords:
            if kw in tl:
                w += _TITLE_W
            elif kw in bl:
                w += _BODY_W
        if w >= _MIN_W:  # порог: попадание в заголовок ИЛИ несколько в теле
            scored.append((w, slug))
    scored.sort(reverse=True)
    return [slug for _w, slug in scored[:_MAX_CONCEPTS]]


def main() -> int:
    ns = os.environ.get("DEMO_NAMESPACE", "sea-demo").strip() or "sea-demo"
    settings = get_settings()
    now = dt.datetime.now().isoformat()
    names = {slug: name for slug, name, _ in CONCEPTS}

    conn = dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        graph.ensure_schema()

        # Сброс: удаляем прежние концепт-узлы (каскадом уходят их рёбра COVERS) — чтобы
        # пересборка давала чистый граф без устаревших/шумных связей. Статьи не трогаем.
        old = [n for n in graph.list_nodes(type="concept", limit=500, namespaces=[ns])]
        for n in old:
            try:
                graph.delete_node(n.get("natural_key", ""), namespace=ns)
            except Exception:  # noqa: BLE001
                pass
        if old:
            print(f"Сброшено прежних концептов: {len(old)}")

        articles = [
            n for n in graph.list_nodes(limit=500, namespaces=[ns]) if n.get("type") == "article"
        ]
        print(f"Статей в '{ns}': {len(articles)}")

        # 1) концепт-узлы (без чанков — только граф)
        used: set[str] = set()
        assignments: list[tuple[str, list[str]]] = []
        for a in articles:
            key = a.get("natural_key", "")
            props = a.get("props") if isinstance(a.get("props"), dict) else {}
            concepts = assign_concepts(a.get("title", ""), props.get("content", ""))
            assignments.append((key, concepts))
            used.update(concepts)

        for slug in sorted(used):
            graph.upsert_node(
                Node(
                    type="concept",
                    natural_key=f"concept:{slug}",
                    title=names.get(slug, slug),
                    properties={"slug": slug},
                    origin="agent",
                    namespace=ns,
                    provenance=Provenance(
                        source_path="demo:graph-builder", confidence=1.0, last_seen=now
                    ),
                )
            )
        print(f"Концепт-узлов: {len(used)}")

        # 2) рёбра статья -[COVERS]-> концепт (к уже существующим узлам)
        made = skipped = 0
        for key, concepts in assignments:
            for slug in concepts:
                ok = graph.upsert_edge(
                    Edge(
                        type="COVERS",
                        src=key,
                        dst=f"concept:{slug}",
                        origin="agent",
                        source_path="demo:graph-builder",
                        namespace=ns,
                    ),
                    now,
                )
                made += int(ok)
                skipped += int(not ok)
        print(f"Рёбер COVERS создано/обновлено: {made}; пропущено: {skipped}")

        # контроль: у типовой статьи теперь есть соседи (концепты + связанные статьи)
        if articles:
            sample = articles[0]["natural_key"]
            nbrs = graph.expand_neighbors([sample], hops=2, namespaces=[ns])
            print(f"Соседей у '{sample[:60]}' (2 хопа): {len(nbrs)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
