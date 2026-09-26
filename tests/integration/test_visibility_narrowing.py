"""Клиентское сужение видимости (MEM-ADR-021) на реальной БД.

Один namespace корневого воркспейса ``org``, в нём два поддерева — ``backend`` и
``finance`` — плюс память уровня ``org``. Ядро сужает чтение до фокусного
воркспейса и его предков (``allowedScopes``); сервис берёт пересечение с серверной
видимостью: сужение работает без policy-режима, не расширяет видимость в
policy-режиме, пустой список — пусто.
"""

from __future__ import annotations

import uuid

import pytest

from platform_memory import retain_observation
from platform_memory.core.namespaces import workspace_namespace

pytestmark = pytest.mark.integration

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG = "0a0a0a0a-0000-4000-8000-000000000001"
BACKEND = "0a0a0a0a-0000-4000-8000-000000000002"
FINANCE = "0a0a0a0a-0000-4000-8000-000000000003"
NS = workspace_namespace(str(TENANT), ORG)

ISSUER = "https://iam.test/iam"
JWKS_URL = "https://iam.test/iam/.well-known/jwks.json"
AUDIENCE = "memory-service"

# Сужение ядра: фокусный воркспейс backend и его предок org.
BACKEND_FOCUS = [f"workspace:{BACKEND}", f"workspace:{ORG}"]


def _observation(ext_id: str, workspace: str, marker: str, team: str, title: str) -> dict:
    return {
        "source": {"system": "tracker", "stream": "events", "external_id": ext_id},
        "kind": "note",
        "occurred_at": "2026-09-01T10:00:00Z",
        "scopes": [f"workspace:{workspace}"],
        "content": f"Регламент бюджета {marker}",
        "assertions": [
            {"assert": "entity", "entity": {"type": "project", "id": "budget", "title": "Бюджет"}},
            {"assert": "entity", "entity": {"type": "team", "id": team, "title": title}},
            {
                "assert": "fact",
                "fact": {
                    "subject": "project:budget",
                    "predicate": "OWNED_BY",
                    "object": f"team:{team}",
                    "valid_from": "2026-09-01T00:00:00Z",
                },
            },
            {
                "assert": "text",
                "text": {"content": f"Регламент бюджета: {marker}", "title": f"Регламент {team}"},
            },
        ],
    }


@pytest.fixture()
def seeded(settings, stores):
    for obs in (
        _observation("b-1", BACKEND, "MARKER-BACKEND", "backend", "Команда бэкенда"),
        _observation("f-1", FINANCE, "MARKER-FINANCE", "finance", "Команда финансов"),
        _observation("o-1", ORG, "MARKER-ORG", "org", "Совет организации"),
    ):
        assert retain_observation(settings, obs, namespace=NS)["status"] == "processed"


def _context(client, headers=None, **extra):
    body = {
        "strategy": "briefing",
        "anchors": ["project:budget"],
        "k": 20,
        "scope": {"namespace": NS},
        **extra,
    }
    resp = client.post("/api/memory/context", json=body, headers=headers or {})
    assert resp.status_code == 200, resp.text
    return " ".join(
        item["text"]
        for s in resp.json()["sections"]
        if s["kind"] != "current"
        for item in s["items"]
    )


def _recall(client, headers=None, **extra):
    body = {"query": "Регламент бюджета", "budget": "high", "scope": {"namespace": NS}, **extra}
    resp = client.post("/api/brain/recall", json=body, headers=headers or {})
    assert resp.status_code == 200, resp.text
    return " ".join(m["text"] for m in resp.json()["memories"])


@pytest.fixture()
def service_client(settings, monkeypatch):
    """Сервис без policy-режима: серверная видимость UNRESTRICTED (как в local-режиме ядра)."""
    import platform_memory.server.app as app_mod
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    return TestClient(app_mod.app)


