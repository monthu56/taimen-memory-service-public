"""Юнит-тесты форматирования контекста retrieval (без БД)."""

from __future__ import annotations

import pytest

from platform_memory.retrieval.query import _fmt_neighbor

pytestmark = pytest.mark.unit


def test_fmt_neighbor_basic():
    line = _fmt_neighbor(
        {
            "natural_key": "T-001",
            "type": "task",
            "title": "Выбор СУБД",
            "source_path": "задачи/T-001.md",
        }
    )
    assert "[T-001]" in line and "(task)" in line and "задачи/T-001.md" in line


def test_fmt_neighbor_shows_community_theme():
    line = _fmt_neighbor(
        {
            "natural_key": "BIZ-010",
            "type": "decision",
            "title": "AGE",
            "community_name": "Инфраструктура",
        }
    )
    assert "тема=Инфраструктура" in line


def test_fmt_neighbor_inner_status():
    line = _fmt_neighbor(
        {"natural_key": "T-002", "type": "task", "title": "X", "props": {"status": "in_progress"}}
    )
    assert "status=in_progress" in line
