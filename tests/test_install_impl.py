import json
import threading
import time
from types import SimpleNamespace

import pytest

from omm import cli
from omm.downloader import DownloadCancelled
from omm.hub import ResolvedModel


def _resolved(filename="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf", provider=None):
    return ResolvedModel(
        url="https://example.com/x.gguf", filename=filename, repo_id="org/repo", provider=provider
    )


def _stub_common(monkeypatch, ollama=True, lmstudio=False):
    monkeypatch.setattr(cli, "sha256_file", lambda dest: "deadbeef")
    monkeypatch.setattr(cli.linker, "is_lmstudio_installed", lambda: lmstudio)
    monkeypatch.setattr(cli.linker, "is_ollama_installed", lambda: ollama)
    monkeypatch.setattr(cli.linker, "is_jan_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_anythingllm_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_mstystudio_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_textgenwebui_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_koboldcpp_installed", lambda: False)
    monkeypatch.setattr(
        cli.linker, "link_ollama", lambda dest, tag, models_dir=None, **kwargs: ollama
    )
    monkeypatch.setattr(cli.linker, "sanitize_ollama_tag", lambda filename: "tinyllama")
    monkeypatch.setattr(
        cli.quality_mod,
        "_model_metadata",
        lambda tag: (_ for _ in ()).throw(cli.quality_mod.QualityEvaluationError("not available")),
    )
    monkeypatch.setattr(cli.quality_mod, "unload_model", lambda tag: True)
    monkeypatch.setattr(cli.quality_mod, "runtime_snapshot", lambda *a, **k: None)


def test_skip_unfit_returns_outcome_without_prompting_or_downloading(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "predict_speed", lambda trees, hw, candidate: 0.0)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda *a, **k: download_calls.append(a))

    outcome = cli._install_impl(_resolved(), skip_unfit=True)

    assert outcome.skipped_unfit is True
    assert outcome.linked == {}
    assert download_calls == []


