from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


def test_add_storage_saved_bytes_accumulates(isolated_omm_home):
    first = config.add_storage_saved_bytes(1024)
    second = config.add_storage_saved_bytes(2048)

    assert first == 1024
    assert second == 3072
    assert config.load_config()["storage_saved_bytes"] == 3072


def test_load_config_sanitizes_invalid_storage_saved_bytes(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"storage_saved_bytes": -5}))

    assert config.load_config()["storage_saved_bytes"] == 0


def test_scan_json_reports_hub_storage_and_saved_bytes(isolated_omm_home, monkeypatch):
    config.add_storage_saved_bytes(2 * 1024**3)
    monkeypatch.setattr(
        cli.registry, "load_registry", lambda: {"model.gguf": {"size_bytes": 1024**3}}
    )
    monkeypatch.setattr(cli.scan_import, "find_external_model_identities", lambda: [])

    result = runner.invoke(cli.app, ["scan", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["hub_storage_gb"] == 1.0
    assert data["storage_saved_gb"] == 2.0


def test_scan_table_shows_hub_storage_and_saved_rows(isolated_omm_home, monkeypatch):
    config.add_storage_saved_bytes(512 * 1024**2)
    monkeypatch.setattr(
        cli.registry, "load_registry", lambda: {"model.gguf": {"size_bytes": 1024**3}}
    )
    monkeypatch.setattr(cli.scan_import, "find_external_model_identities", lambda: [])

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "omm hub storage" in result.stdout
    assert "1.0 GB" in result.stdout
    assert "Saved via omm import" in result.stdout
    assert "0.5 GB" in result.stdout


def test_scan_falls_back_to_stat_when_size_bytes_missing(isolated_omm_home, monkeypatch):
    model_path = cli.MODELS_DIR / "model.gguf"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"x" * 1024**2)
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})
    monkeypatch.setattr(cli.scan_import, "find_external_model_identities", lambda: [])

    result = runner.invoke(cli.app, ["scan", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["hub_storage_gb"] > 0


def test_import_records_saved_bytes_into_config(isolated_omm_home, monkeypatch):
    group = SimpleNamespace(
        sha256="deadbeef", display_name="model.gguf", size_bytes=1024**3, engines=["ollama"]
    )
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: SimpleNamespace(filename="model.gguf", bytes_saved=1024**3, link_warnings=[]),
    )
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["import", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["storage_saved_bytes"] == 1024**3


def test_import_with_zero_bytes_saved_does_not_touch_counter(isolated_omm_home, monkeypatch):
    group = SimpleNamespace(
        sha256="deadbeef", display_name="model.gguf", size_bytes=1024**3, engines=["ollama"]
    )
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: SimpleNamespace(filename="model.gguf", bytes_saved=0, link_warnings=[]),
    )
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["import", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["storage_saved_bytes"] == 0
