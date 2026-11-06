"""The poster drawing runtime: a venv of its own with pinned mflux, and the pinned model.

mflux pulls torch (the venv is ~1.4 GB), so it lives in OMM_HOME/cluster/poster/venv,
not in omm's own venv, where an omm reinstall would drop it. The model goes to
OMM_HOME/cluster/poster/hf so `disable` removes everything this feature added.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from omm.cluster import poster_config
from omm.cluster.errors import ClusterError

log = logging.getLogger("omm.cluster.poster")

DOWNLOAD_ATTEMPTS = 5
_POLL_SECONDS = 5
# Repo, revision, cache dir and patterns come in as argv, never formatted into the code.
_DOWNLOAD = ("import sys\n"
             "from huggingface_hub import snapshot_download\n"
             "print(snapshot_download(sys.argv[1], revision=sys.argv[2], cache_dir=sys.argv[3], "
             "allow_patterns=sys.argv[4:]), flush=True)\n")


def venv_dir() -> Path:
    return poster_config.poster_dir() / "venv"


def venv_python() -> Path:
    return venv_dir() / "bin" / "python"


def _hf_dir() -> Path:
    return poster_config.poster_dir() / "hf"


def snapshot_marker() -> Path:
    return _hf_dir() / "ready.json"


def build_venv(*, run=subprocess.run, out=print) -> None:
    base = getattr(sys, "_base_executable", None) or sys.executable
    out("Creating the drawing environment (mflux, about 1.4 GB)...")
    result = run([base, "-m", "venv", str(venv_dir())], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        out(f"Installing {poster_config.MFLUX_REQUIREMENT}...")
        result = run([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
                      poster_config.MFLUX_REQUIREMENT], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        tail = "\n".join(((result.stdout or "") + (result.stderr or "")).splitlines()[-20:])
        raise ClusterError(f"could not set up the drawing environment:\n{tail}")


def venv_ready(*, run=subprocess.run) -> bool:
    if not venv_python().is_file():
        return False
    return run([str(venv_python()), "-I", "-c", "import mflux"], capture_output=True, check=False).returncode == 0


def model_snapshot() -> Path | None:
    try:
        data = json.loads(snapshot_marker().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("revision") != poster_config.MODEL_REVISION:
        return None
    path = Path(str(data.get("path")))
    return path if path.is_dir() else None


def _bytes_under(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _clear_locks() -> None:
    # A killed download leaves its .lock files; the next one then waits on them forever.
    for lock in (_hf_dir() / ".locks").rglob("*.lock"):
        lock.unlink(missing_ok=True)


def _download_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HF_")}
    env.update({"HF_HUB_DOWNLOAD_TIMEOUT": "60", "HF_HUB_DISABLE_TELEMETRY": "1"})
    return env


def download_model(*, popen=subprocess.Popen, now=time.monotonic, sleep=time.sleep,
                   stall_seconds: float = 300) -> Path:
    """Fetch the pinned snapshot; a download that stops growing for `stall_seconds` is restarted."""
    hf = _hf_dir()
    hf.mkdir(parents=True, exist_ok=True)
    argv = [str(venv_python()), "-I", "-c", _DOWNLOAD, poster_config.MODEL_REPO, poster_config.MODEL_REVISION,
            str(hf), *poster_config.MODEL_ALLOW_PATTERNS]
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        process = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=_download_env())
        size, grew_at = _bytes_under(hf), now()
        while process.poll() is None:
            sleep(_POLL_SECONDS)
            current = _bytes_under(hf)
            if current != size:
                size, grew_at = current, now()
            elif now() - grew_at > stall_seconds:
                log.warning("model download stalled; restarting (attempt %d)", attempt,
                            extra={"event": "poster_download_stalled"})
                process.kill()
                process.wait(timeout=10)
                break
        if process.returncode == 0:
            lines = [line.strip() for line in process.stdout.read().splitlines() if line.strip()]
            path = Path(lines[-1]) if lines else None
            if path is not None and path.is_dir():
                snapshot_marker().write_text(json.dumps({"revision": poster_config.MODEL_REVISION,
                                                         "path": str(path)}), encoding="utf-8")
                return path
        _clear_locks()
        sleep(_POLL_SECONDS)
    raise ClusterError(f"could not download {poster_config.MODEL_REPO} after {DOWNLOAD_ATTEMPTS} attempts")


def remove_all() -> None:
    for name in ("venv", "hf", "tmp"):
        shutil.rmtree(poster_config.poster_dir() / name, ignore_errors=True)
