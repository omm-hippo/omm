from pathlib import Path

from typer.testing import CliRunner

from omm import cli, launcher, linker, registry

runner = CliRunner()


def _entry(**overrides):
    entry = {
        "sha256": "abc1234567890",
        "size_bytes": 1024**3,
        "installed_at": "2026-08-22T00:00:00+00:00",
        "repo_id": "org/repo",
        "ollama_name": "repo-q4",
        "linked": {"ollama": True, "lmstudio": True},
    }
    entry.update(overrides)
    return entry


def _installed(*keys):
    return lambda key: key in keys


def _cached(fn):
    """Stand-in for an lru_cache'd linker finder - conftest clears the
    cache on teardown, so the replacement needs a no-op cache_clear."""
    fn.cache_clear = lambda: None
    return fn


def test_run_uses_ollama_when_linked_and_installed(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("ollama", "lmstudio"))
    monkeypatch.setattr(cli, "_ensure_ollama_running", lambda action, assume_yes=False: None)
    calls = []

    def fake_launch(engine, *, model_filename, model_path, ollama_tag):
        calls.append((engine, model_filename, ollama_tag))
        return launcher.LaunchResult(True, "Chat ended.", interactive=True)

    monkeypatch.setattr(cli.launcher, "launch", fake_launch)

    result = runner.invoke(cli.app, ["run", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert calls == [("ollama", "model.gguf", "repo-q4")]
    assert "Ollama" in result.stdout
    assert "Chat ended." in result.stdout


def test_run_reports_missing_ollama_with_fix_command(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("ollama"))
    monkeypatch.setattr(cli.benchmark, "ollama_install_state", lambda: "missing")

    result = runner.invoke(cli.app, ["run", "model.gguf"])

    assert result.exit_code == 1
    assert "Ollama is not installed or its executable cannot be found." in result.stderr
    stderr_flat = " ".join(result.stderr.split())
    assert (
        "→ Install Ollama from https://ollama.com/download, start it once, "
        "then retry `omm run`." in stderr_flat
    )


def test_run_stops_daemon_it_started(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("ollama"))
    handle = object()
    monkeypatch.setattr(cli, "_ensure_ollama_running", lambda action, assume_yes=False: handle)
    stopped = []
    monkeypatch.setattr(cli, "_stop_engine_daemon", lambda engine, h: stopped.append((engine, h)))
    monkeypatch.setattr(
        cli.launcher, "launch", lambda *a, **k: launcher.LaunchResult(True, "Chat ended.", True)
    )

    result = runner.invoke(cli.app, ["run", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert stopped == [("ollama", handle)]


def test_run_falls_back_to_next_linked_engine(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry(linked={"ollama": False, "lmstudio": True})})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("ollama", "lmstudio"))
    seen = []
    monkeypatch.setattr(
        cli.launcher,
        "launch",
        lambda engine, **k: (seen.append(engine), launcher.LaunchResult(True, "Opened LM Studio."))[1],
    )

    result = runner.invoke(cli.app, ["run", "model"])

    assert result.exit_code == 0, result.stdout
    assert seen == ["lmstudio"]
    assert "Opened LM Studio." in result.stdout


def test_run_honours_engine_flag(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("ollama", "lmstudio"))
    seen = []
    monkeypatch.setattr(
        cli.launcher,
        "launch",
        lambda engine, **k: (seen.append(engine), launcher.LaunchResult(True, "ok"))[1],
    )

    result = runner.invoke(cli.app, ["run", "model.gguf", "--engine", "lmstudio"])

    assert result.exit_code == 0, result.stdout
    assert seen == ["lmstudio"]


def test_run_rejects_engine_flag_when_model_not_linked_there(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry(linked={"ollama": True})})

    result = runner.invoke(cli.app, ["run", "model.gguf", "--engine", "jan"])

    assert result.exit_code == 1
    assert "omm link --engine jan" in result.output


def test_run_rejects_unknown_engine(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["run", "model.gguf", "--engine", "nope"])

    assert result.exit_code == 2
    assert "Unknown engine" in result.output


def test_run_errors_when_nothing_is_linked(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry(linked={})})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("ollama"))

    result = runner.invoke(cli.app, ["run", "model.gguf"])

    assert result.exit_code == 1
    assert "not linked into any installed runner" in result.output


def test_run_with_no_models_points_to_recommend(isolated_omm_home):
    registry.save_registry({})

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 1
    assert "omm recommend" in result.output


def test_run_with_single_model_needs_no_name(isolated_omm_home, monkeypatch):
    registry.save_registry({"only.gguf": _entry()})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("ollama"))
    monkeypatch.setattr(cli, "_ensure_ollama_running", lambda action, assume_yes=False: None)
    seen = []
    monkeypatch.setattr(
        cli.launcher,
        "launch",
        lambda engine, **k: (seen.append(k["model_filename"]), launcher.LaunchResult(True, "ok"))[1],
    )

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 0, result.stdout
    assert seen == ["only.gguf"]


def test_run_with_several_models_and_no_tty_asks_for_a_name(isolated_omm_home, monkeypatch):
    registry.save_registry({"a.gguf": _entry(), "b.gguf": _entry()})
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 1
    assert "omm run <model>" in result.output


def test_run_unknown_model(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["run", "ghost"])

    assert result.exit_code == 1
    assert "not installed via omm" in result.output


