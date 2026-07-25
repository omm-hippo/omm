"""Real generation-speed benchmark, used to build telemetry for the
recommendation model. Only benchmarks via Ollama (has a simple REST API
with built-in per-token timing) - LM Studio benchmarking can be added later.
"""

from __future__ import annotations

import shutil
import statistics
import subprocess
import time

OLLAMA_HOST = "http://localhost:11434"
_BENCHMARK_PROMPT = "Explain what an operating system is."
_NUM_PREDICT = 32
_DAEMON_START_TIMEOUT = 15.0


def ollama_daemon_reachable() -> bool:
    import requests

    try:
        requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
        return True
    except requests.RequestException:
        return False


def start_ollama_daemon(timeout: float = _DAEMON_START_TIMEOUT) -> subprocess.Popen | None:
    """Launch `ollama serve` in the background and wait until it answers.

    Returns the Popen handle (pass it to ``stop_ollama_daemon`` afterwards),
    or None if the binary is missing, failed to start, or never became
    reachable within ``timeout`` seconds.
    """
    if shutil.which("ollama") is None:
        return None
    try:
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ollama_daemon_reachable():
            return proc
        if proc.poll() is not None:
            return None
        time.sleep(0.3)
    stop_ollama_daemon(proc)
    return None


def stop_ollama_daemon(proc: subprocess.Popen) -> None:
    """Stop a daemon previously started by ``start_ollama_daemon``."""
    if proc.poll() is not None:
        return
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
    samples: list[float] = []
    for _ in range(runs):
        try:
            value = benchmark_ollama(tag, options=options)
        except TypeError:  # compatibility with old monkeypatched callables
            value = benchmark_ollama(tag)
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
