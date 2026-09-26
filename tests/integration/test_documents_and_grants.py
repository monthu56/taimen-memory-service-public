"""Интеграционные тесты document-API и auth-грантов (ADR-017).

Документы: ингест → поиск с meta-фильтром → replace → удаление.
Гранты: полный HTTP-стек через TestClient (Depends(authenticate) в деле):
401 без/с неверным ключом, 403 на чужой namespace и на запись в read-only,
200 в своём поддереве, legacy-ключ = full access.
Запуск — см. conftest (CB_TEST_DATABASE_URL).
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration

NS_A = "tp:tenant:aaaa:team:t1"
NS_B = "tp:tenant:bbbb:team:t2"

_KEYS = [
    {
        "name": "tp-api",
        "key": "tp-secret",
        "grants": [
            {"prefix": "tp:", "read": True, "write": True},
            {"prefix": "shared", "read": True, "write": False},
        ],
    }
]


@pytest.fixture
def appmod(test_settings, clean_db, monkeypatch):
    import platform_memory.server.app as mod

    monkeypatch.setattr(mod, "get_settings", lambda: test_settings)
    return mod


def _doc_req(appmod, key: str, *, ns: str = NS_A, collection: str = "licenses", texts=None):
    texts = texts or ["Лицензия на строительство", "Лицензия на проектирование"]
    return appmod.DocumentIngestRequest(
        namespace=ns,
        natural_key=key,
        title="Реестр лицензий",
        source_path="s3://kb/licenses.pdf",
        meta={"collection": collection},
        chunks=[appmod.DocumentChunkIn(text=t, order=i) for i, t in enumerate(texts)],
    )


class TestDocuments:
    def test_ingest_search_replace_delete(self, appmod):
        res = appmod.brain_document_ingest(_doc_req(appmod, "doc:lic-1"))
        assert res["chunks"] == 2 and res["namespace"] == NS_A

        found = appmod.brain_search(
            appmod.SearchRequest(
                query="лицензия строительство",
                filters={"meta": {"collection": "licenses"}},
                scope={"namespaces": [NS_A]},
            )
        )
        assert found["count"] >= 1
        assert all(r["meta"].get("collection") == "licenses" for r in found["results"])

        # meta-фильтр по другой коллекции ничего не находит
        other = appmod.brain_search(
            appmod.SearchRequest(
                query="лицензия",
                filters={"meta": {"collection": "equipment"}},
                scope={"namespaces": [NS_A]},
            )
        )
        assert other["count"] == 0

        # replace: повторный ингест с одним чанком заменяет старые
        res2 = appmod.brain_document_ingest(
            _doc_req(appmod, "doc:lic-1", texts=["Единственная лицензия"])
        )
        assert res2["chunks"] == 1
        after = appmod.brain_search(
            appmod.SearchRequest(query="лицензия", scope={"namespaces": [NS_A]})
        )
        assert all("Единственная" in r["text"] for r in after["results"])

        deleted = appmod.brain_document_delete("doc:lic-1", namespace=NS_A)
        assert deleted["deleted"] is True
        assert deleted["chunks_deleted"] == 1
        # аудит удаления (ADR-001) — в контуре той же KB
        trace = appmod.brain_trace(deleted["trace_id"], namespace=NS_A)
        assert trace["event_count"] == 1
        again = appmod.brain_document_delete("doc:lic-1", namespace=NS_A)
        assert again["deleted"] is False
        empty = appmod.brain_search(
            appmod.SearchRequest(query="лицензия", scope={"namespaces": [NS_A]})
        )
        assert empty["count"] == 0

    def test_document_node_created_in_graph(self, appmod, test_settings):
        from platform_memory.core import db as dbmod
        from platform_memory.graph import GraphStore

        appmod.brain_document_ingest(_doc_req(appmod, "doc:lic-2"))
        conn = dbmod.connect(test_settings, autocommit=True)
        try:
            graph = GraphStore(conn, test_settings.graph_name, test_settings.default_namespace)
            node = graph.get_node("doc:lic-2", namespaces=[NS_A])
        finally:
            conn.close()
        assert node is not None
        assert node["type"] == "document"
        assert node["source_path"] == "s3://kb/licenses.pdf"

    def test_document_via_scope_namespace_and_default(self, appmod, test_settings):
        """scope.namespace — как у остальных маршрутов записи; без него — default KB."""
        res = appmod.brain_document_ingest(
            appmod.DocumentIngestRequest(
                natural_key="doc:scoped",
                title="Док",
                scope={"namespace": NS_B},
                chunks=[appmod.DocumentChunkIn(text="текст тенанта Б")],
            )
        )
        assert res["namespace"] == NS_B
        res_default = appmod.brain_document_ingest(
            appmod.DocumentIngestRequest(
                natural_key="doc:default",
                title="Док",
                chunks=[appmod.DocumentChunkIn(text="текст по умолчанию")],
            )
        )
        assert res_default["namespace"] == test_settings.default_namespace


class TestGrantsOverHttp:
    @pytest.fixture
    def http(self, test_settings, clean_db, monkeypatch):
        """TestClient поверх настроек с реестром ключей (полный auth-стек)."""
        from fastapi.testclient import TestClient

        import platform_memory.server.app as mod

        settings = test_settings.model_copy_with(api_keys=json.dumps(_KEYS))
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
        return TestClient(mod.app)  # схему создал clean_db (TestClient без lifespan)

    @staticmethod
    def _recall_body(namespaces: list[str]) -> dict:
        return {"query": "тест", "scope": {"namespaces": namespaces}}

    def test_missing_or_wrong_key_is_401(self, http):
        assert http.post("/recall", json=self._recall_body([NS_A])).status_code == 401
        assert (
            http.post(
                "/recall",
                json=self._recall_body([NS_A]),
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )

    def test_own_subtree_allowed(self, http):
        headers = {"Authorization": "Bearer tp-secret"}
        assert (
            http.post("/recall", json=self._recall_body([NS_A]), headers=headers).status_code == 200
        )
        write = http.post(
            "/retain",
            json={"content": "факт", "scope": {"namespace": NS_A}},
            headers=headers,
        )
        assert write.status_code == 201

    def test_foreign_namespace_read_is_403(self, http):
        headers = {"Authorization": "Bearer tp-secret"}
        res = http.post("/recall", json=self._recall_body(["nexus"]), headers=headers)
        assert res.status_code == 403

    def test_default_namespace_read_denied_for_scoped_key(self, http):
        """Без scope запрос падает в default namespace (nexus) — tp-ключу он не разрешён."""
        headers = {"Authorization": "Bearer tp-secret"}
        res = http.post("/recall", json={"query": "тест"}, headers=headers)
        assert res.status_code == 403

    def test_shared_is_read_only_for_tp_key(self, http):
        headers = {"Authorization": "Bearer tp-secret"}
        read = http.post("/recall", json=self._recall_body(["shared"]), headers=headers)
        assert read.status_code == 200
        write = http.post(
            "/retain",
            json={"content": "факт", "scope": {"namespace": "shared"}},
            headers=headers,
        )
        assert write.status_code == 403

    def test_legacy_key_is_full_access(self, http, test_settings, monkeypatch):
        import platform_memory.server.app as mod

        settings = test_settings.model_copy_with(
            api_keys=json.dumps(_KEYS), server_api_key="legacy-secret"
        )
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
        headers = {"Authorization": "Bearer legacy-secret"}
        assert (
            http.post("/recall", json=self._recall_body(["nexus"]), headers=headers).status_code
            == 200
        )

    def test_document_ingest_authorized_by_write_grant(self, http):
        headers = {"Authorization": "Bearer tp-secret"}
        body = {
            "namespace": NS_A,
            "natural_key": "doc:x",
            "title": "Док",
            "chunks": [{"text": "текст"}],
        }
        assert http.post("/api/brain/documents", json=body, headers=headers).status_code == 201
        foreign = {**body, "namespace": "nexus"}
        assert http.post("/api/brain/documents", json=foreign, headers=headers).status_code == 403

    def test_memory_api_context_authorized_by_read_grant(self, http):
        headers = {"Authorization": "Bearer tp-secret"}
        own = {"query": "тест", "scope": {"namespaces": [NS_A]}}
        assert http.post("/api/memory/context", json=own, headers=headers).status_code == 200
        foreign = {"query": "тест", "scope": {"namespaces": ["nexus"]}}
        assert http.post("/api/memory/context", json=foreign, headers=headers).status_code == 403
