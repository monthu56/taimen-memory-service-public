"""Видимость по principal (MEM-ADR-019) на остальных поверхностях записи и чтения.

Те же namespace и principal, что в ``test_visibility_reads``/``test_visibility_writes``:
W1, W2 и без воркспейсов (``[]``). Здесь — Console ``/import``, MCP (запись наблюдения
и tools чтения с сужением ``allowed_scopes``), ``/api/memory/consolidate``,
идемпотентность наблюдений с чужой source identity, видимость пишущего в записи
наблюдения для redrive и лок ключа записи (TOCTOU проверки чанков).
"""

from __future__ import annotations

import threading

import pytest

from platform_memory.core import db as dbmod
from platform_memory.core.models import Chunk, Edge, Node
from platform_memory.core.namespaces import workspace_namespace
from platform_memory.core.observations import Observation
from platform_memory.core.scopes import ForeignObjectError
from platform_memory.index import VectorIndex
from platform_memory.mcp import server as mcp
from platform_memory.retrieval.documents import retain_document
from platform_memory.retrieval.query import write_and_index_fact
from tests.integration import test_visibility_reads as reads
from tests.integration import test_visibility_writes as writes
from tests.integration.test_visibility_reads import ALICE, NS, TENANT, W2, _observation

env = reads.env

pytestmark = pytest.mark.integration

W1_SCOPE = writes.W1_SCOPE
W2_SCOPE = writes.W2_SCOPE
SECRET = writes.SECRET
FOREIGN = writes.FOREIGN
# Видимость principal'ов фикстуры env (policy) — как её вычисляет сервер.
ALICE_SCOPES = [W1_SCOPE, f"principal:{ALICE}"]
# Сужение MCP (статический ключ сервиса + allowed_scopes): W1, W2 и без воркспейсов.
MCP_VISIBILITY = {"w1": [W1_SCOPE], "w2": [W2_SCOPE], "none": []}


# --- 1. Console /import -----------------------------------------------------------


@pytest.fixture()
def console(env, monkeypatch):
    """Console включена на NS поверх окружения видимости."""
    import platform_memory.server.app as app_mod

    client, headers = env
    base = app_mod.get_settings()
    console_settings = base.model_copy(update={"console_enabled": True, "console_namespaces": NS})
    monkeypatch.setattr(app_mod, "get_settings", lambda: console_settings)
    return client, headers


def _article(key: str, scopes: list[str]) -> Node:
    """Статья того же вида, что пишет импорт Console (``article``)."""
    props: dict = {"content": SECRET, "scopes": scopes}
    return Node(type="article", natural_key=key, title=key, properties=props, namespace=NS)


def _import(client, key: str, content: str = "OVERWRITE"):
    return client.post(
        "/console/import",
        data={"ns": NS, "content": content, "title": key, "external_id": key},
    )


@pytest.mark.parametrize("owner", [W1_SCOPE, W2_SCOPE])
def test_console_import_does_not_overwrite_private_node(console, stores, settings, owner):
    """Console без principal пишет как «без воркспейсов»: чужой узел — отказ, узел цел."""
    client, _headers = console
    conn, graph, _facts, index, _obs = stores
    graph.upsert_node(_article("private", [owner]))
    chunk = Chunk(
        node_key="private", text=SECRET, source_path="p", namespace=NS, meta={"scopes": [owner]}
    )
    index.add_chunks([chunk], [[0.1] * settings.embedding_dim])

    resp = _import(client, "private")
    assert resp.status_code == 403, resp.text
    assert "Нет прав на запись: private" in resp.text
    assert SECRET not in resp.text and W2 not in resp.text  # W1 — в имени базы NS
    node = graph.get_node("private", namespaces=[NS])
    assert node["props"]["content"] == SECRET
    assert node["props"]["scopes"] == [owner]
    assert writes._chunks(conn, settings, "private") == [(SECRET, {"scopes": [owner]})]


def test_console_import_writes_shared_and_new(console, stores):
    """Общий узел базы и новый ключ Console пишет как раньше."""
    client, _headers = console
    _conn, graph, *_ = stores
    graph.upsert_node(_article("shared", []))

    for key in ("shared", "fresh"):
        resp = _import(client, key, content=f"CONSOLE-{key}")
        assert resp.status_code == 200, resp.text
        node = graph.get_node(key, namespaces=[NS])
        assert node["props"]["content"] == f"CONSOLE-{key}"
        assert "scopes" not in node["props"]


# --- 1. MCP remember_observation ---------------------------------------------------


