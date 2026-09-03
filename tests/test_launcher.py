"""Tests for omm.launcher (`omm run`'s per-engine start/open logic).

tests/test_cli_run.py already exercises the launcher module's happy paths
(dispatch table, launch_ollama success, launch_koboldcpp success/missing
model, launch_textgenwebui success + option-like filename, a case-insensitive
find_windows_app_exe match, and _open_gui_app's Windows missing-exe path).
This file fills in the branches that leaves untested: Windows app-root
composition (including the OMM_HOME/apps direct-install location added by
PR #160), find_windows_app_exe's error/no-match paths, _spawn_detached's
platform-specific Popen kwargs, _open_gui_app's Darwin/Linux/flatpak/failure
branches, per-engine hint wiring, launch()'s unknown-engine error,
launch_description(), and the failure/edge branches of launch_ollama,
launch_koboldcpp and launch_textgenwebui.
"""

from __future__ import annotations

import subprocess as subprocess_module
from pathlib import Path

import pytest

from omm import launcher, linker


def _cached(fn):
    """Stand-in for an lru_cache'd linker finder - conftest's autouse
    fixtures call .cache_clear() on teardown, so the replacement needs a
    no-op one too."""
    fn.cache_clear = lambda: None
    return fn


# --- _windows_programs_roots -------------------------------------------------


def test_windows_programs_roots_is_just_omm_apps_when_no_env_vars_set(monkeypatch, tmp_path):
    apps_dir = tmp_path / "omm-apps"
    monkeypatch.setattr(linker, "engine_install_dir", lambda: apps_dir)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    assert launcher._windows_programs_roots() == [apps_dir]


def test_windows_programs_roots_puts_omm_apps_dir_first(monkeypatch, tmp_path):
    apps_dir = tmp_path / "omm-apps"
    monkeypatch.setattr(linker, "engine_install_dir", lambda: apps_dir)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))

    roots = launcher._windows_programs_roots()

    assert roots == [
        apps_dir,
        tmp_path / "Local" / "Programs",
        tmp_path / "Program Files",
        tmp_path / "Program Files (x86)",
    ]


# --- find_windows_app_exe: OMM_HOME/apps direct-install location ------------


def test_find_windows_app_exe_finds_app_under_omm_home_apps(monkeypatch, tmp_path):
    """Regression: PR #160 added OMM_HOME/apps as a direct-install location
    (AnythingLLM, Msty Studio); find_windows_app_exe must search it, not
    just the vendor %LOCALAPPDATA%\\Programs / Program Files roots. On
    current main this already passes - _windows_programs_roots() puts
    linker.engine_install_dir() first."""
    apps_dir = tmp_path / "omm-apps"
    (apps_dir / "AnythingLLM").mkdir(parents=True)
    (apps_dir / "AnythingLLM" / "AnythingLLM.exe").write_bytes(b"")
    monkeypatch.setattr(linker, "engine_install_dir", lambda: apps_dir)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    found = launcher.find_windows_app_exe(("anythingllm",), ("anythingllm",))

    assert found == apps_dir / "AnythingLLM" / "AnythingLLM.exe"


def test_find_windows_app_exe_falls_back_to_localappdata_when_not_in_omm_apps(
    monkeypatch, tmp_path
):
    apps_dir = tmp_path / "omm-apps"
    apps_dir.mkdir()  # exists but empty - OMM never saw this engine
    monkeypatch.setattr(linker, "engine_install_dir", lambda: apps_dir)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    programs = tmp_path / "Local" / "Programs" / "Jan"
    programs.mkdir(parents=True)
    (programs / "Jan.exe").write_bytes(b"")

    found = launcher.find_windows_app_exe(("jan",), ("jan",))

    assert found == programs / "Jan.exe"


# --- find_windows_app_exe: no-match / error branches ------------------------


def test_find_windows_app_exe_skips_missing_root(monkeypatch, tmp_path):
    missing_root = tmp_path / "does-not-exist"
    programs = tmp_path / "Programs"
    (programs / "Jan").mkdir(parents=True)
    (programs / "Jan" / "Jan.exe").write_bytes(b"")
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [missing_root, programs])

    found = launcher.find_windows_app_exe(("jan",), ("jan",))

    assert found == programs / "Jan" / "Jan.exe"