def test_without_narrowing_both_subtrees_visible(seeded, service_client):
    # Контроль чувствительности: без сужения соседнее поддерево видно — ровно та утечка.
    text = _context(service_client)
    assert "MARKER-BACKEND" in text and "MARKER-FINANCE" in text


def test_narrowing_hides_sibling_subtree(seeded, service_client):
    text = _context(service_client, allowedScopes=BACKEND_FOCUS)
    assert "MARKER-BACKEND" in text and "MARKER-ORG" in text
    assert "MARKER-FINANCE" not in text
    assert "Команда финансов" not in text  # факт поддерева finance
    assert "Команда бэкенда" in text

    recalled = _recall(service_client, allowedScopes=BACKEND_FOCUS)
    assert "MARKER-BACKEND" in recalled
    assert "MARKER-FINANCE" not in recalled


def test_empty_list_sees_nothing(seeded, service_client):
    text = _context(service_client, allowedScopes=[])
    for marker in ("MARKER-BACKEND", "MARKER-FINANCE", "MARKER-ORG"):
        assert marker not in text
    assert "MARKER-" not in _recall(service_client, allowedScopes=[])

    denied = service_client.post(
        "/api/memory/context",
        json={"strategy": "briefing", "scope": {"namespace": NS}, "allowedNamespaces": []},
    )
    assert denied.status_code == 403


def test_client_cannot_widen_policy_visibility(seeded, settings, monkeypatch):
    """Policy-режим: principal видит только backend (и org); клиент просит и finance."""
    import platform_memory.server.app as app_mod
    from fastapi.testclient import TestClient
    from platform_auth import ObjectPage
    from platform_auth.jwks import JwksCache
    from platform_auth.testing import SigningKey

    from platform_memory.server import iam as iam_mod
    from platform_memory.server import visibility as vis_mod

    class _Policy:
        async def list_objects(self, ctx, action, resource_type, **kw):
            objects = [f"ws-{ORG}"] if resource_type == "memory_namespace" else [ORG, BACKEND]
            return ObjectPage(objects=objects, cursor=None, model_version="1")

    policy_settings = settings.model_copy(
        update={
            "api_keys": "",
            "server_api_key": "",
            "iam_enabled": True,
            "iam_issuer": ISSUER,
            "iam_jwks_url": JWKS_URL,
            "iam_audience": AUDIENCE,
            "policy_enabled": True,
            "pii_protection": False,
        }
    )
    key = SigningKey.generate()

    def seeded_keys(_settings):
        cache = JwksCache(JWKS_URL)
        cache.seed(key.jwks())
        return cache

    vis_mod.reset()
    iam_mod.reset()
    monkeypatch.setattr(app_mod, "get_settings", lambda: policy_settings)
    monkeypatch.setattr(iam_mod, "key_source_factory", seeded_keys)
    monkeypatch.setattr(vis_mod, "policy_client_factory", lambda _s: _Policy())
    try:
        token = key.issue(
            issuer=ISSUER,
            audience=AUDIENCE,
            subject=str(ALICE),
            tenant_id=str(TENANT),
            scopes=["memory:read"],
            principal_type="human",
        )
        headers = {"Authorization": f"Bearer {token}"}
        client = TestClient(app_mod.app)

        # Серверная видимость уже отсекает finance.
        assert "MARKER-FINANCE" not in _context(client, headers)
        # Клиентский список шире серверного — finance не появляется.
        widened = [f"workspace:{FINANCE}", *BACKEND_FOCUS]
        text = _context(client, headers, allowedScopes=widened)
        assert "MARKER-BACKEND" in text
        assert "MARKER-FINANCE" not in text
        assert "MARKER-FINANCE" not in _recall(client, headers, allowedScopes=widened)
        # Сужение до одного finance даёт пересечение ∅ — только уровень namespace.
        only_finance = _context(client, headers, allowedScopes=[f"workspace:{FINANCE}"])
        assert "MARKER-FINANCE" not in only_finance and "MARKER-BACKEND" not in only_finance
    finally:
        vis_mod.reset()
        iam_mod.reset()