def test_run_reports_launch_failure(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry(linked={"koboldcpp": True})})
    monkeypatch.setattr(cli.linker, "is_engine_installed", _installed("koboldcpp"))
    monkeypatch.setattr(
        cli.launcher, "launch", lambda *a, **k: launcher.LaunchResult(False, "KoboldCpp binary not found.")
    )

    result = runner.invoke(cli.app, ["run", "model.gguf"])

    assert result.exit_code == 1
    assert "KoboldCpp binary not found." in result.output


def test_run_is_listed_in_help_all(isolated_omm_home):
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0
    assert "omm run" in result.stdout


# --- launcher module ---------------------------------------------------------


def test_launcher_dispatch_covers_every_engine(monkeypatch):
    seen = []
    for spec in linker.ENGINES:
        monkeypatch.setattr(
            launcher,
            f"launch_{spec.key}",
            (lambda key: lambda *a, **k: (seen.append(key), launcher.LaunchResult(True, key))[1])(spec.key),
        )
    for spec in linker.ENGINES:
        result = launcher.launch(
            spec.key, model_filename="m.gguf", model_path=Path("m.gguf"), ollama_tag="m"
        )
        assert result.ok and result.message == spec.key
    assert seen == [spec.key for spec in linker.ENGINES]
    assert set(launcher.ENGINE_PRIORITY) == {spec.key for spec in linker.ENGINES}


def test_launch_ollama_runs_interactive_chat(monkeypatch, tmp_path):
    exe = tmp_path / "ollama"
    monkeypatch.setattr(linker, "find_ollama_executable", lambda: exe)
    calls = []
    monkeypatch.setattr(launcher.subprocess, "call", lambda args, **k: (calls.append(args), 0)[1])

    result = launcher.launch_ollama("repo-q4")

    assert result.ok and result.interactive
    assert calls == [[str(exe), "run", "repo-q4"]]


def test_launch_ollama_reports_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(linker, "find_ollama_executable", lambda: tmp_path / "ollama")
    monkeypatch.setattr(launcher.subprocess, "call", lambda args, **k: 1)

    result = launcher.launch_ollama("repo-q4")

    assert not result.ok
    assert "omm link --engine ollama" in result.message


def test_launch_koboldcpp_passes_model_and_launch_flag(monkeypatch, tmp_path):
    binary = tmp_path / "koboldcpp.exe"
    binary.write_bytes(b"")
    model = tmp_path / "m.gguf"
    model.write_bytes(b"")
    monkeypatch.setattr(linker, "find_koboldcpp_binary", _cached(lambda: binary))
    spawned = []
    monkeypatch.setattr(launcher, "_spawn_detached", lambda args, cwd=None: spawned.append((args, cwd)))

    result = launcher.launch_koboldcpp(model)

    assert result.ok
    assert spawned == [([str(binary), "--model", str(model), "--launch"], tmp_path)]
    assert launcher.KOBOLDCPP_DEFAULT_URL in result.message


def test_launch_koboldcpp_missing_model(monkeypatch, tmp_path):
    monkeypatch.setattr(linker, "find_koboldcpp_binary", _cached(lambda: tmp_path / "koboldcpp"))

    result = launcher.launch_koboldcpp(tmp_path / "missing.gguf")

    assert not result.ok


def test_launch_textgenwebui_uses_platform_start_script(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    (tmp_path / "start_linux.sh").write_text("", encoding="utf-8")
    monkeypatch.setattr(linker, "find_textgenwebui_root", _cached(lambda: tmp_path))
    spawned = []
    monkeypatch.setattr(launcher, "_spawn_detached", lambda args, cwd=None: spawned.append((args, cwd)))

    result = launcher.launch_textgenwebui("m.gguf")

    assert result.ok
    assert spawned == [(["bash", str(tmp_path / "start_linux.sh"), "--model=m.gguf"], tmp_path)]


def test_launch_textgenwebui_binds_option_like_filename_as_model(monkeypatch, tmp_path):
    (tmp_path / "start_linux.sh").touch()
    monkeypatch.setattr(
        launcher.linker, "find_textgenwebui_root", _cached(lambda: tmp_path)
    )
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    spawned = []
    monkeypatch.setattr(
        launcher, "_spawn_detached", lambda args, cwd=None: spawned.append(args)
    )

    result = launcher.launch_textgenwebui("--listen")

    assert result.ok is True
    assert spawned == [["bash", str(tmp_path / "start_linux.sh"), "--model=--listen"]]


def test_find_windows_app_exe_matches_case_insensitively(monkeypatch, tmp_path):
    programs = tmp_path / "Programs"
    (programs / "LM Studio").mkdir(parents=True)
    (programs / "LM Studio" / "LM Studio.exe").write_bytes(b"")
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [programs])

    found = launcher.find_windows_app_exe(("lm studio",), ("lm studio",))

    assert found == programs / "LM Studio" / "LM Studio.exe"
    assert launcher.find_windows_app_exe(("jan",), ("jan",)) is None


def test_open_gui_app_windows_reports_missing_exe(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    monkeypatch.setattr(launcher, "find_windows_app_exe", lambda d, e: None)

    result = launcher.launch_jan()

    assert not result.ok
    assert "Open Jan yourself" in result.message
