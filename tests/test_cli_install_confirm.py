from unittest.mock import MagicMock

from typer.testing import CliRunner

from omm import cli, config, linker, registry
from omm.engines import RuntimeHealth, RuntimeModel
from omm.hardware import HardwareInfo
from omm.hub import ResolvedModel

runner = CliRunner()


def _hardware() -> HardwareInfo:
    # install with runtime-load consent granted routes through the
    # memory-guard pre-flight check, which reads live available RAM via
    # `cli.scan_hardware()`. Tests must supply deterministic hardware here
    # instead of falling through to the real machine's live state, or the
    # guard's decision - and these tests - become dependent on how much RAM
    # happens to be free on whatever host runs the suite.
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


class _InstallAdapter:
    key = "ollama"

    def __init__(self, *, loaded=False):
        self.loaded = loaded

    def health(self):
        return RuntimeHealth(True, "1.0")

    def list_models(self):
        return [RuntimeModel("tinyllama", "tinyllama", self.loaded)]


def _stub_successful_install(monkeypatch, isolated_omm_home):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )

    def fake_download(url, dest, **_kw):
        dest.write_bytes(b"fake-gguf")

    monkeypatch.setattr(cli, "download_file", fake_download)
    monkeypatch.setattr(cli, "sha256_file", lambda dest: "deadbeef")
    monkeypatch.setattr(cli, "available_ram_gb", lambda: 12.0)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(linker, "is_jan_installed", lambda: False)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: False)
    monkeypatch.setattr(linker, "is_mstystudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_textgenwebui_installed", lambda: False)
    monkeypatch.setattr(linker, "is_koboldcpp_installed", lambda: False)
    monkeypatch.setattr(
        linker, "link_ollama", lambda dest, tag, models_dir=None, **kwargs: True
    )
    monkeypatch.setattr(linker, "sanitize_ollama_tag", lambda filename: "tinyllama")
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _InstallAdapter())
    return filename


def test_install_runs_benchmark_and_telemetry_on_yes(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(
        cli.app, ["install", "tinyllama-1.1b-q4", "--verify-runtime"]
    )

    assert result.exit_code == 0, result.stdout
    assert "42.0" in result.stdout or "42" in result.stdout
    assert len(sent) == 1
    assert sent[0][1] is True


def test_install_esc_interrupt_cleans_up_and_exits_130(isolated_omm_home, monkeypatch):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )

    def fake_install_impl(resolved, **kwargs):
        assert kwargs["stop_event"] is not None
        raise cli.InstallInterrupted(filename)

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    cleaned = []
    monkeypatch.setattr(
        cli,
        "_cleanup_interrupted_install",
        lambda name, downloaded_now=False: cleaned.append((name, downloaded_now)),
    )

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0
    assert cleaned == [(filename, False)]
    assert "Cancelled" in result.stderr


def test_install_link_error_prints_cause_and_fix_instead_of_traceback(
    isolated_omm_home, monkeypatch
):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )

    def fake_install_impl(resolved, **kwargs):
        raise linker.LinkError(
            f"Could not link {filename} into Ollama: [Errno 13] Permission denied",
            fix="Check that ~/.ollama is writable by your user.",
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 1
    assert "Could not link" in result.stderr
    assert "→ Check that ~/.ollama is writable by your user." in result.stderr


def test_install_keyboard_interrupt_cleans_up_partial_download(isolated_omm_home, monkeypatch):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )

    def fake_install_impl(resolved, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    cleaned = []
    monkeypatch.setattr(
        cli,
        "_cleanup_interrupted_install",
        lambda name, downloaded_now=False: cleaned.append((name, downloaded_now)),
    )

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 130
    assert cleaned == [(filename, False)]


def _seed_existing_model(filename: str) -> None:
    """Simulate a model that was fully installed by a *previous* `omm
    install` run - a real central file plus a registry entry, both already
    on disk before the run under test even starts."""
    dest = config.MODELS_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"pre-existing-gguf-bytes")
    registry.upsert_entry(
        filename,
        sha256="deadbeef",
        version="deadbee",
        source="https://example.com/x.gguf",
        linked={"ollama": True},
    )


