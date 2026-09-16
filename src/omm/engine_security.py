"""Read local runtime listeners and mutate only a proven OMM-owned Ollama."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from omm import config, linker
from omm.atomic import atomic_write_text, locked

_OWNERSHIP_PATH_NAME = "engine-server-ownership.json"


class EngineSecurityError(RuntimeError):
    pass


def _ownership_path() -> Path:
    return config.OMM_HOME / _OWNERSHIP_PATH_NAME


def _read_receipts() -> dict:
    path = _ownership_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_owned_ollama(proc, executable: str, bind: str) -> bool:
    """Record only after the freshly spawned process answers its API."""
    try:
        process = psutil.Process(proc.pid)
        receipt = {
            "pid": proc.pid,
            "create_time": process.create_time(),
            "executable": str(Path(executable).resolve()),
            "bind": bind,
        }
        path = _ownership_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked(path):
            data = _read_receipts()
            data["ollama"] = receipt
            atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        return True
    except (AttributeError, TypeError, OSError, psutil.Error, ValueError):
        return False


def clear_owned_ollama(pid: int | None = None) -> None:
    path = _ownership_path()
    try:
        with locked(path):
            data = _read_receipts()
            receipt = data.get("ollama")
            if pid is not None and isinstance(receipt, dict) and receipt.get("pid") != pid:
                return
            data.pop("ollama", None)
            if data:
                atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
            else:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def _expected_process(engine: str, pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        name = process.name().casefold()
        executable = Path(process.exe()).name.casefold()
        command = [str(value).casefold() for value in process.cmdline()]
    except (psutil.Error, OSError, ValueError):
        return False
    if engine == "ollama":
        return (
            (name == "ollama" or executable in {"ollama", "ollama.exe"})
            and any(value == "serve" for value in command[1:])
        )
    if engine == "lmstudio":
        identity = " ".join((name, executable, *command[:3]))
        return "lm studio" in identity or "lmstudio" in identity or executable in {"lms", "lms.exe"}
    return False


def _owned_ollama_process(pid: int) -> psutil.Process | None:
    receipt = _read_receipts().get("ollama")
    if not isinstance(receipt, dict) or receipt.get("pid") != pid:
        return None
    try:
        process = psutil.Process(pid)
        if abs(float(receipt["create_time"]) - process.create_time()) > 0.01:
            return None
        if Path(process.exe()).resolve() != Path(str(receipt["executable"])).resolve():
            return None
        if not _expected_process("ollama", pid):
            return None
        return process
    except (KeyError, TypeError, ValueError, OSError, psutil.Error):
        return None


def _port(engine: str) -> int | None:
    if engine == "lmstudio":
        return linker.lmstudio_server_port()
    try:
        parsed = urlsplit(__import__("omm.benchmark", fromlist=["OLLAMA_HOST"]).OLLAMA_HOST)
        return parsed.port or 11434
    except (ValueError, AttributeError):
        return 11434


def _listener_rows(port: int) -> tuple[list[dict], str | None]:
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, psutil.Error, OSError) as error:
        return [], type(error).__name__
    rows = []
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        address = getattr(connection.laddr, "ip", None)
        listener_port = getattr(connection.laddr, "port", None)
        if address is None and isinstance(connection.laddr, tuple):
            address, listener_port = connection.laddr[:2]
        if listener_port == port:
            rows.append({"address": str(address), "port": port, "pid": connection.pid})
    return rows, None


def _is_loopback(address: str) -> bool:
    import ipaddress

    if address.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def inspect(engine: str) -> dict[str, object]:
    engine = engine.strip().casefold()
    if engine not in {"ollama", "lmstudio"}:
        raise EngineSecurityError("engine security supports only ollama and lmstudio")
    port = _port(engine)
    if port is None:
        return {
            "engine": engine,
            "status": "unknown",
            "reason": "server is not running or its listening port could not be read",
            "port": None,
            "listeners": [],
            "owned_by_omm": False,
        }
    listeners, read_error = _listener_rows(port)
    if read_error:
        return {
            "engine": engine,
            "status": "unknown",
            "reason": f"listener information is unavailable ({read_error})",
            "port": port,
            "listeners": [],
            "owned_by_omm": False,
        }
    if not listeners:
        return {
            "engine": engine,
            "status": "unknown",
            "reason": "no matching listening socket was visible",
            "port": port,
            "listeners": [],
            "owned_by_omm": False,
        }
    pids = {row["pid"] for row in listeners}
    if None in pids or any(not _expected_process(engine, int(pid)) for pid in pids if pid is not None):
        return {
            "engine": engine,
            "status": "unknown",
            "reason": "the listener could not be positively identified as this engine",
            "port": port,
            "listeners": [{"address": row["address"], "port": port} for row in listeners],
            "owned_by_omm": False,
        }
    external = any(not _is_loopback(str(row["address"])) for row in listeners)
    owned = bool(
        engine == "ollama"
        and len(pids) == 1
        and _owned_ollama_process(int(next(iter(pids)))) is not None
    )
    return {
        "engine": engine,
        "status": "external_allowed" if external else "local_only",
        "reason": "a non-loopback listener accepts connections from outside this computer" if external else "all identified listeners are loopback-only",
        "port": port,
        "listeners": [{"address": row["address"], "port": port} for row in listeners],
        "owned_by_omm": owned,
        "pid": next(iter(pids)) if len(pids) == 1 else None,
    }


def fix_local_only(engine: str) -> dict[str, object]:
    status = inspect(engine)
    if status["status"] == "local_only":
        return {**status, "changed": False}
    if engine != "ollama" or status["status"] != "external_allowed":
        raise EngineSecurityError(
            "OMM cannot safely change this server. Change its host/listen setting in the engine itself, then run `omm engine security` again."
        )
    pid = status.get("pid")
    process = _owned_ollama_process(int(pid)) if isinstance(pid, int) else None
    if process is None:
        raise EngineSecurityError(
            "The Ollama listener was not started and still owned by OMM, so it was left unchanged."
        )
    try:
        process.terminate()
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except psutil.Error as error:
        raise EngineSecurityError("The OMM-owned Ollama server could not be stopped safely.") from error
    clear_owned_ollama(int(pid))

    from omm import benchmark

    proc = benchmark.start_ollama_daemon()
    if proc is None:
        raise EngineSecurityError("Ollama stopped, but could not be restarted on loopback.")
    for _ in range(20):
        refreshed = inspect("ollama")
        if refreshed["status"] == "local_only":
            return {**refreshed, "changed": True}
        time.sleep(0.1)
    benchmark.stop_ollama_daemon(proc)
    raise EngineSecurityError("The restarted Ollama listener could not be verified as local-only and was stopped.")