def _remember(settings, ext_id: str, scopes: list[str], allowed=None) -> str:
    args = {
        "kind": "note",
        "content": f"MCP-{ext_id}",
        "scopes": scopes,
        "source": {"system": "mcp", "stream": "notes", "external_id": ext_id},
        "namespace": NS,
    }
    if allowed is not None:
        args["allowed_scopes"] = allowed
    return mcp.tool_remember_observation(settings, args)


def test_mcp_remember_observation_checks_writer_scopes(settings, stores):
    """Scope наблюдения — только из видимости вызова, как у POST /observations."""
    _conn, _graph, _facts, _index, obs_store = stores
    assert _remember(settings, "own", [W1_SCOPE], MCP_VISIBILITY["w1"]).startswith("Сохранено")
    for who in FOREIGN:
        with pytest.raises(ForeignObjectError, match=f"Нет прав на запись: {W1_SCOPE}"):
            _remember(settings, f"planted-{who}", [W1_SCOPE], MCP_VISIBILITY[who])
    # Без сужения — service-режим статического ключа, как у HTTP.
    assert _remember(settings, "svc", [W2_SCOPE]).startswith("Сохранено")
    assert obs_store.count([NS]) == 2


def test_mcp_remember_observation_hides_foreign_duplicate(settings, stores):
    assert _remember(settings, "dup", [W1_SCOPE], MCP_VISIBILITY["w1"]).startswith("Сохранено")
    for who in FOREIGN:
        with pytest.raises(ForeignObjectError) as err:
            _remember(settings, "dup", [], MCP_VISIBILITY[who])
        assert "obs-" not in str(err.value)
    assert "дубликат" in _remember(settings, "dup", [W1_SCOPE], MCP_VISIBILITY["w1"])


# --- 2. MCP tools чтения -----------------------------------------------------------


@pytest.fixture()
def mcp_graph(settings, stores):
    """В default namespace: a (W1) — c (общий) — b (W2), чанки a и b со своими scopes."""
    _conn, graph, _facts, index, _obs = stores
    ns = settings.default_namespace
    for key, scopes in (("node-a", [W1_SCOPE]), ("node-b", [W2_SCOPE]), ("node-c", [])):
        props = {"content": f"SECRET-{key}"}
        if scopes:
            props["scopes"] = scopes
        graph.upsert_node(
            Node(type="note", natural_key=key, title=f"Title {key}", properties=props, namespace=ns)
        )
    for src, dst in (("node-a", "node-c"), ("node-c", "node-b")):
        graph.upsert_edge(Edge(type="LINKS_TO", src=src, dst=dst, namespace=ns), "2026-09-30")
    from platform_memory.index import build_embedder

    embedder = build_embedder(settings)
    chunks = [
        Chunk(
            node_key=key,
            text=f"общий термин кварк SECRET-{key}",
            source_path=key,
            title=f"Title {key}",
            namespace=ns,
            meta={"scopes": scopes} if scopes else {},
        )
        for key, scopes in (("node-a", [W1_SCOPE]), ("node-b", [W2_SCOPE]), ("node-c", []))
    ]
    index.add_chunks(chunks, embedder.embed([c.embedding_input() for c in chunks]))
    return graph


def _visible_keys(who: str | None) -> set[str]:
    return {"w1": {"node-a", "node-c"}, "w2": {"node-b", "node-c"}, "none": {"node-c"}}.get(
        who, {"node-a", "node-b", "node-c"}
    )


def _args(who: str | None, **args) -> dict:
    return {**args, "allowed_scopes": MCP_VISIBILITY[who]} if who else args


@pytest.mark.parametrize("who", ["w1", "w2", "none", None])
def test_mcp_get_node_and_neighbors(settings, mcp_graph, who):
    visible = _visible_keys(who)
    for key in ("node-a", "node-b"):
        by_key = mcp.tool_get_node(settings, _args(who, label=key))
        by_title = mcp.tool_get_node(settings, _args(who, label=f"Title {key}"))
        if key in visible:
            assert f"ключ: {key}" in by_key and f"ключ: {key}" in by_title
        else:
            assert by_key == f"Узел не найден: {key!r}"
            assert by_title == f"Узел не найден: {'Title ' + key!r}"
    center = mcp.tool_get_node(settings, _args(who, label="node-c"))
    assert f"соседей: {len(visible) - 1}" in center

    neighbors = mcp.tool_get_neighbors(settings, _args(who, label="node-c"))
    for key in ("node-a", "node-b"):
        assert (f"Title {key}" in neighbors) == (key in visible), neighbors


