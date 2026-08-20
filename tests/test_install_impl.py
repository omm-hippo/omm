import json
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest

from omm import cli, registry
from omm.downloader import DownloadCancelled
from omm.engines import LoadReceipt, ProbeResult, RuntimeHealth, RuntimeModel, UnloadResult
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
    monkeypatch.setattr(cli, "remote_file_sha256", lambda *args: None)


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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    outcome = cli._install_impl(_resolved(), auto_upload=True)

    assert outcome.tokens_per_sec == 55.0
    assert outcome.telemetry_sent is True


def test_install_starts_and_stops_daemon_when_not_reachable(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    started_proc = object()
    start_calls = []
    stop_calls = []
    monkeypatch.setattr(
        cli.benchmark, "start_ollama_daemon", lambda: start_calls.append(1) or started_proc
    )
    monkeypatch.setattr(cli.benchmark, "stop_ollama_daemon", lambda proc: stop_calls.append(proc))

    outcome = cli._install_impl(_resolved(), auto_upload=True)

    assert outcome.tokens_per_sec == 55.0
    assert start_calls == [1]
    assert stop_calls == [started_proc]


def test_install_skips_daemon_start_when_already_reachable(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.benchmark,
        "start_ollama_daemon",
        lambda: (_ for _ in ()).throw(AssertionError("should not start a daemon")),
    )
    monkeypatch.setattr(
        cli.benchmark,
        "stop_ollama_daemon",
        lambda proc: (_ for _ in ()).throw(AssertionError("should not stop a daemon")),
    )

    outcome = cli._install_impl(_resolved(), auto_upload=True)

    assert outcome.tokens_per_sec == 55.0


def test_memory_guard_block_prevents_ollama_benchmark(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli.tuning,
        "recommend_runtime_settings",
        lambda *args, **kwargs: SimpleNamespace(ollama_options={}, required_memory_gb=1.0),
    )
    monkeypatch.setattr(
        cli,
        "_guard_ollama_load",
        lambda tag, required_gb: (False, object(), False),
    )
    monkeypatch.setattr(
        cli.benchmark,
        "benchmark_ollama_samples",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not benchmark")),
    )

    outcome = cli._install_impl(_resolved(), enforce_memory_guard=True)

    assert outcome.tokens_per_sec is None
    assert outcome.failure_reason == "memory_guard_blocked"


class _FakeLmStudioAdapter:
    key = "lmstudio"

    def __init__(self, *, loaded=False):
        self.loaded = loaded

    def health(self):
        return RuntimeHealth(True, "0.4.1")

    def list_models(self):
        return [RuntimeModel("org/repo", "org/repo", self.loaded, "org/repo" if self.loaded else None)]

    def load(self, model, options):
        runtime_model = RuntimeModel("org/repo", "org/repo", True, "org/repo")
        return LoadReceipt(runtime_model, "org/repo", self.loaded, not self.loaded)

    def generate(self, receipt, request):
        return ProbeResult("OK")

    def unload(self, receipt):
        return UnloadResult(True)


def test_memory_guard_blocks_lmstudio_load_verification(isolated_omm_home, monkeypatch):
    """When LM Studio is the selected runtime for post-install verification,
    `_verify_lmstudio_after_install` must consult the memory guard before
    letting the adapter load the model - mirrors
    `test_memory_guard_block_prevents_ollama_benchmark` for the Ollama path."""
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    monkeypatch.setattr(cli, "_ensure_install_disk_capacity", lambda *args, **kwargs: None)
    _stub_common(monkeypatch, ollama=False, lmstudio=True)
    monkeypatch.setattr(
        cli.linker, "link_engine", lambda key, dest, *, repo_id, ollama_tag: None
    )
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _FakeLmStudioAdapter())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        cli,
        "_guard_lmstudio_load",
        lambda model_key, required_gb: (False, object(), False),
    )
    monkeypatch.setattr(
        cli,
        "verify_and_record",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    outcome = cli._install_impl(
        _resolved(), verify_runtime_after_install=True, enforce_memory_guard=True
    )

    assert outcome.compatibility_engine == "lmstudio"
    assert outcome.compatibility_status == "failed"
    assert outcome.failure_reason == "memory_guard_blocked"


def test_lmstudio_load_verification_proceeds_when_guard_allows(isolated_omm_home, monkeypatch):
    """Sanity check for the same wiring: when the guard allows the load,
    the adapter-based probe still runs normally."""
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    monkeypatch.setattr(cli, "_ensure_install_disk_capacity", lambda *args, **kwargs: None)
    _stub_common(monkeypatch, ollama=False, lmstudio=True)
    monkeypatch.setattr(
        cli.linker, "link_engine", lambda key, dest, *, repo_id, ollama_tag: None
    )
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _FakeLmStudioAdapter())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    guard_calls = []
    monkeypatch.setattr(
        cli,
        "_guard_lmstudio_load",
        lambda model_key, required_gb: (guard_calls.append(model_key) or True, object(), False),
    )

    outcome = cli._install_impl(
        _resolved(), verify_runtime_after_install=True, enforce_memory_guard=True
    )

    assert guard_calls == ["org/repo"]
    assert outcome.compatibility_engine == "lmstudio"
    assert outcome.compatibility_status == "passed"


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


