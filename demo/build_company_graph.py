"""Загрузить синтетический граф знаний компании в демо-KB (узлы любых типов + рёбра).

Запускается ВНУТРИ контейнера memory-service (движок + доступ к БД):
    docker cp demo/build_company_graph.py <serve>:/tmp/ && \
    docker cp demo/corpus/company_graph.json <serve>:/tmp/ && \
    docker exec -e DEMO_NAMESPACE=sea-demo -e GRAPH_JSON=/tmp/company_graph.json \
      <serve> python /tmp/build_company_graph.py

Граф компании — проекция единого хранилища: люди, команды, проекты, решения (BIZ-NNN),
задачи (T-NNN), встречи (M-NNN), вопросы (Q-NNN), продукты/доки, клиенты — связанные
типизированными рёбрами (reports_to/owns/authored/decided_in/assigned_to/expert_in/…).
Узлы с телом индексируются чанком (их находит поиск); узлы-сущности (person/team/…) —
только в графе и всплывают по рёбрам. Идемпотентно (upsert). Пишет в один namespace.
"""

from __future__ import annotations

import datetime as dt
import json
import os

from platform_memory.core import db as dbmod
from platform_memory.core.config import get_settings
from platform_memory.core.models import Chunk, Edge, Node, Provenance
from platform_memory.graph import GraphStore
from platform_memory.index import VectorIndex, build_embedder

# Типы, которые индексируем чанком (у них есть содержательное тело → поиск их находит).
_SEARCHABLE = {"product_doc", "decision", "meeting", "question", "task", "person"}


def main() -> int:
    ns = os.environ.get("DEMO_NAMESPACE", "sea-demo").strip() or "sea-demo"
    path = os.environ.get("GRAPH_JSON", "/tmp/company_graph.json")
    data = json.loads(open(path, encoding="utf-8").read())
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    now = dt.datetime.now().isoformat()
    settings = get_settings()

    conn = dbmod.connect(settings, autocommit=True)
    try:
        graph = GraphStore(conn, settings.graph_name, settings.default_namespace)
        graph.ensure_schema()
        index = VectorIndex(
            conn, settings.chunks_table, settings.embedding_dim, settings.default_namespace
        )
        index.ensure_schema()
        embedder = build_embedder(settings)

        # 1) узлы (+ чанк для содержательных типов, чтобы поиск их находил)
        chunked = 0
        for n in nodes:
            key, ntype = n.get("key"), n.get("type", "entity")
            if not key:
                continue
            title = n.get("title", key)
            body = (n.get("body") or "").strip()
            props = {"content": body} if body else {}
            if n.get("subtitle"):
                props["subtitle"] = n["subtitle"]
            graph.upsert_node(
                Node(
                    type=ntype,
                    natural_key=key,
                    title=title,
                    properties=props,
                    origin="agent",
                    namespace=ns,
                    provenance=Provenance(
                        source_path=f"memory:{ntype}/{key}", confidence=0.9, last_seen=now
                    ),
                )
            )
            if body and ntype in _SEARCHABLE:
                chunk = Chunk(
                    node_key=key,
                    text=body,
                    source_path=f"memory:{ntype}/{key}",
                    title=title,
                    namespace=ns,
                )
                index.add_chunks([chunk], [embedder.embed_one(chunk.embedding_input())])
                chunked += 1
        print(f"Узлов: {len(nodes)} (проиндексировано чанком: {chunked})")

        # 2) типизированные рёбра (оба конца уже существуют)
        made = skipped = 0
        for e in edges:
            src, dst, etype = e.get("src"), e.get("dst"), e.get("type", "REL")
            if not src or not dst:
                continue
            ok = graph.upsert_edge(
                Edge(
                    type=etype,
                    src=src,
                    dst=dst,
                    origin="agent",
                    source_path="demo:company-graph",
                    namespace=ns,
                ),
                now,
            )
            made += int(ok)
            skipped += int(not ok)
        print(f"Рёбер создано/обновлено: {made}; пропущено (нет узла): {skipped}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
