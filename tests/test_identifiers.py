"""Извлечение идентификаторов для lexical retrieval (ADR-016)."""

from __future__ import annotations

import pytest

from platform_memory.core.identifiers import extract_identifiers

pytestmark = pytest.mark.unit


def test_uuid_detected():
    text = "ошибка у запроса 8f14e45f-ceea-4b16-8d5a-9a0c8f1e2b3c в проде"
    assert "8f14e45f-ceea-4b16-8d5a-9a0c8f1e2b3c" in extract_identifiers(text)


def test_commit_sha_detected():
    assert "e601addf" in extract_identifiers("починено в e601addf вчера")


def test_error_code_and_ticket():
    ids = extract_identifiers("Почему упал T-1842 с ошибкой ERR_CONN_RESET42?")
    assert "T-1842" in ids
    assert "ERR_CONN_RESET42" in ids


def test_filename_and_dotted_name():
    ids = extract_identifiers("смотри src/platform_memory/core/db.py и функцию dbmod.connect")
    assert "src/platform_memory/core/db.py" in ids
    assert "dbmod.connect" in ids


def test_camel_case():
    assert "ObservationStore" in extract_identifiers("класс ObservationStore не найден")


def test_version_string():
    assert "1.27.3" in extract_identifiers("после апгрейда на 1.27.3 сломалось")


def test_plain_words_ignored():
    assert extract_identifiers("почему упал деплой вчера вечером") == []


def test_short_numbers_ignored():
    assert extract_identifiers("в 2026 году было 404 ошибки") == []


def test_limit_respected():
    text = " ".join(f"tok_{i}x" for i in range(20))
    assert len(extract_identifiers(text)) <= 8


def test_dedup_case_insensitive():
    ids = extract_identifiers("T-100 и t-100 — одна задача")
    assert ids == ["T-100"]
