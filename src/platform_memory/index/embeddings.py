"""Эмбеддинги: OpenAI-совместимый gateway (Eden AI и аналоги) + офлайн-fake для тестов."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol

from platform_memory.core.config import Settings

_TOKEN = re.compile(r"\w+", re.UNICODE)

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """Контракт эмбеддера."""

    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Векторизовать список текстов."""
        ...

    def embed_one(self, text: str) -> list[float]:
        """Векторизовать один текст."""
        ...


class FakeEmbedder:
    """Детерминированный hashing-эмбеддер (bag-of-words). Без сети — для тестов/офлайна.

    Косинусная близость приблизительно отражает пересечение слов, поэтому пригоден
    как лексический заменитель при проверке пайплайна без gateway.
    """

    def __init__(self, dim: int = 256):
        """Создать эмбеддер с заданной размерностью вектора."""
        self.dim = dim

    def embed_one(self, text: str) -> list[float]:
        """Векторизовать один текст в нормированный bag-of-words вектор."""
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 7) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Векторизовать список текстов."""
        return [self.embed_one(t) for t in texts]


class OpenAIEmbedder:
    """Эмбеддер через OpenAI-совместимый endpoint (base_url + api_key из конфига)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 64,
        timeout: float = 25.0,
    ):
        """Создать клиент OpenAI-совместимого endpoint; проверить наличие api_key."""
        from openai import OpenAI

        if not api_key:
            raise ValueError(
                "CB_EMBEDDING_API_KEY пуст. Задайте ключ в .env или используйте "
                "CB_EMBEDDING_PROVIDER=fake для офлайн-режима."
            )
        self._client = OpenAI(base_url=base_url or None, api_key=api_key)
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        # Таймаут обязателен: эмбеддинг — ПЕРВЫЙ вызов в retrieve(), до любого обращения к БД.
        # Дефолт openai-SDK — 600 с с двумя повторами, то есть один зависший gateway держал
        # HTTP-воркер до 30 минут и укладывал /recall, /search и /query целиком.
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Векторизовать список текстов батчами через gateway."""
        # text-embedding-3-* поддерживают усечение размерности через параметр dimensions.
        extra = {"dimensions": self.dim} if self.model.startswith("text-embedding-3") else {}
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            resp = self._client.embeddings.create(
                model=self.model, input=batch, timeout=self.timeout, **extra
            )
            out.extend(item.embedding for item in resp.data)
        if out and len(out[0]) != self.dim:
            raise RuntimeError(
                f"Эмбеддер вернул вектор размерности {len(out[0])}, а конфиг CB_EMBEDDING_DIM="
                f"{self.dim}. Приведите CB_EMBEDDING_DIM к размерности модели {self.model!r} "
                f"и выполните `cb ingest --reset`."
            )
        return out

    def embed_one(self, text: str) -> list[float]:
        """Векторизовать один текст."""
        return self.embed([text])[0]


def build_embedder(settings: Settings) -> Embedder:
    """Собрать эмбеддер согласно конфигу (provider = openai | fake)."""
    provider = (settings.embedding_provider or "openai").lower()
    if provider == "fake":
        # Громко предупреждаем: fake отдаёт md5-мешок слов ТОЙ ЖЕ размерности, что и боевая
        # модель, поэтому проходит все проверки схемы и `_check_dim`. Инсталляция молча
        # получает бессмысленный семантический поиск — единственный сигнал об этом здесь.
        logger.warning(
            "CB_EMBEDDING_PROVIDER=fake: векторы считаются хэшированием слов, а не моделью. "
            "Семантический поиск НЕ работает, выдача лексическая. Режим только для тестов "
            "и офлайн-прогонов; данные, проиндексированные так, требуют переиндекса."
        )
        return FakeEmbedder(dim=settings.embedding_dim)
    if provider == "openai":
        return OpenAIEmbedder(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            timeout=settings.embedding_timeout,
        )
    raise ValueError(f"Неизвестный CB_EMBEDDING_PROVIDER: {settings.embedding_provider!r}")