def test_install_skips_old_lmstudio_probe_when_new_adapter_probe_already_ran(monkeypatch):
    """Regression test for the install-time double-load bug: when LM Studio
    was the runtime `_verify_lmstudio_after_install` already probed via the
    new HTTP adapter (`compatibility_engine == "lmstudio"`), the older
    `lms`-CLI-based `linker.verify_lmstudio_load` probe must not run too -
    that would load/unload the model into LM Studio a second time."""
    from omm import linker

    outcome = cli.InstallOutcome(
        filename="model.gguf",
        repo_id="acme/widget",
        linked={"lmstudio": True},
        compatibility_engine="lmstudio",
    )
    called = {"count": 0}
    monkeypatch.setattr(
        linker,
        "verify_lmstudio_load",
        lambda *a, **k: called.__setitem__("count", called["count"] + 1),
    )

    cli._report_lmstudio_load_verification(outcome)

    assert called["count"] == 0


def test_install_still_runs_old_lmstudio_probe_when_lmstudio_not_the_verified_runtime(
    monkeypatch, capsys
):
    """The old probe still has unique value when LM Studio was linked but a
    different runtime (or none) was selected for adapter-based verification
    - e.g. Ollama was verified instead. That's the one case the task
    description calls out as still needing the old check."""
    from omm import linker

    outcome = cli.InstallOutcome(
        filename="model.gguf",
        repo_id="acme/widget",
        linked={"lmstudio": True, "ollama": True},
        compatibility_engine="ollama",
    )
    monkeypatch.setattr(linker, "verify_lmstudio_load", lambda gguf_path, repo_id: False)

    cli._report_lmstudio_load_verification(outcome)

    captured = capsys.readouterr()
    assert "did not load successfully" in captured.out