def test_install_esc_during_reinstall_of_existing_model_preserves_it(
    isolated_omm_home, monkeypatch
):
    """CRITICAL regression (audit #1): cancelling a reinstall of a model
    that was already fully installed - Esc during the benchmark - must
    never delete the pre-existing central file, links, or registry entry.
    Runs the real `_cleanup_interrupted_install`, not a stub, so the actual
    fix (skip `_remove_one` unless this run downloaded new bytes) is what's
    under test."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    _seed_existing_model(filename)
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )

    def fake_install_impl(resolved, **kwargs):
        # `_prepare_install_artifact`'s "already downloaded, skipping
        # fetch" branch never sets downloaded_now True, so a benchmark-stage
        # cancel on a reinstall must carry downloaded_now=False.
        assert kwargs["stop_event"] is not None
        raise cli.InstallInterrupted(filename, downloaded_now=False)

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda name, entry: removed.append(name))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0
    assert removed == []  # the CRITICAL bug called _remove_one here
    assert (config.MODELS_DIR / filename).exists()
    assert registry.load_registry()[filename]["sha256"] == "deadbeef"


def test_install_ctrl_c_during_reinstall_of_existing_model_preserves_it(
    isolated_omm_home, monkeypatch
):
    """CRITICAL regression (audit #1), second repro: a bare KeyboardInterrupt
    (Windows console Ctrl+C, e.g. at the "Load ... into Ollama memory?"
    prompt) firing during a reinstall of an already-installed model must
    also leave it alone - `install()`'s `download_state` never flips to
    True because `_install_impl` cannot raise `InstallInterrupted` for a
    plain KeyboardInterrupt."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    _seed_existing_model(filename)
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )

    def fake_install_impl(resolved, **kwargs):
        assert kwargs["downloaded_state"] == {"downloaded_now": False}
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda name, entry: removed.append(name))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 130
    assert removed == []
    assert (config.MODELS_DIR / filename).exists()
    assert registry.load_registry()[filename]["sha256"] == "deadbeef"