def test_find_windows_app_exe_ignores_dir_with_unmatched_name(monkeypatch, tmp_path):
    programs = tmp_path / "Programs"
    (programs / "SomeOtherApp").mkdir(parents=True)
    (programs / "SomeOtherApp" / "Jan.exe").write_bytes(b"")
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [programs])

    assert launcher.find_windows_app_exe(("jan",), ("jan",)) is None


def test_find_windows_app_exe_ignores_exe_with_unmatched_prefix(monkeypatch, tmp_path):
    programs = tmp_path / "Programs"
    (programs / "Jan").mkdir(parents=True)
    (programs / "Jan" / "Updater.exe").write_bytes(b"")
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [programs])

    assert launcher.find_windows_app_exe(("jan",), ("jan",)) is None


def test_find_windows_app_exe_ignores_non_exe_files(monkeypatch, tmp_path):
    programs = tmp_path / "Programs"
    (programs / "Jan").mkdir(parents=True)
    (programs / "Jan" / "jan.txt").write_bytes(b"")
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [programs])

    assert launcher.find_windows_app_exe(("jan",), ("jan",)) is None


def test_find_windows_app_exe_ignores_top_level_file_matching_dir_hint(monkeypatch, tmp_path):
    programs = tmp_path / "Programs"
    programs.mkdir(parents=True)
    (programs / "Jan").write_bytes(b"")  # a file, not a directory
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [programs])

    assert launcher.find_windows_app_exe(("jan",), ("jan",)) is None


def test_find_windows_app_exe_matches_any_of_several_hints(monkeypatch, tmp_path):
    programs = tmp_path / "Programs"
    (programs / "msty-studio").mkdir(parents=True)
    (programs / "msty-studio" / "Msty Studio.exe").write_bytes(b"")
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [programs])

    found = launcher.find_windows_app_exe(("msty studio", "msty"), ("msty",))

    assert found == programs / "msty-studio" / "Msty Studio.exe"


def test_find_windows_app_exe_none_when_no_roots_have_the_app(monkeypatch, tmp_path):
    programs = tmp_path / "Programs"
    programs.mkdir(parents=True)
    monkeypatch.setattr(launcher, "_windows_programs_roots", lambda: [programs])

    assert launcher.find_windows_app_exe(("jan",), ("jan",)) is None


# --- _spawn_detached ----------------------------------------------------------


def test_spawn_detached_uses_creation_flags_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    launcher._spawn_detached(["app.exe"], cwd=tmp_path)

    assert captured["args"] == ["app.exe"]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["creationflags"] & subprocess_module.CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in captured["kwargs"]


def test_spawn_detached_uses_start_new_session_off_windows(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    captured = {}
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda args, **kw: captured.update(kw) or captured.update(args=args)
    )

    launcher._spawn_detached(["app"])

    assert captured["start_new_session"] is True
    assert captured["cwd"] is None
    assert "creationflags" not in captured


# --- _open_gui_app: Darwin ----------------------------------------------------


