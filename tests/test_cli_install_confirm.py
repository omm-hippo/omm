from unittest.mock import MagicMock

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
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
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
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
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
