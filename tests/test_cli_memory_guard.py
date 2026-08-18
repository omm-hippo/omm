from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import cli, config, memory_guard
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware(available=12.0):
    return HardwareInfo(
        os_name="macOS",
        os_version="",
        cpu="CPU",
        ram_total_gb=16,
        ram_available_gb=available,
        unified_memory=True,
        gpu_name="GPU",
        vram_total_gb=16,
        vram_free_gb=available,
    )


def test_memory_guard_setting_updates_bounded_policy_and_timings(isolated_omm_home):
    result = runner.invoke(
        cli.app,
        [
            "setting",
            "memory-guard",
            "--policy",
            "observe",
            "--poll-seconds",
            "2",
            "--low-memory-seconds",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    saved = config.load_config()
    assert saved["memory_guard_policy"] == "observe"
    assert saved["memory_guard_poll_seconds"] == 2
    assert saved["memory_guard_low_memory_seconds"] == 5


def test_invalid_stored_memory_guard_values_fall_back_safely(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "memory_guard_policy": "kill-everything",
                "memory_guard_poll_seconds": 0,
                "memory_guard_low_memory_seconds": "forever",
            }
        )
    )

    loaded = config.load_config()

    assert loaded["memory_guard_policy"] == "ask"
    assert loaded["memory_guard_poll_seconds"] == 1.0
    assert loaded["memory_guard_low_memory_seconds"] == 3.0


class _FakeManagedRuntime:
    def __init__(self, registry_data):
        self.resident = memory_guard.ResidentModel("ollama", "old", 4.0, True)
        self.unloaded = []

    def list_residents(self):
        return () if self.unloaded else (self.resident,)

    def unload(self, resident):
        self.unloaded.append(resident.model_id)
        return True

    def is_resident(self, resident):
        return False


def test_cli_guard_denied_consent_does_not_unload(isolated_omm_home, monkeypatch):
    runtime = _FakeManagedRuntime({})
    monkeypatch.setattr(
        cli.memory_guard_mod, "OllamaManagedRuntime", lambda registry_data: runtime
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware())
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda prompt: False)

    allowed, _runtime, _preloaded = cli._guard_ollama_load("new", 12.0)

    assert allowed is False
    assert runtime.unloaded == []


def test_cli_guard_unloads_owned_model_and_recalculates(isolated_omm_home, monkeypatch):
    runtime = _FakeManagedRuntime({})
    scans = iter([_hardware(12.0), _hardware(15.0)])
    monkeypatch.setattr(
        cli.memory_guard_mod, "OllamaManagedRuntime", lambda registry_data: runtime
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: next(scans))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda prompt: True)

    allowed, _runtime, _preloaded = cli._guard_ollama_load("new", 12.0)

    assert allowed is True
    assert runtime.unloaded == ["old"]


def test_cli_guard_preserves_preloaded_target_across_ollama_aliases(
    isolated_omm_home, monkeypatch
):
    runtime = _FakeManagedRuntime({})
    runtime.resident = memory_guard.ResidentModel("ollama", "new:latest", 12.0, True)
    monkeypatch.setattr(
        cli.memory_guard_mod, "OllamaManagedRuntime", lambda registry_data: runtime
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware(available=1.0))
    monkeypatch.setattr(
        cli,
        "_ask_confirm",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    allowed, _runtime, preloaded = cli._guard_ollama_load("new", 12.0)

    assert allowed is True
    assert preloaded is True
    assert runtime.unloaded == []


def test_memory_pressure_report_tells_the_user_what_to_do():
    """"Memory Guard detected sustained low memory and cancelled OMM's
    model operation." is a verdict with no next step attached."""
    verdict, hint = cli._memory_pressure_report_lines("ollama", cancelled=True)

    assert "cancelled OMM's model operation" in verdict
    assert "Close memory-heavy apps" in hint


def test_memory_pressure_report_names_the_engine_when_the_unload_was_not_confirmed():
    """An unconfirmed cancellation means the engine may still hold the
    weights, which closing other apps cannot release - so this branch has to
    point at the engine that was actually in use."""
    verdict, hint = cli._memory_pressure_report_lines("lmstudio", cancelled=False)

    assert "could not confirm cancellation" in verdict
    assert "Restart LM Studio" in hint