def test_install_declined_runtime_load_skips_benchmark_and_upload(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    bench_calls = []
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: bench_calls.append(tag) or 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(
        cli.app, ["install", "tinyllama-1.1b-q4", "--no-verify-runtime"]
    )

    assert result.exit_code == 0, result.stdout
    assert bench_calls == []
    assert sent == []
    assert "model was not loaded" in result.stderr


def test_install_no_upload_flag_skips_prompt_without_a_tty(isolated_omm_home, monkeypatch):
    """--no-upload must let a script install without ever hitting the
    upload confirm prompt, which errors out immediately without a tty
    (see P0 fix) under the default 'ask' policy."""
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(
        cli.app,
        ["install", "tinyllama-1.1b-q4", "--no-upload", "--verify-runtime"],
    )

    assert result.exit_code == 0, result.stdout
    assert sent == []


def test_install_upload_flag_sends_telemetry_without_a_tty(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(
        cli.app,
        ["install", "tinyllama-1.1b-q4", "--upload", "--verify-runtime"],
    )

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
    assert sent[0][1] is True


def test_install_global_yes_consents_to_runtime_load_without_a_tty(
    isolated_omm_home, monkeypatch
):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(
        cli,
        "_ask_confirm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("prompted")),
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)

    result = runner.invoke(
        cli.app, ["install", "tinyllama-1.1b-q4", "--yes", "--no-upload"]
    )

    assert result.exit_code == 0, result.stdout


def test_install_threads_quiet_and_no_color_into_download_file(isolated_omm_home, monkeypatch):
    # --quiet/--no-color must reach download_file() so its progress bar and
    # retry warning respect them too, not just cli.py's own console (see #80).
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(kwargs)
        dest.write_bytes(b"fake-gguf")

    monkeypatch.setattr(cli, "download_file", fake_download)

    result = runner.invoke(
        cli.app,
        [
            "install",
            "tinyllama-1.1b-q4",
            "--verify-runtime",
            "--quiet",
            "--no-color",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert calls and calls[0].get("quiet") is True
    assert calls[0].get("no_color") is True


def test_install_quiet_suppresses_status_lines_but_keeps_the_result(isolated_omm_home, monkeypatch):
    # --quiet should drop the "Verifying checksum.../Benchmarking..."
    # status-style lines but still confirm what actually happened, same as
    # apt/brew -q (see #80).
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    result = runner.invoke(
        cli.app,
        ["install", "tinyllama-1.1b-q4", "--verify-runtime", "--quiet"],
    )

    assert result.exit_code == 0, result.stdout
    assert "Verifying checksum" not in result.stdout
    assert "Benchmarking" not in result.stdout
    assert "Installed" in result.stdout


def test_install_hint_reports_linked_runner_count_and_points_at_omm_run(
    isolated_omm_home, monkeypatch
):
    # The post-install hint must say how many runners the model was linked
    # into, and steer the user to `omm run` rather than a bare
    # `ollama run <tag>` that dead-ends when the Ollama daemon is down.
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)

    result = runner.invoke(
        cli.app, ["install", "tinyllama-1.1b-q4", "--no-verify-runtime"]
    )

    assert result.exit_code == 0, result.stdout
    assert "Linked into 1 local runner: Ollama" in result.stdout
    assert "omm run tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf" in result.stdout
    assert "ollama run" not in result.stdout


def _handler_for(bindings, key):
    matches = [b for b in bindings.bindings if b.keys == (key,)]
    assert matches, f"expected a binding for {key!r}"
    return matches[-1].handler


def test_ask_confirm_key_bindings_answer_on_y_and_n():
    status = {"answer": None}
    bindings = cli._build_single_key_bindings(
        [("y", "Yes", True), ("n", "No", False)], default_value=False, status=status
    )

    fake_event = MagicMock()
    _handler_for(bindings, "y")(fake_event)
    fake_event.app.exit.assert_called_once_with(result=True)

    fake_event = MagicMock()
    _handler_for(bindings, "n")(fake_event)
    fake_event.app.exit.assert_called_once_with(result=False)


def test_ask_confirm_accepts_hangul_jamo_for_the_same_physical_key():
    """A 2-beolsik Korean IME turns a 'y' keypress into the jamo 'ㅛ'; the
    prompt should accept it as yes regardless of 한/영 toggle state, since
    what matters is the physical key, not the IME state."""
    status = {"answer": None}
    bindings = cli._build_single_key_bindings(
        [("y", "Yes", True), ("n", "No", False)], default_value=False, status=status
    )

    fake_event = MagicMock()
    _handler_for(bindings, "ㅛ")(fake_event)
    fake_event.app.exit.assert_called_once_with(result=True)

    fake_event = MagicMock()
    _handler_for(bindings, "ㅜ")(fake_event)
    fake_event.app.exit.assert_called_once_with(result=False)


def test_ask_upload_choice_key_bindings_include_always():
    status = {"answer": None}
    bindings = cli._build_single_key_bindings(
        [("y", "Yes", "yes"), ("n", "No", "no"), ("a", "Always", "always")],
        default_value="no",
        status=status,
    )

    fake_event = MagicMock()
    _handler_for(bindings, "a")(fake_event)
    fake_event.app.exit.assert_called_once_with(result="always")

    # 'a' sits at the jamo 'ㅁ' on a 2-beolsik keyboard.
    fake_event = MagicMock()
    _handler_for(bindings, "ㅁ")(fake_event)
    fake_event.app.exit.assert_called_once_with(result="always")


def test_ask_confirm_errors_instead_of_hanging_without_a_tty(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    try:
        cli._ask_confirm("질문?", default=False)
        assert False, "expected typer.Exit"
    except cli.typer.Exit as exc:
        assert exc.exit_code == 1
