import json

from typer.testing import CliRunner

from omm import cli, linker
from omm.hub import ResolvedModel

runner = CliRunner()


def _stub_successful_install(monkeypatch, ollama_installed=True):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )

    def fake_download(url, dest):
        dest.write_bytes(b"fake-gguf")

    monkeypatch.setattr(cli, "download_file", fake_download)
    monkeypatch.setattr(cli, "sha256_file", lambda dest: "deadbeef")
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: ollama_installed)
    monkeypatch.setattr(linker, "is_jan_installed", lambda: False)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: False)
    monkeypatch.setattr(linker, "is_mstystudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_textgenwebui_installed", lambda: False)
    monkeypatch.setattr(linker, "is_koboldcpp_installed", lambda: False)
    monkeypatch.setattr(linker, "link_ollama", lambda dest, tag, models_dir=None: ollama_installed)
    monkeypatch.setattr(linker, "sanitize_ollama_tag", lambda filename: "tinyllama")
    return filename


def _log_outcomes(isolated_omm_home):
    log_path = isolated_omm_home / "telemetry.log"
    if not log_path.exists():
        return []
    return [json.loads(line)["outcome"] for line in log_path.read_text().splitlines()]


def test_declining_upload_confirm_logs_declined_by_user(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: False)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert _log_outcomes(isolated_omm_home) == ["declined_by_user"]


def test_no_ollama_link_logs_not_attempted(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, ollama_installed=False)
    ask_calls = []
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: ask_calls.append(message) or False)

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert ask_calls == []
    assert _log_outcomes(isolated_omm_home) == ["not_attempted_no_ollama_link"]


def test_report_telemetry_notice_and_log_when_daemon_unreachable(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: None)

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert "wasn't reachable" in result.stdout
    assert _log_outcomes(isolated_omm_home) == ["skipped_daemon_unreachable"]


def test_report_telemetry_prints_retry_notice_when_send_fails(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: False)

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert "will retry" in result.stdout.lower()


def test_report_telemetry_silent_on_success(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert "will retry" not in result.stdout.lower()


def test_root_prints_notice_when_pending_telemetry_flushed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.telemetry, "flush_pending", lambda: 2)

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "sent 2 queued telemetry event" in result.stdout.lower()


def test_root_no_notice_when_nothing_pending(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.telemetry, "flush_pending", lambda: 0)

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "queued telemetry" not in result.stdout.lower()


def test_bare_omm_does_not_flush_pending_telemetry(isolated_omm_home, monkeypatch):
    def _boom():
        raise AssertionError("flush_pending should not run for bare `omm`")

    monkeypatch.setattr(cli.telemetry, "flush_pending", _boom)

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
