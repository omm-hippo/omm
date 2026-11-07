"""Poster art worker settings: the four values from the site, and the token in the keychain.

The token is the one secret: it must never reach argv (visible in `ps`), an
error message, or the settings file.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from omm.cluster import poster_config
from omm.cluster.errors import ClusterError

TOKEN = "a" * 43
ENV = ("# dshs.dev poster art\n"
       "SUPABASE_URL=https://x.supabase.co/\n"
       "SUPABASE_ANON_KEY=anon\n"
       "SITE_URL=https://dshs.dev\n"
       f"TOKEN={TOKEN}\n")


def _ok(argv, **_k):
    return subprocess.CompletedProcess(argv, 0, "", "")


def test_parse_env_reads_four_keys():
    settings, token = poster_config.parse_env(ENV)
    assert settings == poster_config.PosterSettings("https://x.supabase.co", "anon", "https://dshs.dev")
    assert token == TOKEN


def test_parse_env_strips_quotes():
    settings, token = poster_config.parse_env(ENV.replace("anon", '"anon"').replace(f"={TOKEN}", f"='{TOKEN}'"))
    assert settings.anon_key == "anon" and token == TOKEN


@pytest.mark.parametrize("old,new", [
    ("SUPABASE_URL=https://x.supabase.co/", "SUPABASE_URL=http://x.supabase.co"),
    ("SITE_URL=https://dshs.dev", "SITE_URL=https://dshs.dev/?a=1"),
    (f"TOKEN={TOKEN}", "TOKEN=short"),
    (f"TOKEN={TOKEN}", f"TOKEN={'b' * 30} c"),
])
def test_parse_env_rejects(old, new):
    with pytest.raises(ClusterError) as error:
        poster_config.parse_env(ENV.replace(old, new))
    assert TOKEN not in str(error.value) and "b" * 30 not in str(error.value)


def test_missing_key_names_it():
    with pytest.raises(ClusterError) as error:
        poster_config.parse_env(ENV.replace("SITE_URL=https://dshs.dev\n", ""))
    assert "SITE_URL" in str(error.value) and TOKEN not in str(error.value)


def test_store_token_feeds_stdin_not_argv():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return _ok(argv)

    poster_config.store_token(TOKEN, run=run)
    argv, kw = calls[0]
    assert argv == ["/usr/bin/security", "-i"]
    assert TOKEN not in " ".join(argv)
    assert f"add-generic-password -U -s omm-cluster-poster -a worker-token -w {TOKEN}" in kw["input"]


def test_store_token_rejects_a_bad_token_before_running_anything():
    calls = []
    with pytest.raises(ClusterError):
        poster_config.store_token("x y", run=lambda argv, **kw: calls.append(argv))
    assert calls == []


def test_store_token_failure_scrubs_token():
    def run(argv, **_k):
        return subprocess.CompletedProcess(
            argv, 1, f"add-generic-password -U -s omm-cluster-poster -a worker-token -w {TOKEN}\n",
            f"security: add-generic-password -w {TOKEN}: User interaction is not allowed.")

    with pytest.raises(ClusterError) as error:
        poster_config.store_token(TOKEN, run=run)
    assert TOKEN not in str(error.value)
    assert "unlock-keychain" in str(error.value)


def test_read_token():
    def run(argv, **_k):
        assert argv == ["/usr/bin/security", "find-generic-password", "-s", "omm-cluster-poster",
                        "-a", "worker-token", "-w"]
        return subprocess.CompletedProcess(argv, 0, TOKEN + "\n", "")

    assert poster_config.read_token(run=run) == TOKEN


def test_read_token_none_when_missing():
    run = lambda argv, **_k: subprocess.CompletedProcess(argv, 44, "", "could not be found")
    assert poster_config.read_token(run=run) is None


def test_delete_token_ignores_missing():
    run = lambda argv, **_k: subprocess.CompletedProcess(argv, 44, "", "could not be found")
    poster_config.delete_token(run=run)


def test_settings_round_trip(isolated_omm_home):
    settings, _ = poster_config.parse_env(ENV)
    assert poster_config.load_settings() is None
    poster_config.save_settings(settings)
    assert poster_config.load_settings() == settings
    path = poster_config.poster_dir() / "settings.json"
    assert "token" not in json.loads(path.read_text(encoding="utf-8"))
    assert path.stat().st_mode & 0o777 == 0o600
    poster_config.delete_settings()
    poster_config.delete_settings()
    assert poster_config.load_settings() is None
