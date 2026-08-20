"""Ollama daemon-lifecycle helpers (start/stop/status check) and basic
speed-benchmark functions for omm install's one-off model compat check.
For LM Studio speed/quality measurement in omm benchmark/contribute, see
quality.py's _generate_with_runtime when engine="lmstudio" is selected.
"""

from __future__ import annotations

import platform
import shutil
import signal
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from omm.linker import find_ollama_executable  # noqa: F401 - re-exported for callers

OLLAMA_HOST = "http://localhost:11434"
_BENCHMARK_PROMPT = "Explain what an operating system is."
# Matches quality._speed_probe's measured num_predict, so speed numbers from
# this simple probe and the full `omm benchmark` speed probe stay comparable.
_NUM_PREDICT = 64
_DAEMON_START_TIMEOUT = 15.0
_last_daemon_start_error: str | None = None


def ollama_install_state() -> str:
    """Return running, running_path_stale, stopped, or missing."""
    if ollama_daemon_reachable():
        return "running" if shutil.which("ollama") else "running_path_stale"
    return "stopped" if find_ollama_executable() else "missing"


def last_daemon_start_error() -> str | None:
    return _last_daemon_start_error


def ollama_daemon_reachable() -> bool:
    import requests

    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
        response.raise_for_status()
        return isinstance(response.json(), dict)
    except requests.RequestException:
        return False


def start_ollama_daemon(timeout: float = _DAEMON_START_TIMEOUT) -> subprocess.Popen | None:
    """Launch `ollama serve` in the background and wait until it answers.

    Returns the Popen handle (pass it to ``stop_ollama_daemon`` afterwards),
    or None if the binary is missing, failed to start, or never became
    reachable within ``timeout`` seconds.
    """
    global _last_daemon_start_error
    _last_daemon_start_error = None
    executable = find_ollama_executable()
    if executable is None:
        _last_daemon_start_error = (
            "Ollama is not installed, or its executable could not be found. "
            "Install it from https://ollama.com/download."
        )
        return None
    error_log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    try:
        if platform.system() == "Windows":
            # CREATE_NEW_PROCESS_GROUP is required for stop_ollama_daemon's
            # CTRL_BREAK_EVENT to target only this process - without it, the
            # event would also hit omm's own console/process.
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creationflags = 0
        proc = subprocess.Popen(
            [str(executable), "serve"],
            stdout=subprocess.DEVNULL,
            stderr=error_log,
            start_new_session=True,
            creationflags=creationflags,
        )
    except OSError as error:
        error_log.close()
        _last_daemon_start_error = f"Could not launch {executable}: {error}"
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ollama_daemon_reachable():
            error_log.close()
            return proc
        if proc.poll() is not None:
            error_log.seek(0)
            detail = error_log.read().strip()
            error_log.close()
            _last_daemon_start_error = detail or f"Ollama exited with code {proc.returncode}."
            return None
        time.sleep(0.3)
    stop_ollama_daemon(proc)
    error_log.seek(0)
    detail = error_log.read().strip()
    error_log.close()
    _last_daemon_start_error = detail or f"Ollama did not answer at {OLLAMA_HOST} within {timeout:g}s."
    return None


def stop_ollama_daemon(proc: subprocess.Popen) -> None:
    """Stop a daemon previously started by ``start_ollama_daemon``.

    On Windows, ``Popen.terminate()`` maps straight to ``TerminateProcess``
    - a hard kill that delivers no signal at all, so Ollama's own Go
    runtime never runs its shutdown path and the per-model "runner"
    subprocess(es) it spawns are left orphaned, still holding GPU/RAM
    (invisible in the taskbar since the daemon was started with
    CREATE_NO_WINDOW). ``CTRL_BREAK_EVENT`` is the Windows analogue of
    SIGTERM Ollama can actually catch and cascade-kill its children with -
    it only reaches a process started in its own process group, which
    start_ollama_daemon sets up via CREATE_NEW_PROCESS_GROUP.
    """
    if proc.poll() is not None:
        return
    if platform.system() == "Windows":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def benchmark_ollama(tag: str, options: dict | None = None) -> float | None:
    """Return tokens/sec, 0.0 if generation was attempted and failed, or
    None if the daemon wasn't reachable at all (untestable, not a failure).
    """
    import requests

    if not ollama_daemon_reachable():
        return None

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": tag,
                "prompt": _BENCHMARK_PROMPT,
                "stream": False,
                "options": {"num_predict": _NUM_PREDICT, **(options or {})},
            },
            timeout=120,
        )
        data = resp.json()
        eval_count = data.get("eval_count")
        eval_duration = data.get("eval_duration")
        if not eval_count or not eval_duration:
            return 0.0
        return eval_count / (eval_duration / 1e9)
    except (requests.RequestException, ValueError):
        return 0.0


def benchmark_ollama_samples(
    tag: str, runs: int = 3, options: dict | None = None
) -> dict | None:
    """Run a reproducible set of speed probes with identical options.

    ``benchmark_ollama`` remains the one-shot public API for callers which
    only need a float.  A ``None`` result still means the daemon was not
    reachable; failed individual generations are retained as zero samples.
    """
    if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= 10:
        raise ValueError("runs must be an integer from 1 to 10")
    # Resolve the module-global once, up front, rather than re-looking it up
    # on every iteration. If a caller running on a background thread gets
    # abandoned mid-loop (see cli._run_interruptible), a later monkeypatch
    # (or, in production, a hot-swapped implementation) of module-level
    # ``benchmark_ollama`` must not get silently picked up partway through
    # an in-flight call - that's what let one leaked worker thread's
    # straggling iterations start invoking whatever function an unrelated,
    # later caller had since installed.
    _benchmark_ollama = benchmark_ollama
    samples: list[float] = []
    for _ in range(runs):
        try:
            value = _benchmark_ollama(tag, options=options)
        except TypeError:  # compatibility with old monkeypatched callables
            value = _benchmark_ollama(tag)
        if value is None:
            return None
        samples.append(float(value))
    return {
        "median_tokens_per_sec": statistics.median(samples),
        "min_tokens_per_sec": min(samples),
        "max_tokens_per_sec": max(samples),
        "count": len(samples),
        "samples_tokens_per_sec": samples,
        "options": dict(options or {}),
    }
