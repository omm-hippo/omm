"""Employee token store for the cluster gateway (#503)."""

from __future__ import annotations

import stat

import pytest

from omm.cluster import tokens


def test_add_then_verify_returns_name_and_never_stores_plaintext(isolated_omm_home):
    token = tokens.add_token("영업팀-kim")
    assert token.startswith("omm_")
    assert tokens.verify(token) == "영업팀-kim"
    assert token not in tokens.tokens_path().read_text(encoding="utf-8")
    assert stat.S_IMODE(tokens.tokens_path().stat().st_mode) == 0o600


def test_revoked_token_is_rejected_by_a_live_store(isolated_omm_home):
    token = tokens.add_token("kim")
    store = tokens.TokenStore()
    assert store.verify(token) == "kim"
    assert tokens.revoke_token("kim") is True
    assert store.verify(token) is None
    assert tokens.revoke_token("kim") is False


def test_duplicate_and_invalid_names_raise(isolated_omm_home):
    tokens.add_token("kim")
    with pytest.raises(tokens.TokenError):
        tokens.add_token("kim")
    with pytest.raises(tokens.TokenError):
        tokens.add_token("bad name")
    with pytest.raises(tokens.TokenError):
        tokens.add_token("x" * 41)


def test_list_tokens_returns_names_without_hashes(isolated_omm_home):
    tokens.add_token("kim")
    tokens.add_token("lee")
    records = tokens.list_tokens()
    assert [r.name for r in records] == ["kim", "lee"]
    assert all(r.created_at.endswith("Z") for r in records)


def test_empty_or_wrong_prefix_token_is_rejected(isolated_omm_home):
    tokens.add_token("kim")
    assert tokens.verify("") is None
    assert tokens.verify("not-a-token") is None


def test_corrupt_file_fails_closed_and_is_not_overwritten(isolated_omm_home):
    token = tokens.add_token("kim")
    tokens.tokens_path().write_text("{not json", encoding="utf-8")
    assert tokens.verify(token) is None
    assert tokens.TokenStore().verify(token) is None
    with pytest.raises(tokens.TokenError, match="tokens.json"):
        tokens.list_tokens()
    with pytest.raises(tokens.TokenError, match="tokens.json"):
        tokens.add_token("lee")
    assert tokens.tokens_path().read_text(encoding="utf-8") == "{not json"


def test_store_with_no_file_authenticates_nobody(isolated_omm_home):
    assert tokens.TokenStore().verify("omm_anything") is None
