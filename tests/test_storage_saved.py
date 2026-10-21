from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from omm import cli, config, linker

runner = CliRunner()


def test_add_storage_saved_bytes_accumulates(isolated_omm_home):
    first = config.add_storage_saved_bytes(1024)
    second = config.add_storage_saved_bytes(2048)

    assert first == 1024
    assert second == 3072
    assert config.load_config()["storage_saved_bytes"] == 3072


def test_load_config_sanitizes_invalid_storage_saved_bytes(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"storage_saved_bytes": -5}), encoding="utf-8")

    assert config.load_config()["storage_saved_bytes"] == 0


def test_scan_json_reports_hub_storage_and_saved_bytes(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "shared_link_bytes", lambda: 2 * 1024**3)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"fake-gguf")
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
    monkeypatch.setattr(cli.linker, "shared_link_bytes", lambda: 512 * 1024**2)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"fake-gguf")
    monkeypatch.setattr(
        cli.registry, "load_registry", lambda: {"model.gguf": {"size_bytes": 1024**3}}
    )
    monkeypatch.setattr(cli.scan_import, "find_external_model_identities", lambda: [])

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "omm hub storage" in result.stdout
    assert "1.0 GB" in result.stdout
    assert "saved by linking instead of copying" in result.stdout
    assert "0.5 GB" in result.stdout


def test_shared_link_bytes_counts_owned_links_not_copies(isolated_omm_home, tmp_path, monkeypatch):
    """`omm install` links the hub file into each runner without touching the
    import counter; the saved figure must come from those links themselves."""
    hub = cli.MODELS_DIR / "model.gguf"
    hub.parent.mkdir(parents=True, exist_ok=True)
    hub.write_bytes(b"x" * 4096)
    first = linker.link_file(hub, tmp_path / "runner-a" / "model.gguf")
    second = linker.link_file(hub, tmp_path / "runner-b" / "model.gguf")
    shared = [method for method in (first, second) if method in {"hardlink", "symlink"}]

    assert linker.shared_link_bytes() == 4096 * len(shared)

    (tmp_path / "runner-a" / "model.gguf").unlink()
    expected_after = 4096 * (1 if second in {"hardlink", "symlink"} else 0)
    assert linker.shared_link_bytes() == expected_after


def test_shared_link_bytes_ignores_copies(isolated_omm_home, tmp_path):
    hub = cli.MODELS_DIR / "model.gguf"
    hub.parent.mkdir(parents=True, exist_ok=True)
    hub.write_bytes(b"x" * 4096)

    # Recorded directly: whether link_file falls back to a copy depends on
    # the platform, but an owned copy must never count as shared bytes.
    copy = tmp_path / "runner" / "model.gguf"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(hub.read_bytes())
    linker._record_ownership(copy, hub, "copy")
    assert linker.shared_link_bytes() == 0


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
