from omm import session_cache


def _fake_tty(monkeypatch, name="/dev/faketty0"):
    # raising=False: os.ttyname doesn't exist as an attribute on Windows.
    monkeypatch.setattr(session_cache.os, "ttyname", lambda fd: name, raising=False)


def _no_tty(monkeypatch):
    def _raise(fd):
        raise OSError("not a tty")

    monkeypatch.setattr(session_cache.os, "ttyname", _raise, raising=False)


def _windows_no_ttyname(monkeypatch):
    # Simulates os.ttyname not existing at all, the real situation on
    # Windows - AttributeError, not OSError.
    def _raise(fd):
        raise AttributeError("module 'os' has no attribute 'ttyname'")

    monkeypatch.setattr(session_cache.os, "ttyname", _raise, raising=False)


class _FakeKernel32:
    def __init__(self, hwnd):
        self.hwnd = hwnd

    def GetConsoleWindow(self):
        return self.hwnd


class _FakeWindll:
    def __init__(self, hwnd):
        self.kernel32 = _FakeKernel32(hwnd)


def _fake_console_window(monkeypatch, hwnd):
    monkeypatch.setattr(
        session_cache.ctypes, "windll", _FakeWindll(hwnd), raising=False
    )


def test_record_and_load_seen_roundtrips(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_seen(["a", "b"])

    assert session_cache.load_seen() == ["a", "b"]


def test_record_seen_dedupes_and_moves_to_front(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_seen(["a", "b"])
    session_cache.record_seen(["b", "c"])

    assert session_cache.load_seen() == ["b", "c", "a"]


def test_record_seen_caps_at_50(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    for i in range(60):
        session_cache.record_seen([f"model-{i}"])

    assert len(session_cache.load_seen()) == 50
    assert "model-59" in session_cache.load_seen()


def test_record_results_overwrites_last_results(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_results(["x", "y"])
    assert session_cache.load_last_results() == ["x", "y"]

    session_cache.record_results(["z"])
    assert session_cache.load_last_results() == ["z"]


def test_record_results_also_updates_seen(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_results(["x", "y"])

    assert session_cache.load_seen() == ["x", "y"]


def test_no_tty_is_a_silent_noop(isolated_omm_home, monkeypatch):
    _no_tty(monkeypatch)

    session_cache.record_seen(["a"])
    session_cache.record_results(["b"])

    assert session_cache.load_seen() == []
    assert session_cache.load_last_results() == []


def test_different_ttys_do_not_share_state(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch, "/dev/tty-one")
    session_cache.record_results(["from-tty-one"])

    _fake_tty(monkeypatch, "/dev/tty-two")
    assert session_cache.load_last_results() == []


def test_windows_session_survives_new_pid_per_invocation(
    isolated_omm_home, monkeypatch
):
    # Regression test: pipx (and other console-script shims) install `omm`
    # on Windows as an .exe launcher stub that spawns python.exe as a new
    # child process on every invocation, so os.getppid() returns a
    # different value each call even within the same console window. The
    # console window handle must be used instead, so results recorded by
    # one "invocation" are still visible to the next despite getppid()
    # changing in between.
    _windows_no_ttyname(monkeypatch)
    _fake_console_window(monkeypatch, 999)
    monkeypatch.setattr(session_cache.os, "getppid", lambda: 1111)

    session_cache.record_results(["a", "b"])

    monkeypatch.setattr(session_cache.os, "getppid", lambda: 2222)

    assert session_cache.load_last_results() == ["a", "b"]


def test_windows_falls_back_to_getppid_when_no_console(
    isolated_omm_home, monkeypatch
):
    _windows_no_ttyname(monkeypatch)
    _fake_console_window(monkeypatch, 0)
    monkeypatch.setattr(session_cache.os, "getppid", lambda: 4242)

    session_cache.record_results(["x"])

    assert session_cache.load_last_results() == ["x"]


def test_corrupted_cache_file_is_treated_as_empty(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)
    session_cache.record_seen(["a"])

    from omm import config

    session_dir = config.OMM_HOME / "session"
    for f in session_dir.iterdir():
        f.write_text("{not valid json")

    assert session_cache.load_seen() == []
