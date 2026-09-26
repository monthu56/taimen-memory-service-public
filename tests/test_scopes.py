"""Scope model (ADR-016): канонизация, валидация, лимиты."""

from __future__ import annotations

import pytest

from platform_memory.core.scopes import (
    MAX_READ_SCOPES,
    parse_scope,
    resolve_scopes,
    scope_dicts,
    scope_key,
)

pytestmark = pytest.mark.unit


def test_scope_key_canonical():
    assert scope_key("project", "alpha") == "project:alpha"
    assert scope_key("task", "T-001") == "task:T-001"


def test_scope_key_rejects_bad_type():
    with pytest.raises(ValueError):
        scope_key("Project", "alpha")  # верхний регистр в типе запрещён
    with pytest.raises(ValueError):
        scope_key("", "alpha")
    with pytest.raises(ValueError):
        scope_key("pro ject", "alpha")


def test_scope_key_rejects_bad_id():
    with pytest.raises(ValueError):
        scope_key("project", "")
    with pytest.raises(ValueError):
        scope_key("project", "id with space")


def test_parse_scope_accepts_string_and_dict():
    assert parse_scope("project:alpha") == "project:alpha"
    assert parse_scope({"type": "project", "id": "alpha"}) == "project:alpha"
    # id может содержать двоеточия (uuid-подобные, urn)
    assert parse_scope("repo:org/name") == "repo:org/name"
    assert parse_scope("run:ns:123") == "run:ns:123"


def test_parse_scope_rejects_plain_string():
    with pytest.raises(ValueError):
        parse_scope("noscope")
    with pytest.raises(ValueError):
        parse_scope(42)


def test_resolve_scopes_dedup_and_order():
    got = resolve_scopes(["project:a", {"type": "task", "id": "t1"}, "project:a"])
    assert got == ["project:a", "task:t1"]


def test_resolve_scopes_empty_means_no_filter():
    assert resolve_scopes(None) == []
    assert resolve_scopes([]) == []


def test_resolve_scopes_limit():
    many = [f"project:p{i}" for i in range(MAX_READ_SCOPES + 1)]
    with pytest.raises(ValueError):
        resolve_scopes(many)


def test_scope_dicts_roundtrip():
    assert scope_dicts(["project:alpha", "run:ns:1"]) == [
        {"type": "project", "id": "alpha"},
        {"type": "run", "id": "ns:1"},
    ]
