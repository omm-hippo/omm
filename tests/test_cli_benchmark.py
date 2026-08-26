from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from omm import cli, config, contribute_memory, registry
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


def _hardware_with_chip_metadata() -> HardwareInfo:
    return HardwareInfo(
        os_name="Linux",
        os_version="",
        cpu="AMD Ryzen 5 5600X",
        ram_total_gb=16,
        ram_available_gb=12,
        unified_memory=False,
        gpu_name="NVIDIA GeForce RTX 4090",
        vram_total_gb=24,
        vram_free_gb=20,
        cpu_arch="x86_64",
        cpu_physical_cores=6,
        cpu_logical_cores=12,
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
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
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


def test_benchmark_memory_guard_matches_exact_ollama_runtime_name(monkeypatch):
    monkeypatch.setattr(
        cli.registry,
        "load_registry",
        lambda: {
            "qwen3-4b.gguf": {
                "ollama_name": "qwen3-4b",
                "ollama_runtime_name": "qwen3:4b",
                "size_bytes": 1024**3,
            }
        },
    )
    calls = []
    monkeypatch.setattr(
        cli,
        "_guard_ollama_load",
        lambda tag, required_gb: calls.append((tag, required_gb)) or (True, None, False),
    )

    cli._guard_benchmark_models(["qwen3:4b"])

    assert calls == [("qwen3:4b", 1.2)]


def test_benchmark_json_before_subcommand(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama"))
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    result = runner.invoke(cli.app, ["--json", "benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data == _full_report()


def test_benchmark_json_after_subcommand(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama"))
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data == _full_report()


def test_benchmark_json_never_prompts_under_ask_policy(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama"))
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("JSON mode must not prompt")),
    )

    result = runner.invoke(cli.app, ["--json", "benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == _full_report()


def test_benchmark_global_yes_is_forwarded_to_daemon_start(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    seen = {}
    monkeypatch.setattr(
        cli,
        "_ensure_engine_running",
        lambda engine, reason, *, assume_yes=False: (
            seen.update(assume_yes=assume_yes) or (engine, None)
        ),
    )

    result = runner.invoke(cli.app, ["--yes", "benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert seen == {"assume_yes": True}


def test_benchmark_all_expands_to_every_installed_tag(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "list_benchmarkable_tags", lambda: ["a:latest", "b:latest"])
    seen_tags = []
    monkeypatch.setattr(
        cli.quality_mod,
        "collect_evidence",
        lambda tags, *a, **k: seen_tags.append(list(tags)) or _full_report(),
    )
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")

    result = runner.invoke(cli.app, ["benchmark", "all"])

    assert result.exit_code == 0, result.stdout
    assert seen_tags == [["a:latest", "b:latest"]]
    assert "Expanding 'all' to 2 model(s)" in result.stdout


def test_benchmark_all_quiet_suppresses_expansion_line(isolated_omm_home, monkeypatch):
    """`--quiet` was wired into this command's progress spinner (issue #81)
    but left the "Expanding 'all' to N model(s)" hint line unconditional."""
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "list_benchmarkable_tags", lambda: ["a:latest", "b:latest"])
    monkeypatch.setattr(
        cli.quality_mod, "collect_evidence", lambda tags, *a, **k: _full_report()
    )
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")

    result = runner.invoke(cli.app, ["benchmark", "all", "--quiet"])

    assert result.exit_code == 0, result.stdout
    assert "Expanding 'all'" not in result.output


def test_benchmark_all_errors_when_nothing_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.quality_mod, "list_benchmarkable_tags", lambda: [])

    result = runner.invoke(cli.app, ["benchmark", "all"])

    assert result.exit_code == 1
    assert "no models" in result.output.lower()


def test_benchmark_all_mixed_with_other_tag_is_rejected(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    result = runner.invoke(cli.app, ["benchmark", "all", "other:latest"])

    assert result.exit_code == 1
    assert "must be the only argument" in result.output


def test_benchmark_shows_progress_per_model(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)

    def fake_collect_evidence(tags, *a, on_model_start=None, **k):
        for index, tag in enumerate(tags, start=1):
            if on_model_start is not None:
                on_model_start(tag, index, len(tags))
        return _full_report()

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert "Benchmarking small:latest (1/1)" in result.stdout


def test_benchmark_uploads_when_confirmed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
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
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
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
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
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


def test_benchmark_reports_missing_ollama_as_missing(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    # omm benchmark now falls back to LM Studio before giving up entirely
    # (_select_benchmark_engine), so its absence needs to be explicit here
    # too or this test would depend on whether LM Studio happens to be
    # installed on the machine running the test suite.
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: None)
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 1
    assert "Neither Ollama nor LM Studio" in result.stderr
    assert "→ Install one of them, start it once, then retry `omm benchmark`." in result.stderr


def test_select_benchmark_engine_prefers_ollama_when_available(monkeypatch):
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama"))
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: "/some/lms")
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: True)

    assert cli._select_benchmark_engine() == "ollama"


def test_select_benchmark_engine_falls_back_to_lmstudio(monkeypatch):
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: "/some/lms")
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)

    assert cli._select_benchmark_engine() == "lmstudio"


def test_select_benchmark_engine_none_when_neither_available(monkeypatch):
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: None)
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)

    assert cli._select_benchmark_engine() is None


def test_benchmark_declines_starting_lmstudio_when_prompted(isolated_omm_home, monkeypatch):
    """Mirrors test_benchmark_declines_starting_daemon_when_prompted for the
    LM Studio fallback path: Ollama entirely absent, LM Studio installed but
    its server isn't running, user declines the start prompt."""
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: "/some/lms")
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.linker,
        "start_lmstudio_daemon",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not start")),
    )

    result = runner.invoke(cli.app, ["benchmark", "some-model-key"])

    assert result.exit_code == 1
    assert "requires LM Studio's local server" in result.stderr


