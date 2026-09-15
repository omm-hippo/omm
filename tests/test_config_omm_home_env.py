from __future__ import annotations

from pathlib import Path

import pytest

from omm import config


def test_omm_home_env_override_takes_precedence(monkeypatch, tmp_path):
    custom = tmp_path / "custom-omm"
    monkeypatch.setenv("OMM_HOME", str(custom))

    assert config._resolve_omm_home() == custom


def test_omm_home_env_override_expands_user(monkeypatch):
    monkeypatch.setenv("OMM_HOME", "~/custom-omm-dir")

    assert config._resolve_omm_home() == Path.home() / "custom-omm-dir"


def test_omm_home_falls_back_to_default_without_env(monkeypatch):
    monkeypatch.delenv("OMM_HOME", raising=False)

    assert config._resolve_omm_home() == Path.home() / ".omm"


def test_omm_home_falls_back_to_default_when_env_is_blank(monkeypatch):
    monkeypatch.setenv("OMM_HOME", "")

    assert config._resolve_omm_home() == Path.home() / ".omm"


def test_relative_omm_home_is_refused(monkeypatch):
    monkeypatch.setenv("OMM_HOME", ".omm-cache")

    with pytest.raises(SystemExit, match="non-absolute"):
        config._resolve_omm_home()


def test_home_directory_itself_is_refused(monkeypatch):
    monkeypatch.setenv("OMM_HOME", str(Path.home()))

    with pytest.raises(SystemExit, match="unsafe"):
        config._resolve_omm_home()


def test_filesystem_root_is_refused(monkeypatch):
    monkeypatch.setenv("OMM_HOME", Path.home().anchor)

    with pytest.raises(SystemExit, match="unsafe"):
        config._resolve_omm_home()