def test_install_impl_telemetry_includes_model_provider(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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

    def fake_download(url, dest, stop_check=None, **_kw):
        assert stop_check is not None
        raise DownloadCancelled("interrupted")

    monkeypatch.setattr(cli, "download_file", fake_download)
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(cli.InstallInterrupted) as exc_info:
        cli._install_impl(_resolved(), stop_event=stop_event)

    assert exc_info.value.filename == "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"


def test_stop_event_set_during_benchmark_raises_contribution_stopped(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, stop_check=None, **_kw: dest.write_bytes(b"x")
    )
    _stub_common(monkeypatch)

    def slow_benchmark(tag):
        time.sleep(2)
        return 10.0

    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", slow_benchmark)
    stop_event = threading.Event()
    threading.Timer(0.05, stop_event.set).start()

    with pytest.raises(cli.InstallInterrupted):
        cli._install_impl(_resolved(), stop_event=stop_event)


def test_plain_install_path_unaffected_by_stop_event_none(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    calls = []

    def fake_download(url, dest, **_kw):
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


def test_install_rejects_provider_download_when_sha256_does_not_match(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"tampered")
    )
    _stub_common(monkeypatch, ollama=False)
    monkeypatch.setattr(cli, "remote_file_sha256", lambda *args: "0" * 64)

    with pytest.raises(cli.DownloadError, match="does not match"):
        cli._install_impl(_resolved(provider="huggingface"))

    assert not (cli.MODELS_DIR / _resolved().filename).exists()


def test_install_accepts_provider_download_when_sha256_matches(
    isolated_omm_home, monkeypatch
):
    payload = b"provider-bytes"
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(payload)
    )
    _stub_common(monkeypatch, ollama=False)
    monkeypatch.setattr(cli, "sha256_file", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(
        cli, "remote_file_sha256", lambda *args: hashlib.sha256(payload).hexdigest()
    )

    outcome = cli._install_impl(_resolved(provider="huggingface"))

    assert outcome.sha256 == hashlib.sha256(payload).hexdigest()


def test_install_refuses_unverified_existing_file_from_different_source(
    isolated_omm_home, monkeypatch
):
    filename = _resolved().filename
    (cli.MODELS_DIR / filename).write_bytes(b"unowned")
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    _stub_common(monkeypatch, ollama=False)

    with pytest.raises(cli.DownloadError, match="cannot be verified"):
        cli._install_impl(_resolved())


def test_install_rejects_casefold_collision_with_registered_model(
    isolated_omm_home, monkeypatch
):
    existing_name = "Model.gguf"
    existing = cli.MODELS_DIR / existing_name
    existing.write_bytes(b"existing")
    registry.save_registry(
        {
            existing_name: {
                "source": "https://example.com/Model.gguf",
                "sha256": hashlib.sha256(existing.read_bytes()).hexdigest(),
                "linked": {},
            }
        }
    )
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    resolved = ResolvedModel(
        url="https://example.com/model.gguf",
        filename="model.gguf",
        repo_id=None,
        provider=None,
    )

    with pytest.raises(cli.DownloadError, match="collides with registered path"):
        cli._install_impl(resolved)

    assert existing.read_bytes() == b"existing"


def test_install_rejects_unknown_provider_without_key_error(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)

    with pytest.raises(cli.DownloadError, match="unsupported model provider"):
        cli._install_impl(_resolved(provider="unknown"))


def test_benchmark_always_runs_but_upload_needs_confirm(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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


class _StoppedThenRunningAdapter:
    """health() is unreachable until the daemon is started, mimicking the
    real adapter's view of `omm install`'s daemon-autostart path."""

    key = "ollama"

    def __init__(self):
        self.checks = 0

    def health(self):
        self.checks += 1
        return RuntimeHealth(self.checks > 1, "1.0")

    def list_models(self):
        return [RuntimeModel("tinyllama", "tinyllama", False)]


def test_install_offers_to_start_ollama_when_stopped_then_benchmarks_and_uploads(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _StoppedThenRunningAdapter())
    monkeypatch.setattr(cli.benchmark, "ollama_install_state", lambda: "stopped")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    started_proc = object()
    start_calls, stop_calls = [], []
    monkeypatch.setattr(
        cli.benchmark, "start_ollama_daemon", lambda: start_calls.append(1) or started_proc
    )
    monkeypatch.setattr(cli.benchmark, "stop_ollama_daemon", lambda proc: stop_calls.append(proc))
    confirms = []
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda prompt, *a, **k: confirms.append(prompt) or True
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    uploaded = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: uploaded.append(event) or True
    )
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")

    outcome = cli._install_impl(
        _resolved(), verify_runtime_after_install=True, runtime_load_consent=None,
    )

    assert start_calls == [1]
    assert stop_calls == [started_proc]
    assert outcome.tokens_per_sec == 55.0
    assert outcome.telemetry_sent is True
    assert uploaded != []
    assert any("Start it now" in p for p in confirms)
    assert any("Load" in p and "memory" in p for p in confirms)


def test_install_skips_ollama_autostart_prompt_when_declined(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _StoppedThenRunningAdapter())
    monkeypatch.setattr(cli.benchmark, "ollama_install_state", lambda: "stopped")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        cli.benchmark,
        "start_ollama_daemon",
        lambda: (_ for _ in ()).throw(AssertionError("should not start")),
    )
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.benchmark,
        "benchmark_ollama",
        lambda tag: (_ for _ in ()).throw(AssertionError("should not benchmark")),
    )

    outcome = cli._install_impl(
        _resolved(), verify_runtime_after_install=True, runtime_load_consent=None, no_upload=True,
    )

    assert outcome.tokens_per_sec is None
    assert outcome.compatibility_status == "failed"


class _InstallCompatibilityAdapter:
    key = "ollama"

    def __init__(self, *, loaded=False):
        self.loaded = loaded

    def health(self):
        return RuntimeHealth(True, "1.0")

    def list_models(self):
        return [RuntimeModel("tinyllama", "tinyllama", self.loaded)]


def test_post_install_declined_load_runs_no_benchmark_and_records_nothing(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x")
    )
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_compatibility_adapter", lambda engine: _InstallCompatibilityAdapter()
    )
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(
        cli.benchmark, "benchmark_ollama", lambda tag: calls.append(tag) or 42.0
    )

    outcome = cli._install_impl(
        _resolved(),
        verify_runtime_after_install=True,
        runtime_load_consent=None,
        no_upload=True,
    )

    assert calls == []
    assert outcome.runtime_load_declined is True
    assert "compatibility" not in cli.registry.load_registry()[outcome.filename]


