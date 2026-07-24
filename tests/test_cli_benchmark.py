from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import cli, config, registry
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=16,
        ram_available_gb=12,
        unified_memory=False,
        gpu_name=None,
        vram_total_gb=None,
        vram_free_gb=None,
    )


def _full_report():
    return {
        "schema_version": 1,
        "pack": {"id": "localfit-gsm8k-bilingual-smoke", "version": "1.1.0"},
        "models": [
            {
                "tag": "small:latest",
                "parameter_size": "1B",
                "quantization_level": "Q4_K_M",
                "size_bytes": 900_000_000,
                "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
                "speed": {
                    "median_tokens_per_sec": 42.5,
                    "samples_tokens_per_sec": [41.0, 42.5, 44.0],
                    "runs": 3,
                },
            }
        ],
    }


def test_benchmark_saves_local_report_and_asks_before_upload(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert "6/8 (75.0%)" in result.stdout
    assert "42.5 tok/s" in result.stdout
    paths = list(config.EVALUATIONS_DIR.glob("quality-*.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text()) == _full_report()
    assert "leaderboard" in result.stdout
    assert sent == []


def test_benchmark_uploads_when_confirmed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
    event = sent[0]
    assert event["model_installed"] == "small:latest"
    assert event["model_size_bytes"] == 900_000_000
    assert event["sample_count"] == 3
    assert event["tokens_per_sec_min"] == 41.0
    assert event["tokens_per_sec_max"] == 44.0
    assert event["quality_pack_id"] == "localfit-gsm8k-bilingual-smoke"
    assert event["quality_correct"] == 6
    assert event["quality_total"] == 8
    assert event["quality_accuracy"] == 0.75


def test_benchmark_reports_model_provider_from_registry_entry(isolated_omm_home, monkeypatch):
    """Verify that benchmark telemetry includes the model's actual provider from registry,
    not a hardcoded huggingface default."""
    # Set up a registry entry with a non-huggingface provider
    registry.save_registry({
        "small.gguf": {
            "ollama_name": "small:latest",
            "repo_id": "org/small",
            "provider": "modelscope",
            "linked": {}
        }
    })
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
    event = sent[0]
    # The provider should come from the registry entry, not default to huggingface
    assert event["model_provider"] == "modelscope"


def test_benchmark_defaults_provider_to_huggingface_when_entry_not_found(isolated_omm_home, monkeypatch):
    """When a model is not found in registry, provider should default to huggingface."""
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    # Don't set up any registry entry - model won't be found
    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
    event = sent[0]
    # Should default to huggingface when entry is not found
    assert event["model_provider"] == "huggingface"


def test_benchmark_never_uploads_when_policy_never(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert sent == []


def test_benchmark_uploads_without_confirm_when_policy_always(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1


def test_benchmark_resolves_numeric_arg_to_ollama_tag(isolated_omm_home, monkeypatch):
    filename = "small.gguf"
    registry.save_registry({filename: {"ollama_name": "small:latest", "linked": {}}})
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda: [filename])
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    seen = {}

    def fake_collect_evidence(models, *a, **k):
        seen["models"] = models
        raise cli.quality_mod.QualityEvaluationError("stop here, we only care about the arg")

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)

    runner.invoke(cli.app, ["benchmark", "1"])

    assert seen["models"] == ["small:latest"]


def test_benchmark_numeric_arg_without_ollama_tag(isolated_omm_home, monkeypatch):
    filename = "small.gguf"
    registry.save_registry({filename: {"linked": {}}})
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda: [filename])

    result = runner.invoke(cli.app, ["benchmark", "1"])

    assert result.exit_code == 1
    assert "no Ollama tag" in result.stderr


def test_benchmark_stops_when_ollama_is_not_running(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 1
    assert "Ollama is not running" in result.stderr


def test_benchmark_declines_starting_daemon_when_prompted(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.benchmark,
        "start_ollama_daemon",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not start")),
    )

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 1
    assert "Ollama is not running" in result.stderr


def _mixed_report():
    return {
        "schema_version": 1,
        "pack": {"id": "localfit-gsm8k-bilingual-smoke", "version": "1.1.0"},
        "environment": {"engine_version": "0.32.1"},
        "models": [
            {
                "tag": "small:latest",
                "outcome": "success",
                "parameter_size": "1B",
                "quantization_level": "Q4_K_M",
                "size_bytes": 900_000_000,
                "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
                "speed": {
                    "median_tokens_per_sec": 42.5,
                    "samples_tokens_per_sec": [41.0, 42.5, 44.0],
                    "runs": 3,
                },
            },
            {
                "tag": "big:latest",
                "outcome": "model_unfit",
                "failure_reason": "out_of_memory",
                "model_metadata": {"parameter_size": "70B", "quantization_level": "Q4_K_M"},
                "attempted_runtime": {
                    "context_length": 4096, "gpu_offload_percent": 100, "cpu_threads": 8, "num_batch": 512,
                },
            },
            {
                "tag": "flaky:latest",
                "outcome": "transient_error",
                "failure_reason": "connection_error",
            },
        ],
    }


def _all_failed_report():
    report = _mixed_report()
    report["models"] = [m for m in report["models"] if m["outcome"] != "success"]
    return report


def test_benchmark_summary_reports_mixed_outcomes_and_uploads_all_of_them(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _mixed_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest", "big:latest", "flaky:latest"])

    assert result.exit_code == 0, result.stdout
    assert "1 succeeded, 1 model_unfit, 0 performance_unfit, 1 transient_error" in result.stdout
    assert "small:latest" in result.stdout and "42.5 tok/s" in result.stdout
    # Only the succeeded model appears in the results table (it's the only
    # one with quality/speed columns to show); failures get a plain notice.
    assert "big:latest" in result.stderr and "out_of_memory" in result.stderr
    assert "flaky:latest" in result.stderr and "connection_error" in result.stderr

    assert len(sent) == 3
    by_tag = {event["model_installed"]: event for event in sent}
    assert "outcome" not in by_tag["small:latest"]

    unfit_event = by_tag["big:latest"]
    assert unfit_event["benchmark_version"] == 7
    assert unfit_event["outcome"] == "model_unfit"
    assert unfit_event["failure_reason"] == "out_of_memory"
    assert "tokens_per_sec" not in unfit_event
    assert "sample_count" not in unfit_event
    assert unfit_event["context_length"] == 4096

    transient_event = by_tag["flaky:latest"]
    assert transient_event["benchmark_version"] == 7
    assert transient_event["outcome"] == "transient_error"
    assert transient_event["failure_reason"] == "connection_error"
    assert "tokens_per_sec" not in transient_event


def test_benchmark_exits_nonzero_only_when_every_model_fails(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _all_failed_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["benchmark", "big:latest", "flaky:latest"])

    assert result.exit_code == 1, result.stdout
    assert "0 succeeded, 1 model_unfit, 0 performance_unfit, 1 transient_error" in result.stdout


def test_benchmark_does_not_ask_to_upload_when_declined_for_mixed_outcomes(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _mixed_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest", "big:latest", "flaky:latest"])

    assert result.exit_code == 0, result.stdout
    assert sent == []


def test_benchmark_starts_and_stops_daemon_when_confirmed(isolated_omm_home, monkeypatch):
    reachable = {"value": False}
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: reachable["value"])
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    started = object()
    stopped = []

    def _start(*a, **k):
        reachable["value"] = True
        return started

    monkeypatch.setattr(cli.benchmark, "start_ollama_daemon", _start)
    monkeypatch.setattr(cli.benchmark, "stop_ollama_daemon", lambda proc: stopped.append(proc))

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert stopped == [started]


# --- --confirm-performance-timeout wiring and performance_unfit upload ----


def _performance_unfit_report():
    return {
        "schema_version": 1,
        "pack": {"id": "localfit-gsm8k-bilingual-smoke", "version": "1.1.0"},
        "environment": {"engine_version": "0.32.1"},
        "models": [
            {
                "tag": "big:latest",
                "outcome": "performance_unfit",
                "failure_reason": "confirmed_generation_timeout",
                "confirmation_attempts": 2,
                "timeout_seconds": 180,
                "model_metadata": {"parameter_size": "32B", "quantization_level": "Q8_0"},
                "attempted_runtime": {
                    "context_length": 4096, "gpu_offload_percent": 20, "cpu_threads": 8, "num_batch": 512,
                },
            },
        ],
    }


def test_confirm_performance_timeout_flag_is_forwarded_to_collect_evidence(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    seen = {}

    def fake_collect_evidence(models, hw, pack_path=None, speed_runs=3, confirm_performance_timeout=False):
        seen["confirm_performance_timeout"] = confirm_performance_timeout
        return _full_report()

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    runner.invoke(cli.app, ["benchmark", "small:latest", "--confirm-performance-timeout"])

    assert seen["confirm_performance_timeout"] is True


def test_confirm_performance_timeout_flag_defaults_to_false(isolated_omm_home, monkeypatch):
    """Never auto-runs the second attempt without the explicit flag."""
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    seen = {}

    def fake_collect_evidence(models, hw, pack_path=None, speed_runs=3, confirm_performance_timeout=False):
        seen["confirm_performance_timeout"] = confirm_performance_timeout
        return _full_report()

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert seen["confirm_performance_timeout"] is False


def test_benchmark_reports_and_uploads_performance_unfit_outcome(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _performance_unfit_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "big:latest", "--confirm-performance-timeout"])

    assert result.exit_code == 1, result.stdout  # zero successes
    assert "0 succeeded, 0 model_unfit, 1 performance_unfit, 0 transient_error" in result.stdout
    assert "big:latest" in result.stderr and "performance_unfit" in result.stderr

    assert len(sent) == 1  # 8. exactly one Firebase event for this tag - no duplicate upload
    event = sent[0]
    assert event["benchmark_version"] == 7
    assert event["outcome"] == "performance_unfit"
    assert event["failure_reason"] == "confirmed_generation_timeout"
    assert event["confirmation_attempts"] == 2
    assert event["timeout_seconds"] == 180
    for forbidden in ("tokens_per_sec", "tokens_per_sec_min", "tokens_per_sec_max", "sample_count"):
        assert forbidden not in event


def test_performance_unfit_upload_rejected_when_confirmation_attempts_is_not_two(isolated_omm_home, monkeypatch):
    """A malformed performance_unfit (wrong attempt count) is never
    uploaded - the Rules would reject it anyway, so drop it client-side."""
    config.update_config(telemetry_send_policy="always")
    report = _performance_unfit_report()
    report["models"][0]["confirmation_attempts"] = 1
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: report)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    runner.invoke(cli.app, ["benchmark", "big:latest", "--confirm-performance-timeout"])

    assert sent == []


def test_performance_unfit_upload_rejected_when_timeout_seconds_missing(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    report = _performance_unfit_report()
    del report["models"][0]["timeout_seconds"]
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: report)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    runner.invoke(cli.app, ["benchmark", "big:latest", "--confirm-performance-timeout"])

    assert sent == []
