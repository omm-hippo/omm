import questionary
from typer.testing import CliRunner

from omm import cli, linker
from omm.hub import ResolvedModel

runner = CliRunner()


def _stub_successful_install(monkeypatch, isolated_omm_home):
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
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(linker, "is_jan_installed", lambda: False)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: False)
    monkeypatch.setattr(linker, "is_mstystudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_textgenwebui_installed", lambda: False)
    monkeypatch.setattr(linker, "is_koboldcpp_installed", lambda: False)
    monkeypatch.setattr(linker, "link_ollama", lambda dest, tag, models_dir=None: True)
    monkeypatch.setattr(linker, "sanitize_ollama_tag", lambda filename: "tinyllama")
    return filename


def test_install_runs_benchmark_and_telemetry_on_yes(isolated_omm_home, monkeypatch):
    filename = _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert "42.0" in result.stdout or "42" in result.stdout
    assert len(sent) == 1
    assert sent[0][1] is True


def test_install_runs_benchmark_but_skips_upload_on_no(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: False)
    bench_calls = []
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: bench_calls.append(tag) or 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert bench_calls == ["tinyllama"] * 3
    assert sent == []


def test_install_no_upload_flag_skips_prompt_without_a_tty(isolated_omm_home, monkeypatch):
    """--no-upload must let a script install without ever hitting the
    upload confirm prompt, which errors out immediately without a tty
    (see P0 fix) under the default 'ask' policy."""
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4", "--no-upload"])

    assert result.exit_code == 0, result.stdout
    assert sent == []


def test_install_upload_flag_sends_telemetry_without_a_tty(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4", "--upload"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
    assert sent[0][1] is True


def test_ask_confirm_uses_questionary_with_auto_enter(monkeypatch):
    captured = {}

    class FakeQuestion:
        def ask(self):
            return True

    def fake_confirm(message, default=False, auto_enter=True):
        captured["message"] = message
        captured["default"] = default
        captured["auto_enter"] = auto_enter
        return FakeQuestion()

    monkeypatch.setattr(questionary, "confirm", fake_confirm)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    result = cli._ask_confirm("질문?", default=False)

    assert result is True
    assert captured == {"message": "질문?", "default": False, "auto_enter": True}


def test_ask_confirm_errors_instead_of_hanging_without_a_tty(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    try:
        cli._ask_confirm("질문?", default=False)
        assert False, "expected typer.Exit"
    except cli.typer.Exit as exc:
        assert exc.exit_code == 1
