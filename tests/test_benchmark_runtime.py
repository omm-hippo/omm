from pathlib import Path

from omm import benchmark


def test_api_is_primary_even_when_ollama_is_missing_from_path(monkeypatch):
    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)

    assert benchmark.ollama_install_state() == "running_path_stale"


def test_windows_finds_documented_ollama_location(tmp_path, monkeypatch):
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("ProgramFiles", raising=False)

    assert benchmark.find_ollama_executable() == executable


def test_start_failure_keeps_original_reason(monkeypatch):
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama"))
    monkeypatch.setattr(
        benchmark.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("driver initialization failed")),
    )

    assert benchmark.start_ollama_daemon() is None
    assert "driver initialization failed" in (benchmark.last_daemon_start_error() or "")