def test_auto_upload_skips_confirm_prompt_and_sends_telemetry(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    outcome = cli._install_impl(_resolved(), auto_upload=True)

    assert outcome.tokens_per_sec == 55.0
    assert outcome.telemetry_sent is True


# --- LM Studio load-verification reporting ------------------------------


def test_install_prints_warning_when_lmstudio_load_verification_fails(monkeypatch, capsys):
    from omm import linker

    outcome = cli.InstallOutcome(
        filename="model.gguf",
        repo_id="acme/widget",
        linked={"lmstudio": True},
    )
    monkeypatch.setattr(linker, "verify_lmstudio_load", lambda gguf_path, repo_id: False)

    cli._report_lmstudio_load_verification(outcome)

    captured = capsys.readouterr()
    assert "did not load successfully" in captured.out


def test_install_silent_when_lmstudio_load_verification_inconclusive(monkeypatch, capsys):
    from omm import linker

    outcome = cli.InstallOutcome(
        filename="model.gguf",
        repo_id="acme/widget",
        linked={"lmstudio": True},
    )
    monkeypatch.setattr(linker, "verify_lmstudio_load", lambda gguf_path, repo_id: None)

    cli._report_lmstudio_load_verification(outcome)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_install_skips_lmstudio_load_verification_when_not_linked(monkeypatch, capsys):
    from omm import linker

    outcome = cli.InstallOutcome(filename="model.gguf", repo_id=None, linked={"lmstudio": False})
    called = {"count": 0}
    monkeypatch.setattr(
        linker, "verify_lmstudio_load", lambda *a, **k: called.__setitem__("count", called["count"] + 1)
    )

    cli._report_lmstudio_load_verification(outcome)

    assert called["count"] == 0


def test_install_impl_telemetry_includes_model_provider(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    captured = {}
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: captured.update(event) or True
    )

    cli._install_impl(_resolved(provider="modelscope"), auto_upload=True)

    assert captured["model_provider"] == "modelscope"


def test_install_impl_telemetry_defaults_provider_to_huggingface(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    captured = {}
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: captured.update(event) or True
    )

    cli._install_impl(_resolved(), auto_upload=True)

    assert captured["model_provider"] == "huggingface"


def test_no_upload_skips_confirm_prompt_and_does_not_send_telemetry(isolated_omm_home, monkeypatch):
    from omm import config as config_mod

    config_mod.update_config(telemetry_send_policy="ask")
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    outcome = cli._install_impl(_resolved(), no_upload=True)

    assert outcome.tokens_per_sec == 55.0
    assert outcome.telemetry_sent is False
    assert sent == []


def test_stop_event_set_before_download_raises_contribution_stopped(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)

    def fake_download(url, dest, stop_check=None):
        assert stop_check is not None
        raise DownloadCancelled("interrupted")

    monkeypatch.setattr(cli, "download_file", fake_download)
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(cli.ContributionStopped) as exc_info:
        cli._install_impl(_resolved(), stop_event=stop_event)

    assert exc_info.value.filename == "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"


def test_stop_event_set_during_benchmark_raises_contribution_stopped(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, stop_check=None: dest.write_bytes(b"x")
    )
    _stub_common(monkeypatch)

    def slow_benchmark(tag):
        time.sleep(2)
        return 10.0

    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", slow_benchmark)
    stop_event = threading.Event()
    threading.Timer(0.05, stop_event.set).start()

    with pytest.raises(cli.ContributionStopped):
        cli._install_impl(_resolved(), stop_event=stop_event)


def test_plain_install_path_unaffected_by_stop_event_none(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    calls = []

    def fake_download(url, dest):
        calls.append("no-kwargs")
        dest.write_bytes(b"x")

    monkeypatch.setattr(cli, "download_file", fake_download)
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 10.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    outcome = cli._install_impl(_resolved())

    assert calls == ["no-kwargs"]
    assert outcome.tokens_per_sec == 10.0


def test_benchmark_always_runs_but_upload_needs_confirm(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    bench_calls = []
    monkeypatch.setattr(
        cli.benchmark, "benchmark_ollama", lambda tag: bench_calls.append(tag) or 42.0
    )
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force))
    )

    outcome = cli._install_impl(_resolved())

    assert bench_calls == ["tinyllama"] * 3
    assert outcome.tokens_per_sec == 42.0
    assert sent == []
    assert outcome.telemetry_sent is False


def test_auto_calibrate_runs_silently_when_cached_model_available(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli.predictor, "load_cached_model", lambda: {"trees": [{"leaf": True, "value": 20.0}]}
    )
    hw_stub = SimpleNamespace(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=16.0,
        ram_available_gb=12.0,
        vram_total_gb=None,
        vram_free_gb=None,
        unified_memory=False,
        gpu_name=None,
        gpu_tflops=None,
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: hw_stub)
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed_interval",
        lambda *args, **kwargs: (20.0, 20.0, 20.0),
    )
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)
    recorded = {}
    monkeypatch.setattr(
        cli.calibration,
        "record_calibration",
        lambda hardware, **kwargs: recorded.update(kwargs) or 1.5,
    )

    cli._install_impl(_resolved())

    assert recorded["measured_tokens_per_sec"] == 30.0
    assert recorded["predicted_tokens_per_sec"] == 20.0


def test_resolve_upload_decision_always_skips_prompt(isolated_omm_home):
    cli.config_mod.update_config(telemetry_send_policy="always")

    assert cli._resolve_upload_decision("prompt") is True


def test_resolve_upload_decision_never_skips_prompt(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(
        cli, "_ask_upload_choice", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )

    assert cli._resolve_upload_decision("prompt") is False


def test_resolve_upload_decision_ask_falls_back_to_confirm(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="ask")
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda message: "yes" if message == "prompt" else "no")

    assert cli._resolve_upload_decision("prompt") is True
    assert cli._resolve_upload_decision("other") is False


def test_resolve_upload_decision_always_choice_persists_policy_and_uploads(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="ask")
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "always")

    assert cli._resolve_upload_decision("prompt") is True
    assert cli.load_config()["telemetry_send_policy"] == "always"


