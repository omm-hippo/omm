"""`omm update`'s provider-facts step (up to 32 sequential HTTP calls,
~1 minute) must not block the parent process. Mirrors the detached-child
pattern already proven by `_spawn_bg_version_check`/`_bg-version-check`
(see test_cli_update_notice.py)."""

import sys

import pytest

from omm import cli, config
from omm.atomic import locked

pytestmark = pytest.mark.usefixtures("isolated_omm_home")


def test_spawn_provider_facts_refresh_uses_detached_popen_posix(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    popen_calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: popen_calls.append((args, kwargs)))

    cli._spawn_provider_facts_refresh()

    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert args == [sys.executable, "-m", "omm.cli", "_provider-facts-refresh-run"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is cli.subprocess.DEVNULL
    assert kwargs["stdout"] is cli.subprocess.DEVNULL
    assert kwargs["stderr"] is cli.subprocess.DEVNULL


def test_spawn_provider_facts_refresh_windows_uses_detached_process_flags(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
    monkeypatch.setattr(cli.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    popen_calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: popen_calls.append((args, kwargs)))

    cli._spawn_provider_facts_refresh()

    assert len(popen_calls) == 1
    _args, kwargs = popen_calls[0]
    assert kwargs["creationflags"] == 0x00000008 | 0x00000200
    assert "start_new_session" not in kwargs


def test_refresh_data_spawns_background_refresh_instead_of_blocking(monkeypatch):
    """`_refresh_data()` (what `omm update` calls) must hand the slow
    provider-facts fetch to the detached child rather than call
    `recommend_facts.refresh` itself - that synchronous path stays reserved
    for `omm recommend --refresh-metadata`, where the user explicitly asked
    to wait."""
    artifact = {"candidates": [{"repo_id": "a/b"}], "trees": [{"leaf": True, "value": 1}]}
    monkeypatch.setattr(cli.predictor, "fetch_and_cache_model", lambda *a, **k: artifact)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: {"rules_url": None, "model_url": "https://example.com/model.json"},
    )
    from omm import recommend_facts

    monkeypatch.setattr(
        recommend_facts, "refresh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not block"))
    )
    spawn_calls = []
    monkeypatch.setattr(cli, "_spawn_provider_facts_refresh", lambda: spawn_calls.append(1))

    cli._refresh_data()

    assert spawn_calls == [1]


def test_refresh_data_does_not_spawn_when_artifact_has_no_trees(monkeypatch):
    artifact = {"candidates": []}
    monkeypatch.setattr(cli.predictor, "fetch_and_cache_model", lambda *a, **k: artifact)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: {"rules_url": None, "model_url": "https://example.com/model.json"},
    )
    monkeypatch.setattr(
        cli, "_spawn_provider_facts_refresh", lambda: (_ for _ in ()).throw(AssertionError("no spawn"))
    )

    cli._refresh_data()


def test_provider_facts_refresh_run_cmd_uses_cached_model_and_live_hardware(monkeypatch):
    artifact = {"candidates": [{"repo_id": "a/b"}], "trees": [{"leaf": True, "value": 1}]}
    hw = object()
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: artifact)
    monkeypatch.setattr(cli, "scan_hardware", lambda: hw)
    from omm import recommend_facts

    calls = []
    monkeypatch.setattr(recommend_facts, "refresh", lambda a, h: calls.append((a, h)))

    cli._provider_facts_refresh_run_cmd()

    assert calls == [(artifact, hw)]


def test_provider_facts_refresh_run_cmd_noop_when_no_cached_model(monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    from omm import recommend_facts

    monkeypatch.setattr(
        recommend_facts, "refresh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no refresh"))
    )

    cli._provider_facts_refresh_run_cmd()


def test_provider_facts_refresh_run_cmd_skips_when_already_running(monkeypatch):
    """A second `omm update` launched while the first background refresh is
    still in flight must not fetch a duplicate set of repositories."""
    artifact = {"candidates": [{"repo_id": "a/b"}], "trees": [{"leaf": True, "value": 1}]}
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: artifact)
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    from omm import recommend_facts

    monkeypatch.setattr(
        recommend_facts, "refresh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no refresh"))
    )

    with locked(config.OMM_HOME / "locks" / "provider-facts-refresh", timeout=0):
        cli._provider_facts_refresh_run_cmd()