def test_benchmark_falls_back_to_lmstudio_when_ollama_daemon_wont_start(
    isolated_omm_home, monkeypatch
):
    """_select_benchmark_engine only checks that the Ollama executable
    exists, not that its (stopped) daemon can actually come up - a
    corrupted install or port conflict should still fall back to a
    healthy, already-running LM Studio instead of exiting outright."""
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama"))
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "start_ollama_daemon", lambda *a, **k: None)
    monkeypatch.setattr(cli.benchmark, "last_daemon_start_error", lambda: "port already in use")
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: "/some/lms")
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.linker,
        "_lmstudio_list_models",
        lambda lms_path: [
            {
                "type": "llm",
                "modelKey": "qwen2.5-0.5b-instruct",
                "architecture": "qwen2",
                "quantization": {"name": "Q8_0", "bits": 8},
                "paramsString": "630M",
                "maxContextLength": 32768,
                "trainedForToolUse": True,
            },
        ],
    )
    seen = {}

    def fake_collect_evidence(models, *a, engine=None, **k):
        seen["engine"] = engine
        raise cli.quality_mod.QualityEvaluationError("stop here, we only care about the engine")

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)

    result = runner.invoke(cli.app, ["benchmark", "qwen2.5-0.5b-instruct"])

    assert seen["engine"] == "lmstudio"
    assert "falling back to LM Studio" in result.output