def test_open_gui_app_darwin_success(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess_module.CompletedProcess(args, 0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    result = launcher.launch_jan()

    assert result.ok
    assert calls == [["open", "-a", "Jan"]]
    assert result.message == "Opened Jan."


def test_open_gui_app_darwin_app_not_found(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda args, **kw: subprocess_module.CompletedProcess(args, 1)
    )

    result = launcher.launch_jan()

    assert not result.ok
    assert "is Jan installed" in result.message


def test_open_gui_app_darwin_oserror_is_caught(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")

    def raise_oserror(args, **kw):
        raise OSError("boom")

    monkeypatch.setattr(launcher.subprocess, "run", raise_oserror)

    result = launcher.launch_jan()

    assert not result.ok
    assert "Could not start Jan" in result.message


# --- _open_gui_app: Windows ----------------------------------------------------


def test_open_gui_app_windows_spawns_found_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    exe = tmp_path / "LM Studio.exe"
    monkeypatch.setattr(launcher, "find_windows_app_exe", lambda d, e: exe)
    spawned = []
    monkeypatch.setattr(launcher, "_spawn_detached", lambda args, cwd=None: spawned.append(args))

    result = launcher.launch_lmstudio()

    assert result.ok
    assert spawned == [[str(exe)]]
    assert result.message == "Opened LM Studio."


# --- _open_gui_app: Linux/flatpak ---------------------------------------------


def test_open_gui_app_linux_uses_first_available_command(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(launcher.shutil, "which", lambda cmd: "/usr/bin/jan" if cmd == "jan" else None)
    spawned = []
    monkeypatch.setattr(launcher, "_spawn_detached", lambda args, cwd=None: spawned.append(args))

    result = launcher.launch_jan()

    assert result.ok
    assert spawned == [["/usr/bin/jan"]]


def test_open_gui_app_linux_falls_back_to_flatpak(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        launcher.shutil, "which", lambda cmd: "/usr/bin/flatpak" if cmd == "flatpak" else None
    )
    spawned = []
    monkeypatch.setattr(launcher, "_spawn_detached", lambda args, cwd=None: spawned.append(args))

    result = launcher.launch_jan()

    assert result.ok
    assert spawned == [["flatpak", "run", "ai.jan.Jan"]]
    assert "flatpak" in result.message


def test_open_gui_app_linux_no_command_and_no_flatpak_id(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(launcher.shutil, "which", lambda cmd: None)

    result = launcher.launch_lmstudio()  # no flatpak_id configured

    assert not result.ok
    assert "No launch command for LM Studio is known on Linux." in result.message


def test_open_gui_app_linux_flatpak_id_but_flatpak_missing(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(launcher.shutil, "which", lambda cmd: None)

    result = launcher.launch_jan()  # has flatpak_id, but flatpak itself is absent

    assert not result.ok
    assert "No launch command for Jan is known on Linux." in result.message


# --- per-engine hint wiring ----------------------------------------------------


def _capture_open_gui_app(monkeypatch):
    captured = {}

    def fake(label, **kw):
        captured["label"] = label
        captured.update(kw)
        return launcher.LaunchResult(True, "ok")

    monkeypatch.setattr(launcher, "_open_gui_app", fake)
    return captured


def test_launch_lmstudio_passes_expected_hints(monkeypatch):
    captured = _capture_open_gui_app(monkeypatch)

    launcher.launch_lmstudio()

    assert captured["label"] == "LM Studio"
    assert captured["mac_app"] == "LM Studio"
    assert captured["win_dir_hints"] == ("lm studio", "lm-studio", "lmstudio")
    assert captured["win_exe_hints"] == ("lm studio", "lm-studio", "lmstudio")
    assert captured["linux_commands"] == ("lm-studio", "lmstudio")
    assert captured.get("flatpak_id") is None


def test_launch_jan_passes_expected_hints(monkeypatch):
    captured = _capture_open_gui_app(monkeypatch)

    launcher.launch_jan()

    assert captured["label"] == "Jan"
    assert captured["mac_app"] == "Jan"
    assert captured["win_dir_hints"] == ("jan",)
    assert captured["linux_commands"] == ("jan",)
    assert captured["flatpak_id"] == "ai.jan.Jan"


def test_launch_anythingllm_passes_expected_hints(monkeypatch):
    captured = _capture_open_gui_app(monkeypatch)

    launcher.launch_anythingllm()

    assert captured["label"] == "AnythingLLM"
    assert captured["mac_app"] == "AnythingLLM"
    assert captured["win_dir_hints"] == ("anythingllm",)
    assert captured["linux_commands"] == ("anythingllm",)


def test_launch_mstystudio_passes_expected_hints(monkeypatch):
    captured = _capture_open_gui_app(monkeypatch)

    launcher.launch_mstystudio()

    assert captured["label"] == "Msty"
    assert captured["mac_app"] == "Msty Studio"
    assert captured["win_dir_hints"] == ("msty",)
    assert captured["linux_commands"] == ("msty-studio", "msty")


# --- launch() dispatch and launch_description() --------------------------------


def test_launch_raises_for_unknown_engine():
    with pytest.raises(ValueError, match="unknown engine"):
        launcher.launch(
            "nope", model_filename="m.gguf", model_path=Path("m.gguf"), ollama_tag="m"
        )


@pytest.mark.parametrize(
    "engine,expected_fragment",
    [
        ("ollama", "interactive chat in this terminal"),
        ("koboldcpp", "starts with the model loaded"),
        ("textgenwebui", "starts with the model loaded"),
        ("lmstudio", "opens the app"),
        ("jan", "opens the app"),
        ("anythingllm", "opens the app"),
        ("mstystudio", "opens the app"),
    ],
)
def test_launch_description_per_engine(engine, expected_fragment):
    assert expected_fragment in launcher.launch_description(engine)


# --- launch_ollama: failure/edge branches --------------------------------------


def test_launch_ollama_missing_executable(monkeypatch):
    monkeypatch.setattr(linker, "find_ollama_executable", lambda: None)

    result = launcher.launch_ollama("repo-q4")

    assert not result.ok
    assert "Ollama is not installed" in result.message


def test_launch_ollama_keyboard_interrupt_treated_as_clean_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(linker, "find_ollama_executable", lambda: tmp_path / "ollama")

    def raise_kb(args, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.subprocess, "call", raise_kb)

    result = launcher.launch_ollama("repo-q4")

    assert result.ok
    assert result.interactive
    assert result.message == "Chat ended."


def test_launch_ollama_oserror_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(linker, "find_ollama_executable", lambda: tmp_path / "ollama")

    def raise_oserror(args, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(launcher.subprocess, "call", raise_oserror)

    result = launcher.launch_ollama("repo-q4")

    assert not result.ok
    assert "Could not start" in result.message


# --- launch_koboldcpp: failure branches -----------------------------------------


def test_launch_koboldcpp_binary_missing(monkeypatch):
    monkeypatch.setattr(linker, "find_koboldcpp_binary", _cached(lambda: None))

    result = launcher.launch_koboldcpp(Path("m.gguf"))

    assert not result.ok
    assert result.message == "KoboldCpp binary not found."


def test_launch_koboldcpp_spawn_failure(monkeypatch, tmp_path):
    binary = tmp_path / "koboldcpp.exe"
    binary.write_bytes(b"")
    model = tmp_path / "m.gguf"
    model.write_bytes(b"")
    monkeypatch.setattr(linker, "find_koboldcpp_binary", _cached(lambda: binary))

    def raise_oserror(args, cwd=None):
        raise OSError("boom")

    monkeypatch.setattr(launcher, "_spawn_detached", raise_oserror)

    result = launcher.launch_koboldcpp(model)

    assert not result.ok
    assert "Could not start KoboldCpp" in result.message


# --- launch_textgenwebui: failure/edge branches ---------------------------------


def test_launch_textgenwebui_root_not_found(monkeypatch):
    monkeypatch.setattr(linker, "find_textgenwebui_root", _cached(lambda: None))

    result = launcher.launch_textgenwebui("m.gguf")

    assert not result.ok
    assert result.message == "text-generation-webui install not found."


def test_launch_textgenwebui_missing_start_script(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "find_textgenwebui_root", _cached(lambda: tmp_path))

    result = launcher.launch_textgenwebui("m.gguf")

    assert not result.ok
    assert "No start script found" in result.message
    assert "m.gguf" in result.message


def test_launch_textgenwebui_unknown_platform_has_no_start_script(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(linker, "find_textgenwebui_root", _cached(lambda: tmp_path))

    result = launcher.launch_textgenwebui("m.gguf")

    assert not result.ok
    assert "No start script found" in result.message


def test_launch_textgenwebui_windows_omits_bash_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    (tmp_path / "start_windows.bat").write_text("", encoding="utf-8")
    monkeypatch.setattr(linker, "find_textgenwebui_root", _cached(lambda: tmp_path))
    spawned = []
    monkeypatch.setattr(launcher, "_spawn_detached", lambda args, cwd=None: spawned.append(args))

    result = launcher.launch_textgenwebui("m.gguf")

    assert result.ok
    assert spawned == [[str(tmp_path / "start_windows.bat"), "--model=m.gguf"]]


def test_launch_textgenwebui_spawn_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    (tmp_path / "start_linux.sh").write_text("", encoding="utf-8")
    monkeypatch.setattr(linker, "find_textgenwebui_root", _cached(lambda: tmp_path))

    def raise_oserror(args, cwd=None):
        raise OSError("boom")

    monkeypatch.setattr(launcher, "_spawn_detached", raise_oserror)

    result = launcher.launch_textgenwebui("m.gguf")

    assert not result.ok
    assert "Could not start text-generation-webui" in result.message
