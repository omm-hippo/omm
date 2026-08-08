from __future__ import annotations

from typer.testing import CliRunner

from omm import cli
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="macOS",
        os_version="test",
        cpu="Apple Silicon",
        ram_total_gb=24,
        ram_available_gb=10,
        unified_memory=True,
        gpu_name="Apple GPU",
        vram_total_gb=24,
        vram_free_gb=10,
    )


def test_scan_displays_live_safe_budget(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Safe model budget now" in result.stdout
    assert "7.6 GB" in result.stdout
    assert "Reserved for apps/OS" in result.stdout


def test_scan_clears_stale_link_record_for_uninstalled_engine(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key != "jan")
    cli.registry.upsert_entry("model.gguf", linked={"jan": True, "ollama": True})

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Cleared stale link record" in result.stdout
    assert "model.gguf" in result.stdout
    reg = cli.registry.load_registry()
    assert reg["model.gguf"]["linked"] == {"jan": False, "ollama": True}


def test_scan_leaves_link_record_untouched_when_engine_still_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: True)
    cli.registry.upsert_entry("model.gguf", linked={"jan": True, "ollama": True})

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Cleared stale link record" not in result.stdout
    reg = cli.registry.load_registry()
    assert reg["model.gguf"]["linked"] == {"jan": True, "ollama": True}


def test_scan_runner_table_shows_only_installed_engines(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Ollama" in result.stdout
    assert "LM Studio" not in result.stdout
    assert "not detected" not in result.stdout


def test_scan_runner_table_notes_missing_engine_count_with_link(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    missing_count = len(cli.linker.ENGINES) - 1
    assert f"+ {missing_count} program(s) not installed" in result.stdout
    assert cli.COMPATIBLE_PROGRAMS_URL in result.stdout


def test_scan_runner_table_omits_note_when_all_engines_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: True)

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "not installed" not in result.stdout


def test_tune_uses_live_budget_for_installed_model(monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(
        cli.registry,
        "load_registry",
        lambda: {
            "model-7B-Q4.gguf": {
                "repo_id": "org/model-GGUF",
                "size_bytes": 4 * 1024**3,
            }
        },
    )

    result = runner.invoke(cli.app, ["tune", "model-7B-Q4.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "Safe model budget now" in result.stdout
    assert "7.6 GB" in result.stdout
    assert "Context length" in result.stdout