def test_benchmark_lmstudio_expands_all_and_rejects_unknown_model(isolated_omm_home, monkeypatch):
    """LM Studio's 'all' expansion and free-form tag validation go through
    _lmstudio_installed_models instead of quality_mod.list_benchmarkable_tags
    - exercise both the expansion and the unknown-model rejection path."""
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: "/some/lms")
    monkeypatch.setattr(
        cli.linker,
        "_lmstudio_list_models",
        lambda lms_path: [
            {
                "type": "llm",
                "modelKey": "qwen2.5-0.5b-instruct",
                "architecture": "qwen2",
                "quantization": {"name": "Q8_0", "bits": 8},
                "paramsString": "630M",
                "maxContextLength": 32768,
                "trainedForToolUse": True,
            },
            {"type": "embedding", "modelKey": "nomic-embed-text"},
        ],
    )

    result = runner.invoke(cli.app, ["benchmark", "does-not-exist"])
    assert result.exit_code == 1
    assert "Not installed in LM Studio" in result.stderr

    seen = {}

    def fake_collect_evidence(models, *a, engine=None, lmstudio_models=None, **k):
        seen["models"] = models
        seen["engine"] = engine
        seen["lmstudio_models"] = lmstudio_models
        raise cli.quality_mod.QualityEvaluationError("stop here, we only care about the args")

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)

    runner.invoke(cli.app, ["benchmark", "all"])

    assert seen["models"] == ["qwen2.5-0.5b-instruct"]
    assert seen["engine"] == "lmstudio"
    assert "qwen2.5-0.5b-instruct" in seen["lmstudio_models"]
    assert "nomic-embed-text" not in seen["lmstudio_models"]


def test_benchmark_declines_starting_daemon_when_prompted(isolated_omm_home, monkeypatch):
    # An existing install, not a fresh one: _root's onboarding gate now
    # covers every subcommand, and on a truly fresh isolated_omm_home the
    # monkeypatched _stdin_is_tty below would also let the real setup
    # wizard fire here first, crashing in its own (unmocked) engine
    # checklist before this test's own scenario ever runs.
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama.exe"))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.benchmark,
        "start_ollama_daemon",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not start")),
    )

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 1
    assert "requires the Ollama API" in result.stderr


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
    assert unfit_event["benchmark_version"] == 8
    assert unfit_event["outcome"] == "model_unfit"
    assert unfit_event["failure_reason"] == "out_of_memory"
    assert "tokens_per_sec" not in unfit_event
    assert "sample_count" not in unfit_event
    assert unfit_event["context_length"] == 4096

    transient_event = by_tag["flaky:latest"]
    assert transient_event["benchmark_version"] == 8
    assert transient_event["outcome"] == "transient_error"
    assert transient_event["failure_reason"] == "connection_error"
    assert "tokens_per_sec" not in transient_event


def test_benchmark_exits_nonzero_only_when_every_model_fails(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _all_failed_report())
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")

    result = runner.invoke(cli.app, ["benchmark", "big:latest", "flaky:latest"])

    assert result.exit_code == 1, result.stdout
    assert "0 succeeded, 1 model_unfit, 0 performance_unfit, 1 transient_error" in result.stdout


def test_benchmark_does_not_ask_to_upload_when_declined_for_mixed_outcomes(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _mixed_report())
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest", "big:latest", "flaky:latest"])

    assert result.exit_code == 0, result.stdout
    assert sent == []


def test_benchmark_starts_and_stops_daemon_when_confirmed(isolated_omm_home, monkeypatch):
    # See test_benchmark_declines_starting_daemon_when_prompted above: the
    # onboarding gate now covers every subcommand, so a truly fresh config
    # plus the monkeypatched _stdin_is_tty below would let the real setup
    # wizard fire before this test's own scenario runs.
    config.update_config(onboarding_completed=True)
    reachable = {"value": False}
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: reachable["value"])
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama.exe"))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
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

    def fake_collect_evidence(
        models, hw, pack_path=None, speed_runs=3, confirm_performance_timeout=False,
        on_model_start=None, on_daemon_event=None, engine="ollama", lmstudio_models=None,
        daemon_ref=None,
    ):
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

    def fake_collect_evidence(
        models, hw, pack_path=None, speed_runs=3, confirm_performance_timeout=False,
        on_model_start=None, on_daemon_event=None, engine="ollama", lmstudio_models=None,
        daemon_ref=None,
    ):
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
    assert event["benchmark_version"] == 8
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