@pytest.mark.parametrize("who", ["w1", "w2", "none", None])
def test_mcp_shortest_path_skips_invisible(settings, mcp_graph, who):
    path = mcp.tool_shortest_path(settings, _args(who, source="node-a", target="node-b"))
    if who is None:
        assert path.startswith("Путь (2 шаг(ов))")
    elif who == "w1":
        assert path == "Цель не найдена: 'node-b'"
    else:
        assert path == "Источник не найден: 'node-a'"
    for text in (path,):
        for key in {"node-a", "node-b"} - _visible_keys(who):
            assert f"Title {key}" not in text


@pytest.mark.parametrize("who", ["w1", "w2", "none", None])
def test_mcp_query_graph_and_build_context(settings, mcp_graph, who):
    visible = _visible_keys(who)
    subgraph = mcp.tool_query_graph(settings, _args(who, question="кварк"))
    context = mcp.tool_build_context(settings, _args(who, query="кварк"))
    for key in ("node-a", "node-b"):
        if key not in visible:
            assert f"Title {key}" not in subgraph, subgraph
            assert f"SECRET-{key}" not in context, context
    assert "SECRET-node-c" in context
    for key in visible:
        assert f"SECRET-{key}" in context, context


def test_mcp_rejects_malformed_allowed_scopes(settings, mcp_graph):
    with pytest.raises(ValueError, match="allowed_scopes"):
        mcp.tool_get_node(settings, {"label": "node-c", "allowed_scopes": "workspace:x"})


# --- 3. /api/memory/consolidate ----------------------------------------------------


def test_consolidate_foreign_namespace_is_403(env, stores):
    client, headers = env
    foreign_ns = workspace_namespace(str(TENANT), W2)  # гранты пускают, видимость — нет
    for who in ("w1", "w2", "none"):
        resp = client.post(
            "/api/memory/consolidate", json={"namespace": foreign_ns}, headers=headers[who]
        )
        assert resp.status_code == 403, (who, resp.text)
        assert "statuses" not in resp.text
    svc = client.post(
        "/api/memory/consolidate", json={"namespace": foreign_ns}, headers=headers["service"]
    )
    assert svc.status_code == 200, svc.text


def _stored(obs_store, ext_id: str, scopes: list[str], writer, assertions=()) -> str:
    obs = Observation.from_payload(
        {**_observation(ext_id, scopes, f"OBS-{ext_id}"), "assertions": list(assertions)},
        namespace=NS,
    )
    oid, duplicate = obs_store.insert(obs, writer_scopes=writer)
    assert not duplicate
    return oid


def test_consolidate_counts_only_visible(env, stores):
    """Redrive и свод статусов — только по видимым вызывающему наблюдениям."""
    client, headers = env
    _conn, _graph, _facts, _index, obs_store = stores
    ids = {
        "public": _stored(obs_store, "public", [], None),
        "w1": _stored(obs_store, "w1", [W1_SCOPE], None),
        "w2": _stored(obs_store, "w2", [W2_SCOPE], None),
    }
    for who, processed in (("none", ["public"]), ("w2", ["w2"]), ("w1", ["w1"])):
        resp = client.post("/api/memory/consolidate", json={"namespace": NS}, headers=headers[who])
        assert resp.status_code == 200, (who, resp.text)
        assert resp.json()["processed"] == 1, (who, resp.json())
        assert resp.json()["statuses"] == {"processed": 1}
        for name in processed:
            assert obs_store.get(ids[name], namespaces=[NS]).status == "processed"
    assert obs_store.count([NS], status="received") == 0


# --- 4. Идемпотентность с чужой source identity ------------------------------------


def test_duplicate_of_foreign_observation_is_denied_without_id(env, stores):
    client, headers = env
    _conn, _graph, _facts, _index, obs_store = stores
    own = writes._observe(client, headers["w1"], _observation("same", [W1_SCOPE], "OBS-W1"))
    assert own.status_code == 201, own.text
    oid = own.json()["observation_id"]

    for who in FOREIGN:
        for scopes in ([], [W2_SCOPE] if who == "w2" else []):
            resp = writes._observe(client, headers[who], _observation("same", scopes, "OTHER"))
            assert resp.status_code == 403, (who, resp.text)
            assert resp.json()["detail"] == "Нет прав на запись: tracker/events/same"
            assert oid not in resp.text and "status" not in resp.json()
        batch = client.post(
            "/api/memory/observations:batch",
            json={"observations": [_observation("same", [], "OTHER")], "scope": {"namespace": NS}},
            headers=headers[who],
        )
        assert batch.status_code == 207, batch.text
        assert batch.json()["results"] == [
            {"index": 0, "error": "Нет прав на запись: tracker/events/same"}
        ]
        assert oid not in batch.text

    for who in ("w1", "service"):
        again = writes._observe(client, headers[who], _observation("same", [W1_SCOPE], "X"))
        assert again.status_code == 201, (who, again.text)
        assert again.json() == {"observation_id": oid, "duplicate": True, "status": "processed"}
    # Запись W1 не тронута.
    assert obs_store.get(oid, namespaces=[NS]).content == "OBS-W1"


