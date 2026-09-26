"""Unit-тесты валидации и разрешения namespace (ADR-017)."""

from __future__ import annotations

import pytest

from platform_memory.core.namespaces import (
    FALLBACK_NAMESPACE,
    MAX_READ_NAMESPACES,
    SHARED_NAMESPACE,
    resolve_namespace,
    resolve_namespaces,
    validate_namespace,
)

pytestmark = pytest.mark.unit


class TestValidateNamespace:
    def test_accepts_plain_and_hierarchical(self) -> None:
        for ns in (
            "nexus",
            "shared",
            "tp:tenant:0b7e9c1a-1111-2222-3333-444455556666:team:abc",
            "a.b-c_d:e",
            "0start-with-digit",
        ):
            assert validate_namespace(ns) == ns

    def test_rejects_invalid(self) -> None:
        for bad in ("", "UPPER", "с-кириллицей", "space here", ":starts-with-colon", "a" * 201):
            with pytest.raises(ValueError):
                validate_namespace(bad)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            validate_namespace(None)  # type: ignore[arg-type]


class TestResolveNamespace:
    def test_empty_falls_back_to_default(self) -> None:
        assert resolve_namespace("", "tp:tenant:x") == "tp:tenant:x"

    def test_empty_without_default_uses_fallback(self) -> None:
        assert resolve_namespace("") == FALLBACK_NAMESPACE

    def test_explicit_wins_over_default(self) -> None:
        assert resolve_namespace(SHARED_NAMESPACE, "nexus") == SHARED_NAMESPACE


class TestResolveNamespaces:
    def test_empty_list_resolves_to_default(self) -> None:
        assert resolve_namespaces([], "tp:tenant:x") == ["tp:tenant:x"]
        assert resolve_namespaces(None) == [FALLBACK_NAMESPACE]

    def test_dedup_preserves_order(self) -> None:
        assert resolve_namespaces(["b", "a", "b"]) == ["b", "a"]

    def test_too_many_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_namespaces([f"ns{i}" for i in range(MAX_READ_NAMESPACES + 1)])

    def test_invalid_item_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_namespaces(["ok", "NOT OK"])
