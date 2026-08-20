import json
import time

import requests

from omm import firebase_auth


class _FakeResp:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


def _cache_file(isolated_omm_home):
    return isolated_omm_home / "firebase_auth.json"


def test_get_id_token_signs_up_anonymously_when_no_cache(isolated_omm_home, monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        assert url == firebase_auth._IDENTITY_TOOLKIT_SIGN_UP_URL
        assert kwargs["json"] == {"returnSecureToken": True}
        return _FakeResp(
            200,
            {"idToken": "id-1", "refreshToken": "refresh-1", "expiresIn": "3600"},
        )

    monkeypatch.setattr(requests, "post", fake_post)

    token = firebase_auth.get_id_token()

    assert token == "id-1"
    assert len(calls) == 1
    cached = json.loads(_cache_file(isolated_omm_home).read_text())
    assert cached["id_token"] == "id-1"
    assert cached["refresh_token"] == "refresh-1"


def test_get_id_token_reuses_unexpired_cache_without_network_call(isolated_omm_home, monkeypatch):
    _cache_file(isolated_omm_home).parent.mkdir(parents=True, exist_ok=True)
    _cache_file(isolated_omm_home).write_text(
        json.dumps({"id_token": "cached", "refresh_token": "r", "expires_at": time.time() + 3600})
    )
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1))

    token = firebase_auth.get_id_token()

    assert token == "cached"
    assert calls == []


def test_get_id_token_refreshes_when_cached_token_near_expiry(isolated_omm_home, monkeypatch):
    _cache_file(isolated_omm_home).parent.mkdir(parents=True, exist_ok=True)
    _cache_file(isolated_omm_home).write_text(
        json.dumps({"id_token": "stale", "refresh_token": "refresh-1", "expires_at": time.time() + 1})
    )

    def fake_post(url, **kwargs):
        assert url == firebase_auth._SECURE_TOKEN_URL
        assert kwargs["data"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
        return _FakeResp(200, {"id_token": "fresh", "refresh_token": "refresh-2", "expires_in": "3600"})

    monkeypatch.setattr(requests, "post", fake_post)

    token = firebase_auth.get_id_token()

    assert token == "fresh"
    cached = json.loads(_cache_file(isolated_omm_home).read_text())
    assert cached["refresh_token"] == "refresh-2"


def test_get_id_token_falls_back_to_sign_up_when_refresh_fails(isolated_omm_home, monkeypatch):
    _cache_file(isolated_omm_home).parent.mkdir(parents=True, exist_ok=True)
    _cache_file(isolated_omm_home).write_text(
        json.dumps({"id_token": "stale", "refresh_token": "dead", "expires_at": time.time() + 1})
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url == firebase_auth._SECURE_TOKEN_URL:
            return _FakeResp(400)
        return _FakeResp(200, {"idToken": "new", "refreshToken": "new-r", "expiresIn": "3600"})

    monkeypatch.setattr(requests, "post", fake_post)

    token = firebase_auth.get_id_token()

    assert token == "new"
    assert calls == [firebase_auth._SECURE_TOKEN_URL, firebase_auth._IDENTITY_TOOLKIT_SIGN_UP_URL]


def test_get_id_token_returns_none_on_network_failure(isolated_omm_home, monkeypatch):
    def raise_network_error(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "post", raise_network_error)

    assert firebase_auth.get_id_token() is None
    assert not _cache_file(isolated_omm_home).exists()


def test_get_id_token_returns_none_on_malformed_response(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(200, {"idToken": "only-half"}))

    assert firebase_auth.get_id_token() is None


def test_save_cache_restricts_acl_to_current_user_on_windows(isolated_omm_home, monkeypatch):
    """POSIX incidentally gets owner-only protection for this refresh-token
    cache from tempfile.mkstemp's 0600 mode; Windows has no equivalent, so
    the file would otherwise inherit its parent directory's ACL - this
    verifies the icacls hardening actually runs on Windows."""
    monkeypatch.setattr(firebase_auth.platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERNAME", "testuser")
    calls = []
    monkeypatch.setattr(
        firebase_auth.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args),
    )

    firebase_auth._save_cache({"id_token": "x", "refresh_token": "y", "expires_at": 0})

    assert len(calls) == 1
    assert calls[0][0] == "icacls"
    assert calls[0][-1] == "testuser:F"


def test_save_cache_skips_acl_hardening_off_windows(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(firebase_auth.platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(firebase_auth.subprocess, "run", lambda *a, **k: calls.append(a))

    firebase_auth._save_cache({"id_token": "x", "refresh_token": "y", "expires_at": 0})

    assert calls == []


def test_harden_windows_file_permissions_swallows_icacls_failure(isolated_omm_home, monkeypatch):
    """Best-effort: icacls missing/failing must never break saving the
    token cache."""
    monkeypatch.setattr(firebase_auth.platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERNAME", "testuser")

    def raise_missing(*a, **k):
        raise OSError("icacls not found")

    monkeypatch.setattr(firebase_auth.subprocess, "run", raise_missing)

    firebase_auth._harden_windows_file_permissions(_cache_file(isolated_omm_home))
