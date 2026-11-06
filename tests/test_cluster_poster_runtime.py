"""The poster drawing runtime: its own venv with pinned mflux, and the pinned model snapshot."""

from __future__ import annotations

import io
import json
import subprocess

from omm.cluster import poster_config, poster_runtime


class FakeProcess:
    def __init__(self, polls_before_exit, stdout="", returncode=0, on_poll=None):
        self._polls = polls_before_exit
        self.stdout = io.StringIO(stdout)
        self.returncode = None
        self._final = returncode
        self.killed = False
        self._on_poll = on_poll

    def poll(self):
        if self._on_poll:
            self._on_poll()
        if self.killed:
            self.returncode = -9
        elif self._polls is not None and self._polls <= 0:
            self.returncode = self._final
        elif self._polls is not None:
            self._polls -= 1
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


def test_build_venv_pins_mflux(isolated_omm_home):
    calls = []

    def run(argv, **_k):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    poster_runtime.build_venv(run=run, out=lambda *_a: None)
    assert calls[0][1:] == ["-m", "venv", str(poster_runtime.venv_dir())]
    assert calls[1] == [str(poster_runtime.venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
                        "mflux==0.24.0"]


def test_build_venv_failure_shows_pip_tail(isolated_omm_home):
    def run(argv, **_k):
        code = 1 if "pip" in argv else 0
        return subprocess.CompletedProcess(argv, code, "\n".join(f"line {i}" for i in range(50)), "")

    try:
        poster_runtime.build_venv(run=run, out=lambda *_a: None)
    except poster_config.ClusterError as error:
        assert "line 49" in str(error) and "line 10" not in str(error)
    else:
        raise AssertionError("expected ClusterError")


def test_download_writes_marker(isolated_omm_home):
    snapshot = poster_config.poster_dir() / "hf" / "snap"
    snapshot.mkdir(parents=True)
    argvs = []

    def popen(argv, **_k):
        argvs.append(argv)
        return FakeProcess(2, stdout=f"progress\n{snapshot}\n")

    path = poster_runtime.download_model(popen=popen, sleep=lambda _s: None)
    assert path == snapshot and poster_runtime.model_snapshot() == snapshot
    argv = argvs[0]
    assert argv[0] == str(poster_runtime.venv_python()) and argv[1:3] == ["-I", "-c"]
    assert argv[4:7] == [poster_config.MODEL_REPO, poster_config.MODEL_REVISION, str(poster_config.poster_dir() / "hf")]
    assert argv[7:] == list(poster_config.MODEL_ALLOW_PATTERNS)


def test_download_stall_restarts_and_clears_locks(isolated_omm_home):
    hf = poster_config.poster_dir() / "hf"
    lock = hf / ".locks" / "models--x" / "abc.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")
    snapshot = hf / "snap"
    snapshot.mkdir()
    clock = [0.0]
    processes = []

    def popen(argv, **_k):
        if not processes:
            processes.append(FakeProcess(None))  # never finishes, never grows
        else:
            processes.append(FakeProcess(1, stdout=f"{snapshot}\n"))
        return processes[-1]

    def sleep(seconds):
        clock[0] += seconds

    path = poster_runtime.download_model(popen=popen, now=lambda: clock[0], sleep=sleep, stall_seconds=300)
    assert path == snapshot
    assert processes[0].killed and len(processes) == 2
    assert not lock.exists()


def test_download_gives_up_after_five_attempts(isolated_omm_home):
    starts = []

    def popen(argv, **_k):
        starts.append(1)
        return FakeProcess(0, returncode=1)

    try:
        poster_runtime.download_model(popen=popen, sleep=lambda _s: None)
    except poster_config.ClusterError:
        pass
    else:
        raise AssertionError("expected ClusterError")
    assert len(starts) == 5


def test_snapshot_ignored_when_revision_differs(isolated_omm_home):
    snapshot = poster_config.poster_dir() / "hf" / "snap"
    snapshot.mkdir(parents=True)
    marker = poster_runtime.snapshot_marker()
    marker.write_text(json.dumps({"revision": "old", "path": str(snapshot)}), encoding="utf-8")
    assert poster_runtime.model_snapshot() is None
    marker.write_text(json.dumps({"revision": poster_config.MODEL_REVISION, "path": str(snapshot)}), encoding="utf-8")
    assert poster_runtime.model_snapshot() == snapshot


def test_remove_all_is_idempotent(isolated_omm_home):
    (poster_config.poster_dir() / "venv" / "bin").mkdir(parents=True)
    (poster_config.poster_dir() / "hf").mkdir(parents=True)
    poster_runtime.remove_all()
    poster_runtime.remove_all()
    assert not (poster_config.poster_dir() / "venv").exists()
    assert not (poster_config.poster_dir() / "hf").exists()