def test_report_telemetry_v8_success_sends_chip_scores_not_raw_names(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware_with_chip_metadata)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    cli._report_telemetry(
        "model-7B-Q4.gguf", "org/model", 42.5,
        size_bytes=4 * 1024**3, sample_count=3, speed_min=40.0, speed_max=45.0,
        model_metadata={"parameter_size": "7B", "quantization_level": "Q4_K_M"},
        runtime={
            "runtime_profile": "explicit_ollama_options", "context_length": 4096,
            "gpu_offload_percent": 100, "cpu_threads": 8, "num_batch": 512,
        },
        engine_version="0.32.1",
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["cpu_score"] == 5600.0
    assert event["cpu_tier"] == 0.0
    assert event["gpu_score"] == 4090.0
    assert event["gpu_tier"] == 0.0
    assert "cpu_model" not in event
    assert "gpu_name" not in event


def test_report_telemetry_v9_labels_clean_contribution(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware_with_chip_metadata)
    sent = []
    monkeypatch.setattr(
        cli.telemetry,
        "send_event",
        lambda event, force=False: sent.append(event) or True,
    )
    estimate = contribute_memory.ContributionMemoryEstimate(
        mapped_weights_ram_gb=0.75,
        committed_ram_gb=0.3,
        required_vram_gb=0.0,
        kv_cache_gb=0.1,
        compute_buffer_gb=0.1,
        runtime_overhead_gb=0.1,
        source="gguf_header",
        confidence="medium",
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        runtime_buffer_ram_gb=0.3,
    )

    cli._report_telemetry(
        "model-1B-Q4.gguf",
        "org/model",
        42.5,
        size_bytes=int(0.75 * 1024**3),
        sample_count=3,
        speed_min=41.0,
        speed_max=44.0,
        speed_samples=[41.0, 42.5, 44.0],
        model_metadata={"parameter_size": "1B", "quantization_level": "Q4_K_M"},
        runtime={
            "runtime_profile": "explicit_ollama_options",
            "context_length": 1024,
            "gpu_offload_percent": 0,
            "cpu_threads": 8,
            "num_batch": 128,
        },
        engine_version="0.32.1",
        memory_measurement={
            "ram_available_before_gb": 2.2,
            "ram_available_min_gb": 2.0,
            "ram_available_after_gb": 2.1,
            "memory_pressure_observed": False,
        },
        memory_estimate=estimate,
    )

    event = sent[0]
    assert event["benchmark_version"] == 9
    assert event["measurement_profile"] == "contribute-v1"
    assert event["measurement_quality"] == "clean"
    assert event["context_length"] == 1024
    assert event["num_batch"] == 128
    assert "host_cpu_load_percent" not in event


def _v9_report(monkeypatch, sent, **overrides):
    """Send one v9 contribution, defaulting to the clean case above."""
    monkeypatch.setattr(cli, "scan_hardware", _hardware_with_chip_metadata)
    monkeypatch.setattr(
        cli.telemetry,
        "send_event",
        lambda event, force=False: sent.append(event) or True,
    )
    estimate = contribute_memory.ContributionMemoryEstimate(
        mapped_weights_ram_gb=0.75,
        committed_ram_gb=0.3,
        required_vram_gb=0.0,
        kv_cache_gb=0.1,
        compute_buffer_gb=0.1,
        runtime_overhead_gb=0.1,
        source="gguf_header",
        confidence="medium",
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        runtime_buffer_ram_gb=0.3,
    )
    kwargs = {
        "size_bytes": int(0.75 * 1024**3),
        "sample_count": 3,
        "speed_min": 41.0,
        "speed_max": 44.0,
        "speed_samples": [41.0, 42.5, 44.0],
        "model_metadata": {"parameter_size": "1B", "quantization_level": "Q4_K_M"},
        "runtime": {
            "runtime_profile": "explicit_ollama_options",
            "context_length": 1024,
            "gpu_offload_percent": 0,
            "cpu_threads": 8,
            "num_batch": 128,
        },
        "engine_version": "0.32.1",
        "memory_measurement": {
            "ram_available_before_gb": 2.2,
            "ram_available_min_gb": 2.0,
            "ram_available_after_gb": 2.1,
            "memory_pressure_observed": False,
        },
        "memory_estimate": estimate,
    }
    kwargs.update(overrides)
    cli._report_telemetry("model-1B-Q4.gguf", "org/model", 42.5, **kwargs)
    return sent[-1]


def test_report_telemetry_v9_labels_a_busy_host_loaded(isolated_omm_home, monkeypatch):
    event = _v9_report(monkeypatch, [], host_cpu_load_percent=61.0)

    assert event["measurement_quality"] == "loaded"
    assert event["host_cpu_load_percent"] == 61.0


def test_report_telemetry_v9_records_a_quiet_host_and_stays_clean(
    isolated_omm_home, monkeypatch
):
    """A reading below the threshold is still worth carrying: it turns a
    `clean` row from "nobody looked" into "the host was measured quiet"."""
    event = _v9_report(monkeypatch, [], host_cpu_load_percent=3.24)

    assert event["measurement_quality"] == "clean"
    assert event["host_cpu_load_percent"] == 3.2


def test_report_telemetry_v9_labels_the_rounded_host_reading_it_sends(
    isolated_omm_home, monkeypatch
):
    """The label has to describe the number that leaves the machine. A
    reading just under the threshold rounds up to it, and a row calling that
    `clean` is one the server rejects outright."""
    event = _v9_report(monkeypatch, [], host_cpu_load_percent=24.96)

    assert event["host_cpu_load_percent"] == 25.0
    assert event["measurement_quality"] == "loaded"


@pytest.mark.parametrize("unreadable", [None, float("nan"), 140.0, -1.0, True])
def test_report_telemetry_v9_omits_an_unusable_host_reading(
    isolated_omm_home, monkeypatch, unreadable
):
    """An unavailable or out-of-range sample is unknown, not idle. Sending a
    zero would claim an idle host omm never observed, and the rules reject
    anything outside 0-100 anyway."""
    event = _v9_report(monkeypatch, [], host_cpu_load_percent=unreadable)

    assert "host_cpu_load_percent" not in event
    assert event["measurement_quality"] == "clean"


def test_report_telemetry_v9_keeps_dispersion_ahead_of_host_load(
    isolated_omm_home, monkeypatch
):
    event = _v9_report(
        monkeypatch,
        [],
        speed_samples=[10.0, 42.5, 80.0],
        host_cpu_load_percent=61.0,
    )

    assert event["measurement_quality"] == "unstable"
    assert event["host_cpu_load_percent"] == 61.0


def test_report_telemetry_v9_keeps_memory_pressure_ahead_of_host_load(
    isolated_omm_home, monkeypatch
):
    event = _v9_report(
        monkeypatch,
        [],
        memory_measurement={
            "ram_available_before_gb": 2.2,
            "ram_available_min_gb": 0.2,
            "ram_available_after_gb": 2.1,
            "memory_pressure_observed": True,
        },
        host_cpu_load_percent=61.0,
    )

    assert event["measurement_quality"] == "pressured"


def test_report_telemetry_v9_labels_the_issue_32_loaded_run(
    isolated_omm_home, monkeypatch
):
    """Issue #32's own loaded run: three samples that agree with each other
    while sitting a third below the same machine's idle result. Every signal
    v9 had before reads healthy, so it uploaded as `clean`."""
    samples = [3.18, 3.41, 3.56]
    assert contribute_memory.speed_mad_ratio(samples) <= 0.15

    loaded = _v9_report(
        monkeypatch, [], speed_samples=samples, host_cpu_load_percent=58.0
    )
    idle = _v9_report(
        monkeypatch, [], speed_samples=[5.08, 5.09, 5.15], host_cpu_load_percent=4.0
    )

    assert loaded["measurement_quality"] == "loaded"
    assert idle["measurement_quality"] == "clean"


_LMSTUDIO_RUNTIME_KEYS = (
    "runtime_profile",
    "context_length",
    "gpu_offload_percent",
    "cpu_threads",
    "num_batch",
)


def _lmstudio_report(monkeypatch, sent, **overrides):
    monkeypatch.setattr(cli, "scan_hardware", _hardware_with_chip_metadata)
    monkeypatch.setattr(
        cli.telemetry,
        "send_event",
        lambda event, force=False: sent.append(event) or True,
    )
    kwargs = {
        "size_bytes": 4 * 1024**3,
        "sample_count": 3,
        "speed_min": 100.0,
        "speed_max": 106.0,
        "model_metadata": {"parameter_size": "0.5B", "quantization_level": "Q4_K_M"},
        "runtime": None,
        "engine_version": "0.4.21",
        "engine": "lmstudio",
    }
    kwargs.update(overrides)
    cli._report_telemetry("qwen2.5-0.5b-instruct", None, 103.2, **kwargs)


def test_lmstudio_benchmark_reports_the_full_v8_metadata_it_can_observe(
    isolated_omm_home, monkeypatch
):
    sent = []
    _lmstudio_report(monkeypatch, sent)

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["engine"] == "lmstudio"
    assert event["outcome"] == "success"
    assert event["engine_version"] == "0.4.21"
    assert event["parameter_count_b"] == 0.5
    assert event["quant_bits"] == 4.0
    assert event["cpu_score"] == 5600.0
    assert event["cpu_arch"]


def test_lmstudio_benchmark_omits_the_runtime_block_it_cannot_measure(
    isolated_omm_home, monkeypatch
):
    sent = []
    _lmstudio_report(monkeypatch, sent)

    assert [key for key in _LMSTUDIO_RUNTIME_KEYS if key in sent[0]] == []


def test_lmstudio_benchmark_ignores_an_ollama_shaped_runtime_snapshot(
    isolated_omm_home, monkeypatch
):
    """A caller handing LM Studio an Ollama runtime dict must not turn
    those numbers into a claim about how LM Studio actually ran."""
    sent = []
    _lmstudio_report(
        monkeypatch,
        sent,
        runtime={
            "runtime_profile": "explicit_ollama_options",
            "context_length": 4096,
            "gpu_offload_percent": 100,
            "cpu_threads": 8,
            "num_batch": 512,
        },
    )

    assert [key for key in _LMSTUDIO_RUNTIME_KEYS if key in sent[0]] == []


def test_lmstudio_benchmark_stays_on_the_v4_shape_without_an_engine_version(
    isolated_omm_home, monkeypatch
):
    sent = []
    _lmstudio_report(monkeypatch, sent, engine_version=None)

    assert sent[0]["benchmark_version"] == 4


def test_lmstudio_benchmark_never_claims_the_contribute_v1_profile(
    isolated_omm_home, monkeypatch
):
    """contribute-v1 asserts a fixed context/batch configuration LM Studio
    never applies, so a memory-measured LM Studio run stops at v8."""
    estimate = contribute_memory.ContributionMemoryEstimate(
        mapped_weights_ram_gb=0.75,
        committed_ram_gb=0.3,
        required_vram_gb=0.0,
        kv_cache_gb=0.1,
        compute_buffer_gb=0.1,
        runtime_overhead_gb=0.1,
        source="gguf_header",
        confidence="medium",
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        runtime_buffer_ram_gb=0.3,
    )
    sent = []
    _lmstudio_report(
        monkeypatch,
        sent,
        speed_samples=[100.0, 103.2, 106.0],
        memory_measurement={
            "ram_available_before_gb": 2.2,
            "ram_available_min_gb": 2.0,
            "ram_available_after_gb": 2.1,
            "memory_pressure_observed": False,
        },
        memory_estimate=estimate,
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert "measurement_profile" not in event
    assert "memory_estimate_source" not in event


def test_ollama_benchmark_still_falls_back_when_the_runtime_is_unmeasured(
    isolated_omm_home, monkeypatch
):
    """The LM Studio relaxation must not loosen Ollama: without a
    /api/ps-confirmed runtime snapshot, Ollama still drops to v4."""
    sent = []
    _lmstudio_report(monkeypatch, sent, engine="ollama", engine_version="0.32.1")

    assert sent[0]["benchmark_version"] == 4


def test_report_failure_telemetry_v8_sends_chip_scores_not_raw_names(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware_with_chip_metadata)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    cli._report_failure_telemetry(
        {"tag": "too-big:latest", "outcome": "model_unfit", "failure_reason": "out_of_memory"},
        {},
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["cpu_score"] == 5600.0
    assert event["gpu_score"] == 4090.0
    assert "cpu_model" not in event


def test_benchmark_says_when_it_falls_back_to_lmstudio(isolated_omm_home, monkeypatch):
    """Ollama and LM Studio produce numbers that read identically, so a run
    that silently switched engines left the user unable to tell which one
    measured their model."""
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: "/some/lms")
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["benchmark", "some-model-key"])

    assert "using LM Studio instead" in result.stdout


def test_engine_selection_notice_stays_silent_for_ollama(monkeypatch):
    """Ollama is the documented default, so naming it on every run would be
    noise rather than information."""
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))

    cli._print_engine_selection_notice("ollama")

    assert printed == []