def test_install_auto_uploads_without_confirm_when_policy_always(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="always")
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    outcome = cli._install_impl(_resolved())

    assert outcome.telemetry_sent is True
    assert sent


def test_install_never_uploads_without_confirm_when_policy_never(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send"))
    )

    outcome = cli._install_impl(_resolved())

    assert outcome.telemetry_sent is False


def test_report_telemetry_includes_quality_fields_when_provided(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli, "scan_hardware",
        lambda: SimpleNamespace(ram_total_gb=16.0, vram_total_gb=None, unified_memory=False, gpu_tflops=None),
    )
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli._report_telemetry(
        "small:latest",
        "org/small",
        42.5,
        size_bytes=123,
        sample_count=3,
        speed_min=40.0,
        speed_max=45.0,
        quality={"pack_id": "pack-1", "pack_version": "1.1.0", "correct": 6, "total": 8, "accuracy": 0.75},
    )

    event = sent[0]
    assert event["model_size_bytes"] == 123
    assert event["sample_count"] == 3
    assert event["tokens_per_sec_min"] == 40.0
    assert event["tokens_per_sec_max"] == 45.0
    assert event["quality_pack_id"] == "pack-1"
    assert event["quality_correct"] == 6
    assert event["quality_total"] == 8
    assert event["quality_accuracy"] == 0.75


def test_report_telemetry_omits_quality_fields_by_default(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli, "scan_hardware",
        lambda: SimpleNamespace(ram_total_gb=16.0, vram_total_gb=None, unified_memory=False, gpu_tflops=None),
    )
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli._report_telemetry("model.gguf", "org/repo", 10.0)

    assert "quality_pack_id" not in sent[0]
    assert sent[0]["sample_count"] == 1


def test_report_telemetry_emits_v7_success_with_cpu_fields(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "scan_hardware",
        lambda: SimpleNamespace(
            ram_total_gb=16.0,
            vram_total_gb=8.0,
            unified_memory=False,
            gpu_tflops=20.0,
            cpu="private CPU name",
            cpu_arch="x86_64",
            cpu_physical_cores=4,
            cpu_logical_cores=8,
            gpu_name="private GPU name",
        ),
    )
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli._report_telemetry(
        "model-7B-A3B-Q4.gguf",
        "org/model",
        42.5,
        size_bytes=4 * 1024**3,
        sample_count=3,
        speed_min=40.0,
        speed_max=45.0,
        model_metadata={"parameter_size": "7B", "quantization_level": "Q4_K_M"},
        runtime={
            "runtime_profile": "explicit_ollama_options",
            "context_length": 4096,
            "gpu_offload_percent": 75,
            "cpu_threads": 8,
            "num_batch": 512,
        },
        engine_version="0.12.0",
        model_filename="model-7B-A3B-Q4.gguf",
        model_digest="sha256:" + "a" * 64,
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["outcome"] == "success"
    assert "failure_reason" not in event
    assert event["parameter_count_b"] == 7
    assert event["active_parameter_count_b"] == 3
    assert event["quant_bits"] == 4
    assert event["context_length"] == 4096
    assert event["gpu_offload_percent"] == 75
    assert event["model_digest"] == "a" * 64
    assert "runtime" not in event
    assert event["cpu_score"] == 0.0
    assert event["cpu_tier"] == 0.0
    assert event["cpu_arch"] == "x86_64"
    assert event["cpu_physical_cores"] == 4
    assert event["cpu_logical_cores"] == 8
    assert event["gpu_score"] == 0.0
    assert event["gpu_tier"] == 0.0
    assert "cpu_model" not in event
    assert "private CPU name" not in json.dumps(event)
    assert "private GPU name" not in json.dumps(event)


def test_report_telemetry_skips_moe_when_active_count_is_unknown(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "scan_hardware",
        lambda: SimpleNamespace(
            ram_total_gb=16.0,
            vram_total_gb=8.0,
            unified_memory=False,
            gpu_tflops=20.0,
        ),
    )
    sent = []
    attempts = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )
    monkeypatch.setattr(
        cli.telemetry,
        "log_attempt",
        lambda action, detail=None: attempts.append((action, detail)),
    )

    result = cli._report_telemetry(
        "custom-moe:20b",
        None,
        10.0,
        model_metadata={
            "is_moe": True,
            "parameter_size": "20B",
            "quantization_level": "Q4_K_M",
        },
    )

    assert result is False
    assert sent == []
    assert attempts == [
        ("skipped_moe_active_parameters_unknown", "custom-moe:20b")
    ]


