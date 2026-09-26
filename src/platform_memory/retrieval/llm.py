"""LLM для генерации ответа: OpenAI-совместимый gateway + офлайн-echo."""

from __future__ import annotations

from typing import Protocol

from platform_memory.core.config import Settings


class LLM(Protocol):
    """Протокол LLM: принимает system/user-сообщения и возвращает текст ответа."""

    def answer(self, system: str, user: str, max_tokens: int | None = None) -> str:
        """Сгенерировать ответ по system- и user-сообщениям (опц. лимит длины)."""
        ...


class EchoLLM:
    """Офлайн-режим: возвращает собранный контекст без генерации (для тестов/демо без ключа)."""

    def answer(self, system: str, user: str, max_tokens: int | None = None) -> str:
        """Вернуть user-контекст без вызова LLM, с пометкой о режиме echo."""
        return "[режим echo — LLM не вызывался; ниже извлечённый из графа контекст]\n\n" + user


class OpenAILLM:
    """LLM поверх OpenAI-совместимого gateway (chat completions)."""

    def __init__(self, base_url: str, api_key: str, model: str):
        """Создать клиент OpenAI; требует непустой api_key."""
        from openai import OpenAI

        if not api_key:
            raise ValueError(
                "CB_LLM_API_KEY пуст. Задайте ключ в .env или используйте CB_LLM_PROVIDER=echo."
            )
        self._client = OpenAI(base_url=base_url or None, api_key=api_key)
        self.model = model

    def answer(self, system: str, user: str, max_tokens: int | None = None) -> str:
        """Вызвать chat completions и вернуть очищенный текст ответа.

        ``max_tokens`` ограничивает длину генерации (короткий ответ витрины — быстрее).
        """
        kwargs: dict = {}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            timeout=25,  # витрина: не висим бесконечно на генерации gateway
            **kwargs,
        )
        return (resp.choices[0].message.content or "").strip()


def build_llm(settings: Settings) -> LLM:
    """Собрать реализацию LLM по настройкам (echo или openai)."""
    provider = (settings.llm_provider or "openai").lower()
    if provider == "echo":
        return EchoLLM()
    if provider == "openai":
        return OpenAILLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    raise ValueError(f"Неизвестный CB_LLM_PROVIDER: {settings.llm_provider!r}")