def test_transient_benchmark_failures_come_with_a_next_step():
    """A bare `(model_load_failed)` told a first-time tester nothing (promo
    dry run, 2026-08-23); every transient reason a user can hit from a
    healthy install must map to one actionable line."""
    from omm import quality

    for reason in (
        quality.FAILURE_REASON_MODEL_LOAD_FAILED,
        quality.FAILURE_REASON_OLLAMA_UNAVAILABLE,
        quality.FAILURE_REASON_CONNECTION_ERROR,
        quality.FAILURE_REASON_GENERATION_TIMEOUT,
        quality.FAILURE_REASON_NO_TIMING_METRICS,
    ):
        assert reason in cli._TRANSIENT_FAILURE_HINTS, reason
    assert "omm doctor" in cli._TRANSIENT_FAILURE_HINTS[quality.FAILURE_REASON_MODEL_LOAD_FAILED]
    assert "omm link --engine ollama" in cli._TRANSIENT_FAILURE_HINTS[quality.FAILURE_REASON_MODEL_LOAD_FAILED]
def _capture_benchmark_models(monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    seen = {}

    def fake_collect_evidence(models, *a, **k):
        seen["models"] = models
        raise cli.quality_mod.QualityEvaluationError("stop here, we only care about the arg")

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)
    return seen


