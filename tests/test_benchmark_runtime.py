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


def test_start_ollama_daemon_windows_sets_new_process_group_flag(monkeypatch):
    """CREATE_NEW_PROCESS_GROUP is required for stop_ollama_daemon's
    CTRL_BREAK_EVENT to target only the daemon, not omm's own console."""
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama.exe"))
    monkeypatch.setattr(benchmark.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(benchmark.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)
    popen_calls = []

    class _FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(
        benchmark.subprocess,
        "Popen",
        lambda *a, **k: (popen_calls.append(k), _FakeProc())[1],
    )

    benchmark.start_ollama_daemon()

    assert len(popen_calls) == 1
    assert popen_calls[0]["creationflags"] == 0x08000000 | 0x00000200


def test_stop_ollama_daemon_windows_sends_ctrl_break_event(monkeypatch):
    """TerminateProcess (what .terminate() maps to on Windows) delivers no
    signal at all, so Ollama's Go runtime never runs its shutdown path and
    orphans the per-model runner subprocess(es) it spawns. CTRL_BREAK_EVENT
    is the signal it can actually catch and cascade-kill children with."""
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    monkeypatch.setattr(benchmark, "signal", __import__("types").SimpleNamespace(CTRL_BREAK_EVENT="CTRL_BREAK"))
    calls = []

    class _FakeProc:
        def poll(self):
            return None

        def send_signal(self, sig):
            calls.append(("send_signal", sig))

        def terminate(self):
            calls.append(("terminate",))

        def wait(self, timeout=None):
            return 0

    benchmark.stop_ollama_daemon(_FakeProc())

    assert calls == [("send_signal", "CTRL_BREAK")]


def test_stop_ollama_daemon_windows_falls_back_when_ctrl_break_fails(monkeypatch):
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        benchmark,
        "signal",
        __import__("types").SimpleNamespace(CTRL_BREAK_EVENT="CTRL_BREAK"),
    )
    calls = []

    class _FakeProc:
        def poll(self):
            return None

        def send_signal(self, sig):
            raise OSError("no console")

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout=None):
            return 0

    benchmark.stop_ollama_daemon(_FakeProc())

    assert calls == ["terminate"]


def test_start_failure_keeps_original_reason(monkeypatch):
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama"))
    monkeypatch.setattr(
        benchmark.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("driver initialization failed")),
    )

    assert benchmark.start_ollama_daemon() is None
    assert "driver initialization failed" in (benchmark.last_daemon_start_error() or "")


def test_one_token_or_implausible_timing_is_not_a_speed_measurement(monkeypatch):
    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

        def raise_for_status(self):
            return None

    import requests

    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: Response({"eval_count": 1, "eval_duration": 1_000}),
    )
    assert benchmark.benchmark_ollama("model") == 0.0

    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: Response({"eval_count": 64, "eval_duration": 1_000}),
    )
    assert benchmark.benchmark_ollama("model") == 0.0


def test_http_error_body_is_never_accepted_as_a_speed_measurement(monkeypatch):
    import requests

    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)

    class Response:
        def raise_for_status(self):
            raise requests.HTTPError("500")

        def json(self):
            return {"eval_count": 64, "eval_duration": 1_000_000_000}

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    assert benchmark.benchmark_ollama("model") == 0.0
