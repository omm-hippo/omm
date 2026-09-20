import subprocess
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


def test_start_ollama_daemon_windows_sets_new_process_group_flag_with_console(monkeypatch):
    """CREATE_NEW_PROCESS_GROUP is required for stop_ollama_daemon's
    CTRL_BREAK_EVENT to target only the daemon, not omm's own console. When
    omm's own parent has a real console, CREATE_NO_WINDOW is skipped."""
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama.exe"))
    monkeypatch.setattr(benchmark.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(benchmark.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(benchmark, "_windows_parent_has_console", lambda: True)
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
    assert popen_calls[0]["creationflags"] == 0x00000200


def test_start_ollama_daemon_windows_sets_no_window_flag_without_console(monkeypatch):
    """Without a console of its own, omm's parent gets CREATE_NO_WINDOW too,
    so the daemon doesn't pop up a visible console window."""
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama.exe"))
    monkeypatch.setattr(benchmark.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(benchmark.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(benchmark, "_windows_parent_has_console", lambda: False)
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
        pid = 1234

        def poll(self):
            return None

        def send_signal(self, sig):
            raise OSError("no console")

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            return 0

    run_calls = []

    def fake_run(argv, **kwargs):
        run_calls.append(argv)
        return __import__("types").SimpleNamespace(returncode=0)

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    benchmark.stop_ollama_daemon(_FakeProc())

    assert run_calls == [["taskkill", "/PID", "1234", "/T", "/F"]]
    assert "terminate" not in calls


def test_stop_ollama_daemon_windows_tree_kills_after_graceful_timeout(monkeypatch):
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        benchmark,
        "signal",
        __import__("types").SimpleNamespace(CTRL_BREAK_EVENT="CTRL_BREAK"),
    )
    calls = []
    wait_calls = {"n": 0}

    class _FakeProc:
        pid = 1234

        def poll(self):
            return None

        def send_signal(self, sig):
            calls.append(("send_signal", sig))

        def kill(self):
            calls.append(("kill",))

        def wait(self, timeout=None):
            wait_calls["n"] += 1
            if wait_calls["n"] == 1:
                raise benchmark.subprocess.TimeoutExpired("ollama", 10)
            return 0

    run_calls = []

    def fake_run(argv, **kwargs):
        run_calls.append(argv)
        return __import__("types").SimpleNamespace(returncode=0)

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    benchmark.stop_ollama_daemon(_FakeProc())

    assert run_calls == [["taskkill", "/PID", "1234", "/T", "/F"]]
    assert ("kill",) not in calls


def test_stop_ollama_daemon_posix_tolerates_a_process_that_never_reaps(monkeypatch):
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Linux")
    calls = []

    class _FakeProc:
        pid = 4321

        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            raise subprocess.TimeoutExpired("ollama", timeout)

    benchmark.stop_ollama_daemon(_FakeProc())  # must not raise TimeoutExpired

    assert calls == ["terminate", ("wait", 10), "kill", ("wait", 5)]


def test_start_failure_keeps_original_reason(monkeypatch):
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama"))
    monkeypatch.setattr(
        benchmark.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("driver initialization failed")),
    )

    assert benchmark.start_ollama_daemon() is None
    assert "driver initialization failed" in (benchmark.last_daemon_start_error() or "")


def test_start_ollama_daemon_pins_ollama_host_to_loopback(monkeypatch):
    """A daemon omm launches itself must not inherit a stray OLLAMA_HOST
    from the user's environment - engine security's local-only verdict
    assumes any daemon omm starts is loopback-bound."""
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama"))
    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(benchmark.engine_security, "record_owned_ollama", lambda *a, **k: True)
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    popen_calls = []

    class _FakeProc:
        pid = 999

        def poll(self):
            return None

    monkeypatch.setattr(
        benchmark.subprocess,
        "Popen",
        lambda *a, **k: (popen_calls.append(k), _FakeProc())[1],
    )

    benchmark.start_ollama_daemon()

    assert popen_calls[0]["env"]["OLLAMA_HOST"] == "127.0.0.1:11434"


def test_start_ollama_daemon_records_ownership_on_success(monkeypatch):
    monkeypatch.setattr(benchmark, "find_ollama_executable", lambda: Path("ollama"))
    monkeypatch.setattr(benchmark, "ollama_daemon_reachable", lambda: True)

    class _FakeProc:
        pid = 999

        def poll(self):
            return None

    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *a, **k: _FakeProc())
    recorded = []
    monkeypatch.setattr(
        benchmark.engine_security,
        "record_owned_ollama",
        lambda proc, executable, bind: recorded.append((proc, executable, bind)),
    )

    result = benchmark.start_ollama_daemon()

    assert result is not None
    assert recorded == [(result, "ollama", "127.0.0.1:11434")]


def test_stop_ollama_daemon_clears_ownership_receipt_when_already_exited(monkeypatch):
    class _FakeProc:
        pid = 4321

        def poll(self):
            return 0

    cleared = []
    monkeypatch.setattr(benchmark.engine_security, "clear_owned_ollama", lambda pid: cleared.append(pid))

    benchmark.stop_ollama_daemon(_FakeProc())

    assert cleared == [4321]


def test_stop_ollama_daemon_posix_clears_ownership_receipt_after_stopping(monkeypatch):
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Linux")

    class _FakeProc:
        pid = 4321

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    cleared = []
    monkeypatch.setattr(benchmark.engine_security, "clear_owned_ollama", lambda pid: cleared.append(pid))

    benchmark.stop_ollama_daemon(_FakeProc())

    assert cleared == [4321]


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