def test_report_telemetry_falls_back_to_v4_when_runtime_is_unverified(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "scan_hardware",
        lambda: SimpleNamespace(
            ram_total_gb=16.0,
            vram_total_gb=None,
            unified_memory=False,
            gpu_tflops=None,
        ),
    )
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli._report_telemetry(
        "model-7B-Q4.gguf",
        "org/model",
        10.0,
        sample_count=3,
        speed_min=9.0,
        speed_max=11.0,
        model_metadata={"parameter_size": "7B", "quantization_level": "Q4_K_M"},
        runtime=None,
        engine_version="0.12.0",
    )

    assert sent[0]["benchmark_version"] == 4
    assert "parameter_count_b" not in sent[0]


def test_use_quality_eval_reports_median_speed_and_quality_summary(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    fake_result = {
        "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
        "speed": {
            "median_tokens_per_sec": 42.5,
            "samples_tokens_per_sec": [41.0, 42.5, 44.0],
            "runs": 3,
        },
    }
    monkeypatch.setattr(cli.quality_mod, "evaluate_model", lambda tag, pack, speed_runs=3: fake_result)
    unloaded = []
    monkeypatch.setattr(
        cli.quality_mod, "ensure_model_unloaded", lambda tag: unloaded.append(tag) or True
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    outcome = cli._install_impl(
        _resolved(),
        auto_upload=True,
        use_quality_eval=True,
        quality_pack={"pack_id": "pack-1", "pack_version": "1.1.0", "items": []},
        stop_event=threading.Event(),
    )

    assert outcome.tokens_per_sec == 42.5
    assert unloaded == ["tinyllama"]
    event = sent[0]
    assert event["sample_count"] == 3
    assert event["tokens_per_sec_min"] == 41.0
    assert event["tokens_per_sec_max"] == 44.0
    assert event["quality_correct"] == 6
    assert event["quality_total"] == 8


def test_use_quality_eval_failure_reports_no_result(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)

    def raise_eval(tag, pack, speed_runs=3):
        raise cli.quality_mod.QualityEvaluationError("ollama returned nothing")

    monkeypatch.setattr(cli.quality_mod, "evaluate_model", raise_eval)
    monkeypatch.setattr(cli.quality_mod, "ensure_model_unloaded", lambda tag: True)
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send"))
    )

    outcome = cli._install_impl(
        _resolved(),
        auto_upload=True,
        use_quality_eval=True,
        quality_pack={"pack_id": "pack-1", "pack_version": "1.1.0", "items": []},
        stop_event=threading.Event(),
    )

    assert outcome.tokens_per_sec is None
    assert outcome.telemetry_sent is False


def test_install_impl_exits_when_not_enough_disk_space_and_not_skip_unfit(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: 50 * 1024**3)
    monkeypatch.setattr(
        cli.shutil, "disk_usage", lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024**3)
    )
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda *a, **k: download_calls.append(a))

    with pytest.raises(cli.typer.Exit) as exc_info:
        cli._install_impl(_resolved())

    assert exc_info.value.exit_code == 1
    assert download_calls == []


def test_install_impl_skips_gracefully_when_not_enough_disk_space_and_skip_unfit(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: 50 * 1024**3)
    monkeypatch.setattr(
        cli.shutil, "disk_usage", lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024**3)
    )
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda *a, **k: download_calls.append(a))

    outcome = cli._install_impl(_resolved(), skip_unfit=True)

    assert outcome.skipped_low_disk is True
    assert download_calls == []