def test_post_install_preserves_preloaded_model_and_records_compatibility(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x")
    )
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_compatibility_adapter",
        lambda engine: _InstallCompatibilityAdapter(loaded=True),
    )
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    unloads = []
    monkeypatch.setattr(
        cli.quality_mod, "unload_model", lambda tag: unloads.append(tag) or True
    )

    outcome = cli._install_impl(
        _resolved(),
        verify_runtime_after_install=True,
        runtime_load_consent=None,
        no_upload=True,
    )

    assert outcome.compatibility_status == "passed"
    assert unloads == []
    saved = cli.registry.load_registry()[outcome.filename]["compatibility"]["ollama"]
    assert saved["status"] == "passed"


def test_memory_guard_preloaded_observation_prevents_cleanup_race(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x")
    )
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_compatibility_adapter",
        lambda engine: _InstallCompatibilityAdapter(loaded=False),
    )
    monkeypatch.setattr(
        cli,
        "_guard_ollama_load",
        lambda tag, required_gb: (True, object(), True),
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    unloads = []
    monkeypatch.setattr(
        cli.quality_mod, "unload_model", lambda tag: unloads.append(tag) or True
    )

    outcome = cli._install_impl(
        _resolved(),
        verify_runtime_after_install=True,
        runtime_load_consent=True,
        enforce_memory_guard=True,
        no_upload=True,
    )

    assert outcome.tokens_per_sec == 42.0
    assert unloads == []


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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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


def test_auto_calibrate_is_skipped_when_the_host_cpu_was_already_busy(
    isolated_omm_home, monkeypatch, capsys
):
    """A load-depressed number is exactly the transient error a per-machine
    correction factor must not absorb, and a plain install has no dispersion
    or memory-pressure record to catch it with."""
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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)
    monkeypatch.setattr(cli, "sample_cpu_utilization_percent", lambda: 55.0)
    monkeypatch.setattr(
        cli.calibration,
        "record_calibration",
        lambda hardware, **kwargs: (_ for _ in ()).throw(
            AssertionError("calibration must not absorb a load-depressed benchmark")
        ),
    )

    cli._install_impl(_resolved())

    output = " ".join(capsys.readouterr().out.split())
    assert "Local calibration not updated" in output
    assert "other programs were using the CPU" in output


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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None, **_kw: dest.write_bytes(b"x"))
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


def _partial_offload_profile():
    return cli.tuning.RuntimeProfile(
        context_length=4096,
        gpu_offload_percent=50,
        cpu_threads=4,
        num_batch=512,
        profile_name="test",
        model_size_gb=1.0,
        required_memory_gb=1.5,
        available_memory_gb=4.0,
        headroom_gb=1.0,
        quant_bits=4.0,
    )


def test_use_quality_eval_gpu_crash_retries_same_candidate_on_cpu(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli.tuning, "recommend_runtime_settings", lambda hw, candidate: _partial_offload_profile())
    fake_result = {
        "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
        "speed": {
            "median_tokens_per_sec": 3.2,
            "samples_tokens_per_sec": [3.0, 3.2, 3.4],
            "runs": 3,
        },
    }
    calls = []

    def fake_evaluate(tag, pack, speed_runs=3, runtime_options=None):
        calls.append(dict(runtime_options or {}))
        if len(calls) == 1:
            raise cli.quality_mod.QualityEvaluationError(
                "Ollama /api/generate request failed",
                failure_reason="unsupported_runtime",
                gpu_crash=True,
            )
        return fake_result

    monkeypatch.setattr(cli.quality_mod, "evaluate_model", fake_evaluate)
    monkeypatch.setattr(cli.quality_mod, "ensure_model_unloaded", lambda tag: True)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    gpu_state = {"force_cpu": False}
    outcome = cli._install_impl(
        _resolved(),
        auto_upload=True,
        use_quality_eval=True,
        quality_pack={"pack_id": "pack-1", "pack_version": "1.1.0", "items": []},
        stop_event=threading.Event(),
        gpu_state=gpu_state,
    )

    assert len(calls) == 2
    assert calls[0].get("num_gpu") != 0
    assert calls[1]["num_gpu"] == 0
    assert outcome.tokens_per_sec == 3.2
    assert gpu_state["force_cpu"] is True