def test_duplicate_of_shared_observation_stays_idempotent(env, stores):
    client, headers = env
    first = writes._observe(client, headers["none"], _observation("pub", [], "OBS-PUBLIC"))
    assert first.status_code == 201, first.text
    for who in ("w1", "w2", "none"):
        again = writes._observe(client, headers[who], _observation("pub", [], "OBS-PUBLIC"))
        assert again.status_code == 201, (who, again.text)
        assert again.json()["duplicate"] is True
        assert again.json()["observation_id"] == first.json()["observation_id"]


# --- 6. Видимость пишущего в записи наблюдения -------------------------------------


def _writer_visibility(conn, settings, oid: str):
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT writer_visibility FROM "{settings.observations_table}" '
            "WHERE namespace = %s AND observation_id = %s",
            (NS, oid),
        )
        return cur.fetchone()[0]


def test_writer_visibility_is_stored_and_not_exposed(env, stores, settings):
    client, headers = env
    conn, *_ = stores
    stored = {}
    for who, scopes in (("w1", [W1_SCOPE]), ("none", []), ("service", [])):
        resp = writes._observe(client, headers[who], _observation(f"wv-{who}", scopes, "OBS"))
        assert resp.status_code == 201, resp.text
        oid = resp.json()["observation_id"]
        stored[who] = _writer_visibility(conn, settings, oid)
        read = client.get(
            f"/api/memory/observations/{oid}", params={"namespace": NS}, headers=headers[who]
        )
        assert read.status_code == 200, read.text
        assert "writer_visibility" not in read.json()
    assert stored == {
        "w1": {"scopes": ALICE_SCOPES},
        "none": {"scopes": [f"principal:{reads.CAROL}"]},
        "service": {"scopes": None},
    }


_VICTIM = [{"assert": "entity", "entity": {"type": "person", "id": "victim", "title": "HIJACKED"}}]


@pytest.mark.parametrize(
    ("writer", "owner_ok"),
    [
        (None, False),  # запись до TASK-001056: scopes записи не доверяем — уровень namespace
        ([W2_SCOPE], False),
        ([], False),
        ([W1_SCOPE], True),
    ],
)
def test_redrive_uses_writer_visibility_not_record_scopes(env, stores, settings, writer, owner_ok):
    """Наблюдение со scope W1 от чужого пишущего (до TASK-001052) redrive не проецирует."""
    client, headers = env
    conn, graph, _facts, _index, obs_store = stores
    graph.upsert_node(writes._entity("person:victim", [W1_SCOPE]))
    oid = _stored(obs_store, "legacy", [W1_SCOPE], writer or [], _VICTIM)
    if writer is None:
        with conn.cursor() as cur:  # как у строки, записанной до колонки writer_visibility
            cur.execute(
                f'UPDATE "{settings.observations_table}" SET writer_visibility = NULL '
                "WHERE observation_id = %s",
                (oid,),
            )

    resp = client.post(
        "/api/memory/consolidate", json={"namespace": NS}, headers=headers["service"]
    )
    assert resp.status_code == 200, resp.text
    record = obs_store.get(oid, namespaces=[NS])
    node = graph.get_node("person:victim", namespaces=[NS])
    assert node["props"]["scopes"] == [W1_SCOPE]
    if owner_ok:
        assert record.status == "processed"
        assert node["title"] == "HIJACKED"
    else:
        assert record.status == "failed", record.status_detail
        assert node["title"] != "HIJACKED"


