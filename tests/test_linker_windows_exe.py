"""engine_windows_executable (#368): locating each engine's main .exe so
engine_manager can read its version resource. Runs on every OS by faking
platform.system() and the program roots."""
from omm import linker


def _fake_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker, "_windows_program_roots", lambda: [tmp_path])
    monkeypatch.delenv("LOCALAPPDATA", raising=False)


def test_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    assert linker.engine_windows_executable("lmstudio") is None


def test_finds_the_known_main_exe_not_the_uninstaller(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    folder = tmp_path / "LM Studio"
    folder.mkdir()
    (folder / "Uninstall LM Studio.exe").write_bytes(b"MZ")
    (folder / "LM Studio.exe").write_bytes(b"MZ")
    assert linker.engine_windows_executable("lmstudio") == folder / "LM Studio.exe"


def test_accepts_a_single_unknown_exe_name(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    folder = tmp_path / "Msty Studio"
    folder.mkdir()
    (folder / "Uninstall Msty Studio.exe").write_bytes(b"MZ")
    (folder / "MstyNext.exe").write_bytes(b"MZ")
    assert linker.engine_windows_executable("mstystudio") == folder / "MstyNext.exe"


def test_refuses_to_guess_between_several_unknown_exes(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    folder = tmp_path / "AnythingLLM"
    folder.mkdir()
    (folder / "helper.exe").write_bytes(b"MZ")
    (folder / "other.exe").write_bytes(b"MZ")
    assert linker.engine_windows_executable("anythingllm") is None


def test_jan_is_found_under_localappdata(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path / "programs")
    local = tmp_path / "local"
    (local / "Jan").mkdir(parents=True)
    (local / "Jan" / "Jan.exe").write_bytes(b"MZ")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert linker.engine_windows_executable("jan") == local / "Jan" / "Jan.exe"


def test_ollama_reuses_find_ollama_executable(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    exe = tmp_path / "ollama.exe"
    monkeypatch.setattr(linker, "find_ollama_executable", lambda: exe)
    assert linker.engine_windows_executable("ollama") == exe


def test_engines_without_an_installer_have_no_exe(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    assert linker.engine_windows_executable("koboldcpp") is None
    assert linker.engine_windows_executable("textgenwebui") is None


def test_not_installed_is_none(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    assert linker.engine_windows_executable("lmstudio") is None
