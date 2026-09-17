import io
import json

import pytest
from rich.console import Console
from typer.testing import CliRunner

from omm import cli, config, theme
from omm.hardware import HardwareInfo


@pytest.fixture
def scan_fixture(monkeypatch):
    info = HardwareInfo(os_name="TestOS", os_version="12", cpu="CPU identity",
                        ram_total_gb=16, ram_available_gb=12, unified_memory=True,
                        gpu_name="GPU identity", vram_total_gb=None, vram_free_gb=None)
    monkeypatch.setattr(cli, "scan_hardware", lambda: info)
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key in {"ollama", "lmstudio"})
    monkeypatch.setattr(cli.scan_import, "find_external_model_identities", lambda: [])
    return info


def test_scan_compact_view_keeps_resources_and_engine_names(scan_fixture):
    result = CliRunner().invoke(cli.app, ["scan", "--quiet"])
    assert result.exit_code == 0, result.output
    for text in ("RAM", "Safe model budget", "hub storage", "Ollama", "LM Studio"):
        assert text in result.stdout
    for text in ("CPU identity", "GPU identity", "TestOS", "installed"):
        assert text not in result.stdout


def test_scan_hides_hardware_identity_but_json_retains_it(scan_fixture):
    # #339: OS/CPU/GPU identity is not shown in the human view (no --details
    # flag either); the JSON schema keeps those fields for scripts.
    assert CliRunner().invoke(cli.app, ["scan", "--details"]).exit_code != 0
    result = CliRunner().invoke(cli.app, ["scan", "--json"])
    data = json.loads(result.stdout)
    assert data["cpu"] == "CPU identity"
    assert data["gpu_name"] == "GPU identity"
    assert data["os"] == "TestOS 12"
    assert data["engines_installed"] == ["ollama", "lmstudio"]


def test_scan_lists_runners_horizontally_without_status(scan_fixture):
    result = CliRunner().invoke(cli.app, ["scan", "--quiet"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    runner_lines = [line for line in lines if "Ollama" in line]
    assert len(runner_lines) == 1 and "LM Studio" in runner_lines[0]
    assert "hardware" not in result.stdout.lower()


@pytest.mark.parametrize("preset", theme.THEME_NAMES)
@pytest.mark.parametrize("width", [40, 80])
def test_scan_presentation_wraps_for_each_theme(scan_fixture, preset, width, monkeypatch):
    config.update_config(theme=preset)
    stream = io.StringIO()
    console = Console(file=stream, width=width, theme=theme.build_rich_theme(preset))
    monkeypatch.setattr(cli, "console", console)
    result = CliRunner().invoke(cli.app, ["scan", "--quiet"])
    assert result.exit_code == 0, result.output
    output = stream.getvalue()
    assert "Ollama" in output and "LM Studio" in output
    assert all(len(line) <= width for line in output.splitlines())