def test_redrive_is_narrowed_by_caller(env, stores):
    """Запустивший redrive с меньшей видимостью не переигрывает невидимое ему."""
    client, headers = env
    _conn, graph, _facts, _index, obs_store = stores
    graph.upsert_node(writes._entity("person:victim", [W1_SCOPE]))
    _stored(obs_store, "narrow", [], None, _VICTIM)  # пишущий без ограничения
    # Наблюдение общее (видно W2), но W2 не видит узел W1 — assertion падает.
    resp = client.post("/api/memory/consolidate", json={"namespace": NS}, headers=headers["w2"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["statuses"] == {"failed": 1}
    assert graph.get_node("person:victim", namespaces=[NS])["title"] != "HIJACKED"


# --- 5. Лок ключа записи: проверка чанков не отстаёт от записи (TOCTOU) -----------


def _write_fact(settings, key, allowed):
    write_and_index_fact(
        settings, key, "note", key, text="RACE", namespace=NS, allowed_scopes=allowed
    )


def _write_doc(settings, key, allowed):
    retain_document(
        settings,
        namespace=NS,
        natural_key=key,
        title=key,
        chunks=[{"text": "RACE"}],
        replace=False,
        allowed_scopes=allowed,
    )


def _signal_key_lock(monkeypatch) -> threading.Event:
    """Событие «пишущий дошёл до лока ключа» — без опроса ``pg_locks`` и таймаутов.

    Срабатывает, когда поток пишущего входит в :meth:`VectorIndex.key_write_lock`:
    всё, что он делает с чанками после этого, идёт уже под локом. Прежде тест
    опрашивал ``pg_locks`` по всему кластеру — ожидающий advisory-лок любого
    другого клиента давал ложное «дошёл», а занятая база — таймаут.
    """
    entered = threading.Event()
    original = VectorIndex.key_write_lock

    def key_write_lock(self, node_key, namespace=""):
        if threading.current_thread() is not threading.main_thread():
            entered.set()
        return original(self, node_key, namespace)

    monkeypatch.setattr(VectorIndex, "key_write_lock", key_write_lock)
    return entered


@pytest.mark.parametrize("write", [_write_fact, _write_doc], ids=["fact", "document"])
@pytest.mark.parametrize("who", ["w1", "w2", "none"])
def test_chunk_guard_runs_under_key_lock(settings, stores, write, who, monkeypatch):
    """Чанк W1, появившийся, пока пишущий ждёт лок ключа, проверку не обходит."""
    conn, graph, _facts, index, _obs = stores
    allowed = {"w1": ALICE_SCOPES, "w2": [W2_SCOPE], "none": []}[who]
    key = f"race-{who}"
    holder = dbmod.connect(settings, autocommit=True)
    outcome: dict = {}
    entered = _signal_key_lock(monkeypatch)

    def run() -> None:
        try:
            write(settings, key, allowed)
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001 — результат проверяет тест
            outcome["error"] = exc

    try:
        holder_index = VectorIndex(
            holder, settings.chunks_table, settings.embedding_dim, settings.default_namespace
        )
        with holder_index.key_write_lock(key, NS):
            writer = threading.Thread(target=run)
            writer.start()
            assert entered.wait(timeout=60), "пишущий не дошёл до лока ключа"
            # Пишущий у лока (ждёт или вот-вот ждёт), а чанк W1 без узла появляется —
            # проверка чанков обязана увидеть его.
            chunk = Chunk(
                node_key=key,
                text=SECRET,
                source_path=key,
                namespace=NS,
                meta={"scopes": [W1_SCOPE]},
            )
            holder_index.add_chunks([chunk], [[0.1] * settings.embedding_dim])
        writer.join(timeout=30)
        assert not writer.is_alive()
    finally:
        holder.close()

    if who == "w1":
        assert outcome == {"ok": True}, outcome
        scopes = [meta["scopes"] for _text, meta in writes._chunks(conn, settings, key)]
        assert all(W1_SCOPE in s for s in scopes), scopes
    else:
        assert isinstance(outcome.get("error"), ForeignObjectError), outcome
        assert graph.get_node(key, namespaces=[NS]) is None
        assert writes._chunks(conn, settings, key) == [(SECRET, {"scopes": [W1_SCOPE]})]


def test_redrive_migrates_table_without_writer_column(settings, stores):
    """Таблица до TASK-001056: redrive добавляет колонку, прежние записи — уровень namespace."""
    from platform_memory.observations.ingest import process_observations

    conn, graph, _facts, _index, obs_store = stores
    graph.upsert_node(writes._entity("person:victim", [W1_SCOPE]))
    oid = _stored(obs_store, "old", [W1_SCOPE], [W1_SCOPE], _VICTIM)
    with conn.cursor() as cur:
        cur.execute(f'ALTER TABLE "{settings.observations_table}" DROP COLUMN writer_visibility')

    result = process_observations(settings, namespace=NS)
    assert result["statuses"] == {"failed": 1}
    assert _writer_visibility(conn, settings, oid) is None
    assert graph.get_node("person:victim", namespaces=[NS])["title"] != "HIJACKED"
