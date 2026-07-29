from __future__ import annotations

from pathlib import Path

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
