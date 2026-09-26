"""Unit-тесты auth-грантов движка памяти (CB_API_KEYS, ADR-054)."""

from __future__ import annotations

import pytest

from platform_memory.server.auth import (
    ApiKeysConfigError,
    CallerGrants,
    NamespaceGrant,
    check_grants,
    full_access,
    match_key,
    parse_api_keys,
)

pytestmark = pytest.mark.unit

_KEYS_JSON = (
    '[{"name": "tp-api", "key": "tp-secret", "grants": ['
    '{"prefix": "tp:", "read": true, "write": true},'
    '{"prefix": "shared", "read": true, "write": false}]},'
    '{"name": "nexus", "key": "nexus-secret", "grants": [{"prefix": ""}]}]'
)


class TestParseApiKeys:
    def test_parses_registry(self) -> None:
        keys = parse_api_keys(_KEYS_JSON)
        assert set(keys) == {"tp-secret", "nexus-secret"}
        tp = keys["tp-secret"]
        assert tp.name == "tp-api"
        assert tp.grants[0] == NamespaceGrant(prefix="tp:", read=True, write=True)
        assert tp.grants[1] == NamespaceGrant(prefix="shared", read=True, write=False)

    def test_empty_string_means_no_registry(self) -> None:
        assert parse_api_keys("") == {}
        assert parse_api_keys("   ") == {}

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ApiKeysConfigError):
            parse_api_keys("{not json")

    def test_missing_key_or_grants_rejected(self) -> None:
        with pytest.raises(ApiKeysConfigError):
            parse_api_keys('[{"name": "x"}]')
        with pytest.raises(ApiKeysConfigError):
            parse_api_keys('[{"key": "k", "grants": []}]')
        with pytest.raises(ApiKeysConfigError):
            parse_api_keys('[{"key": "k", "grants": [{"read": true}]}]')


class TestMatchKey:
    def test_finds_by_exact_token(self) -> None:
        keys = parse_api_keys(_KEYS_JSON)
        assert match_key("tp-secret", keys) is not None
        assert match_key("tp-secret", keys).name == "tp-api"  # type: ignore[union-attr]
        assert match_key("wrong", keys) is None


class TestCheckGrants:
    @pytest.fixture
    def tp_grants(self) -> CallerGrants:
        return parse_api_keys(_KEYS_JSON)["tp-secret"]

    def test_prefix_read_write(self, tp_grants: CallerGrants) -> None:
        ns = "tp:tenant:x:team:y"
        assert check_grants(tp_grants, [ns], write=False) is None
        assert check_grants(tp_grants, [ns], write=True) is None

    def test_shared_read_only(self, tp_grants: CallerGrants) -> None:
        assert check_grants(tp_grants, ["shared"], write=False) is None
        assert check_grants(tp_grants, ["shared"], write=True) == "shared"

    def test_foreign_namespace_denied(self, tp_grants: CallerGrants) -> None:
        assert check_grants(tp_grants, ["nexus"], write=False) == "nexus"

    def test_mixed_scope_reports_first_denied(self, tp_grants: CallerGrants) -> None:
        assert check_grants(tp_grants, ["tp:tenant:x", "nexus"], write=False) == "nexus"

    def test_full_access_and_local_mode(self) -> None:
        assert check_grants(full_access("legacy"), ["anything", ""], write=True) is None
        # grants=None — авторизация не настроена (локальный режим): всё разрешено.
        assert check_grants(None, ["anything"], write=True) is None

    def test_empty_prefix_grant_covers_global_stats(self) -> None:
        nexus = parse_api_keys(_KEYS_JSON)["nexus-secret"]
        assert check_grants(nexus, [""], write=False) is None
        tp = parse_api_keys(_KEYS_JSON)["tp-secret"]
        assert check_grants(tp, [""], write=False) == ""


class TestPiiFlag:
    def test_pii_flag_parsed_and_defaults_false(self) -> None:
        keys = parse_api_keys(
            '[{"key": "a", "pii": true, "grants": [{"prefix": ""}]},'
            '{"key": "b", "grants": [{"prefix": ""}]}]'
        )
        assert keys["a"].pii is True
        assert keys["b"].pii is False
        assert full_access("legacy").pii is False
        assert full_access("legacy", pii=True).pii is True
