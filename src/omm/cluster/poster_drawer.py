"""The worker's handle on the drawer child (poster_draw.py running in the poster venv).

The drawer loads the model, answers one JSON request line with one JSON reply
line, and exits (one picture per process, see poster_draw.py). The prompt only
ever travels inside that JSON. The process ending is also what gives the
model's memory back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from omm.cluster import poster_config


class DrawerError(Exception):
    """The drawer failed to start, crashed, or could not draw this picture."""


class DrawerTimeout(DrawerError):
    """The drawer did not answer in time; it has been killed."""


def drawer_argv(venv: Path, snapshot: Path) -> list[str]:
    script = Path(__file__).with_name("poster_draw.py")
    return [str(venv / "bin" / "python"), "-I", str(script), "--model-path", str(snapshot),
            "--model", poster_config.MFLUX_MODEL, "--quantize", str(poster_config.QUANTIZE),
            "--steps", str(poster_config.STEPS)]


def drawer_env() -> dict[str, str]:
    return {"HF_HOME": str(poster_config.poster_dir() / "hf"), "HF_HUB_OFFLINE": "1",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", str(Path.home()))}


class Drawer:
    def __init__(self, argv: list[str], env: dict[str, str], *, popen=subprocess.Popen):
        self._argv = argv
        self._env = env
        self._popen = popen
        self._process = None
        self._lines: queue.Queue = queue.Queue()
        self._last_used = time.monotonic()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _pump(self, stream, lines: queue.Queue) -> None:
        for line in stream:
            lines.put(line)
        lines.put(None)

    def _reply(self, timeout: float) -> dict:
        try:
            line = self._lines.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            self._kill()
            raise DrawerTimeout("the drawer did not answer in time") from None
        if line is None:
            self._kill()
            raise DrawerError("the drawer stopped")
        try:
            reply = json.loads(line)
        except ValueError:
            self._kill()
            raise DrawerError("the drawer printed something that is not JSON") from None
        return reply if isinstance(reply, dict) else {}

    def start(self, timeout: float) -> float:
        """Start the drawer and wait for its model to load; returns the load time in seconds."""
        self.stop()
        try:
            self._process = self._popen(self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=self._env, text=True, bufsize=1)
        except OSError as error:
            raise DrawerError(f"could not start the drawer ({type(error).__name__})") from None
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._process.stdout, self._lines), daemon=True).start()
        reply = self._reply(timeout)
        if reply.get("ready") is not True:
            self._kill()
            raise DrawerError("the drawer could not load the model")
        self._last_used = time.monotonic()
        return float(reply.get("seconds") or 0.0)

    def draw(self, prompt: str, width: int, height: int, seed: int, out: Path, timeout: float,
             quality: int = poster_config.JPEG_QUALITY) -> float:
        if not self.running:
            raise DrawerError("the drawer is not running")
        request = {"prompt": prompt, "width": width, "height": height, "seed": seed, "out": str(out),
                   "quality": quality}
        try:
            self._process.stdin.write(json.dumps(request) + "\n")
            self._process.stdin.flush()
        except (OSError, ValueError):
            self._kill()
            raise DrawerError("the drawer stopped") from None
        reply = self._reply(timeout)
        self._last_used = time.monotonic()
        if reply.get("ok") is not True:
            raise DrawerError(f"the drawer could not draw ({str(reply.get('error') or 'unknown')[:60]})")
        return float(reply.get("seconds") or 0.0)

    def idle_seconds(self, now: float) -> float:
        return now - self._last_used

    def _kill(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self._kill()