def test_install_impl_budgets_download_and_ollama_native_copy_together(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda *args: 4 * 1024**3)
    monkeypatch.setattr(
        cli.linker,
        "disk_copy_risks",
        lambda dest, only_ollama=False: [
            cli.linker.DiskCopyRisk(dest.parent, "Ollama", "native full-model import")
        ],
    )
    monkeypatch.setattr(cli.linker, "storage_volume_key", lambda path: ("test", "same"))
    monkeypatch.setattr(
        cli.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=8 * 1024**3),
    )
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda *args, **kwargs: download_calls.append(args))

    outcome = cli._install_impl(_resolved(), skip_unfit=True)

    assert outcome.skipped_low_disk is True
    assert download_calls == []


def test_install_impl_removes_new_download_when_post_download_copy_budget_fails(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda *args: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"model"))
    monkeypatch.setattr(
        cli.linker,
        "disk_copy_risks",
        lambda dest, only_ollama=False: [
            cli.linker.DiskCopyRisk(dest.parent, "Ollama", "native full-model import")
        ],
    )
    monkeypatch.setattr(cli.linker, "storage_volume_key", lambda path: ("test", "same"))
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))

    outcome = cli._install_impl(_resolved(), skip_unfit=True)

    assert outcome.skipped_low_disk is True
    assert not (cli.MODELS_DIR / _resolved().filename).exists()


def test_install_impl_proceeds_when_disk_space_check_is_inconclusive(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 10.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    outcome = cli._install_impl(_resolved())

    assert outcome.tokens_per_sec == 10.0


def test_install_impl_cleans_up_partial_file_and_skips_on_insufficient_disk_space_mid_download(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)

    def fake_download(url, dest, **kwargs):
        dest.with_suffix(dest.suffix + ".part").write_bytes(b"partial")
        raise cli.InsufficientDiskSpaceError("disk full mid-download")

    monkeypatch.setattr(cli, "download_file", fake_download)

    outcome = cli._install_impl(_resolved(), skip_unfit=True)

    assert outcome.skipped_low_disk is True
    dest = cli.MODELS_DIR / "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_install_impl_exits_cleanly_on_insufficient_disk_space_mid_download_without_skip_unfit(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)
    monkeypatch.setattr(
        cli,
        "download_file",
        lambda *a, **k: (_ for _ in ()).throw(cli.InsufficientDiskSpaceError("disk full")),
    )

    with pytest.raises(cli.typer.Exit) as exc_info:
        cli._install_impl(_resolved())

    assert exc_info.value.exit_code == 1


def test_auto_calibrate_does_not_crash_install_when_write_fails(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli.predictor, "load_cached_model", lambda: {"trees": [{"leaf": True, "value": 20.0}]}
    )
    hw_stub = SimpleNamespace(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=16.0,
        ram_available_gb=12.0,
        vram_total_gb=None,
        vram_free_gb=None,
        unified_memory=False,
        gpu_name=None,
        gpu_tflops=None,
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: hw_stub)
    monkeypatch.setattr(
        cli.predictor, "predict_speed_interval", lambda *args, **kwargs: (20.0, 20.0, 20.0)
    )
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)
    monkeypatch.setattr(
        cli.calibration,
        "record_calibration",
        lambda hardware, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    outcome = cli._install_impl(_resolved())  # must not raise

    assert outcome.tokens_per_sec == 30.0


def test_link_model_does_not_print_skip_notice_for_uninstalled_engines(
    isolated_omm_home, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    monkeypatch.setattr(
        cli.linker,
        "link_engine",
        lambda key, dest, *, repo_id, ollama_tag: None,
    )
    dest = tmp_path / "model.gguf"
    dest.write_bytes(b"x")

    linked = cli._link_model(dest, "org/repo", "model-tag")

    captured = capsys.readouterr()
    assert "not detected, skipping link" not in captured.out
    assert linked == {
        "ollama": True,
        "lmstudio": False,
        "jan": False,
        "anythingllm": False,
        "mstystudio": False,
        "textgenwebui": False,
        "koboldcpp": False,
    }
