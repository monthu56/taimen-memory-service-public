"""Тесты защит эмбеддера: таймаут вызова и громкое предупреждение о fake-режиме."""

from __future__ import annotations

import logging

import pytest

from platform_memory.core.config import Settings
from platform_memory.index.embeddings import FakeEmbedder, OpenAIEmbedder, build_embedder

pytestmark = pytest.mark.unit


def _settings(**kwargs: object) -> Settings:
    """Настройки без чтения .env — тест не зависит от окружения разработчика."""
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_embedding_timeout_has_sane_default() -> None:
    """Без явной настройки таймаут конечен: дефолт SDK (600 с × 2 повтора) недопустим."""
    assert 0 < _settings().embedding_timeout <= 60


def test_timeout_reaches_the_api_call() -> None:
    """Таймаут должен уходить в вызов, а не оставаться полем: иначе он не защищает."""
    captured: dict[str, object] = {}

    class _FakeEmbeddings:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return type("Resp", (), {"data": [type("D", (), {"embedding": [0.0] * 4})()]})()

    embedder = OpenAIEmbedder(base_url="", api_key="x", model="m", dim=4, timeout=7.5)
    embedder._client = type("C", (), {"embeddings": _FakeEmbeddings()})()  # type: ignore[assignment]

    embedder.embed(["привет"])

    assert captured["timeout"] == 7.5


def test_fake_provider_warns_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """fake отдаёт md5-векторы боевой размерности и проходит проверки схемы — нужен сигнал.

    Без этого предупреждения инсталляция с CB_EMBEDDING_PROVIDER=fake (дефолт в deploy/)
    молча индексирует мусор: `_check_dim` совпадение размерности пропускает.
    """
    with caplog.at_level(logging.WARNING):
        embedder = build_embedder(_settings(embedding_provider="fake"))

    assert isinstance(embedder, FakeEmbedder)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "fake" in caplog.text.lower()


def test_fake_keeps_production_dimension() -> None:
    """Зафиксировать причину опасности: размерность fake совпадает с боевой, а не отличается."""
    settings = _settings(embedding_provider="fake", embedding_dim=1536)
    assert build_embedder(settings).dim == settings.embedding_dim