def test_gpu_state_force_cpu_skips_straight_to_cpu_for_next_candidate(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None, **_kw: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli.tuning, "recommend_runtime_settings", lambda hw, candidate: _partial_offload_profile())
    fake_result = {
        "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
        "speed": {
            "median_tokens_per_sec": 2.1,
            "samples_tokens_per_sec": [2.0, 2.1, 2.2],
            "runs": 3,
        },
    }
    calls = []

    def fake_evaluate(tag, pack, speed_runs=3, runtime_options=None):
        calls.append(dict(runtime_options or {}))
        return fake_result

    monkeypatch.setattr(cli.quality_mod, "evaluate_model", fake_evaluate)
    monkeypatch.setattr(cli.quality_mod, "ensure_model_unloaded", lambda tag: True)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    gpu_state = {"force_cpu": True}
    outcome = cli._install_impl(
        _resolved(),
        auto_upload=True,
        use_quality_eval=True,
        quality_pack={"pack_id": "pack-1", "pack_version": "1.1.0", "items": []},
        stop_event=threading.Event(),
        gpu_state=gpu_state,
    )

    assert len(calls) == 1
    assert calls[0]["num_gpu"] == 0
    assert outcome.tokens_per_sec == 2.1


def test_use_quality_eval_failure_reports_no_result(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None, **_kw: dest.write_bytes(b"x"))
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
        lambda dest, only_engine=None: [
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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"model"))
    monkeypatch.setattr(
        cli.linker,
        "disk_copy_risks",
        lambda dest, only_engine=None: [
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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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


def test_force_redownloads_even_when_already_present(isolated_omm_home, monkeypatch):
    """Without --force, an existing file at dest short-circuits the fetch
    (see the 'already downloaded, skipping fetch' branch); force=True must
    bypass that and re-run the size check + download."""
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)

    resolved = _resolved()
    dest = cli.MODELS_DIR / resolved.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"stale-bytes")

    size_calls = []
    monkeypatch.setattr(
        cli,
        "remote_file_size",
        lambda provider, repo_id, filename: size_calls.append(filename) or None,
    )
    download_calls = []

    def fake_download(url, dst, **_kw):
        download_calls.append(dst)
        dst.write_bytes(b"fresh-bytes")

    monkeypatch.setattr(cli, "download_file", fake_download)

    outcome = cli._install_impl(resolved, force=True)

    assert size_calls == [resolved.filename]
    assert download_calls == [dest]
    assert dest.read_bytes() == b"fresh-bytes"
    assert outcome.filename == resolved.filename


def test_force_clears_stale_dest_and_part_before_redownloading(isolated_omm_home, monkeypatch):
    """--force must guarantee a genuinely fresh download: a leftover
    completed `dest` and a stale `.part` sidecar from an earlier, unrelated
    partial download must both be gone before `download_file` is invoked.
    This is what would have caught the Windows `Path.rename` FileExistsError
    (rename onto an existing dest) and the "resumes a stale partial instead
    of starting fresh" bug, without needing to run on Windows."""
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)

    resolved = _resolved()
    dest = cli.MODELS_DIR / resolved.filename
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"stale-complete-bytes")
    part.write_bytes(b"stale-partial-bytes")

    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)

    dest_existed_at_download_time = []

    def fake_download(url, dst, **_kw):
        dest_existed_at_download_time.append(dst.exists())
        assert not part.exists(), "stale .part must be cleared before a fresh --force download"
        dst.write_bytes(b"fresh-bytes")

    monkeypatch.setattr(cli, "download_file", fake_download)

    cli._install_impl(resolved, force=True)

    assert dest_existed_at_download_time == [False]
    assert not part.exists()
    assert dest.read_bytes() == b"fresh-bytes"


def test_without_force_skips_fetch_when_already_present(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)

    resolved = _resolved()
    dest = cli.MODELS_DIR / resolved.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"stale-bytes")
    registry.save_registry(
        {
            resolved.filename: {
                "source": resolved.url,
                "sha256": "deadbeef",
                "linked": {},
            }
        }
    )

    monkeypatch.setattr(
        cli, "remote_file_size", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no size check"))
    )
    monkeypatch.setattr(
        cli, "download_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download"))
    )

    outcome = cli._install_impl(resolved)

    assert dest.read_bytes() == b"stale-bytes"
    assert outcome.filename == resolved.filename


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
    monkeypatch.setattr(cli, "download_file", lambda url, dest, **_kw: dest.write_bytes(b"x"))
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