def test_benchmark_resolves_registry_filename_to_ollama_tag(isolated_omm_home, monkeypatch):
    """Promo dry run, 2026-08-23: `omm benchmark qwen2.5-…q4_k_m.gguf` (the
    name `omm list`/`omm info` print) was sent to Ollama verbatim, which has
    no model called '*.gguf', and came back as model_load_failed."""
    registry.save_registry({"small.gguf": {"ollama_name": "small:latest", "linked": {}}})
    seen = _capture_benchmark_models(monkeypatch)

    runner.invoke(cli.app, ["benchmark", "small.gguf"])
    assert seen["models"] == ["small:latest"]

    runner.invoke(cli.app, ["benchmark", "small"])  # `.gguf` may be omitted, like `omm info`
    assert seen["models"] == ["small:latest"]


def test_benchmark_still_accepts_a_literal_ollama_tag(isolated_omm_home, monkeypatch):
    registry.save_registry({"small.gguf": {"ollama_name": "small:latest", "linked": {}}})
    seen = _capture_benchmark_models(monkeypatch)

    runner.invoke(cli.app, ["benchmark", "other:7b"])
    assert seen["models"] == ["other:7b"]


def test_benchmark_registry_filename_without_ollama_tag_says_to_link(isolated_omm_home, monkeypatch):
    registry.save_registry({"small.gguf": {"linked": {}}})
    _capture_benchmark_models(monkeypatch)

    result = runner.invoke(cli.app, ["benchmark", "small.gguf"])

    assert result.exit_code == 1
    assert "omm link" in result.output
