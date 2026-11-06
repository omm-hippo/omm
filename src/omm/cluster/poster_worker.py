"""The poster art worker loop (LaunchAgent com.omm.cluster-poster, `omm _cluster-poster-run`).

Between jobs it checks a gate: enough free memory for one picture, macOS memory
pressure not critical, and exo not bringing up a cluster model on this Mac (the
cluster LLM gets memory first). A closed gate sends neither claim nor ping, so
the site sees this Mac as gone and falls back to the cloud. With the gate open
it claims every 3 s; a job is drawn by a fresh drawer process, uploaded, and
the drawer ends. Logs carry the job id, outcome and seconds only: never the
token, the job secret or the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import secrets
import subprocess
import threading
import time

from omm.cluster import poster_config, poster_contract
from omm.cluster.poster_contract import BadJob, Job, TransientError
from omm.cluster.poster_drawer import DrawerError, DrawerTimeout

log = logging.getLogger("omm.cluster.poster")

IDLE_SECONDS = 3
PING_SECONDS = 15
LEASE_SECONDS = 180
DRAW_MARGIN_SECONDS = 30
BACKOFF = (3, 6, 12, 24, 48, 60)
# A Mac whose drawer keeps failing (broken venv, corrupt model, full disk) must
# not eat the whole queue a job per second: after this many failures in a row
# it goes quiet for a growing pause, so the site sees it as gone.
DRAWER_FAILS_BEFORE_PAUSE = 3
DRAWER_PAUSE_SECONDS = (300, 600, 1200, 1800, 3600)
_SETUP_RETRY_SECONDS = 60
_LOG_HINT = "~/.omm/logs/cluster-poster.log"


# exo 1.0.x runner states while a model instance is being brought up. RunnerReady
# and RunnerRunning (busy answering a request) are a model already in memory;
# failed/shut-down/unknown states do not hold the gate, or one stuck placement
# would keep the worker off forever.
_EXO_LOADING = frozenset({"RunnerLoading", "RunnerLoaded", "RunnerWarmingUp"})


def exo_wants_memory(raw_state: dict | None, own_node_id: str | None) -> bool:
    """True while exo brings up a model instance that uses this Mac (no runner yet, or one still loading)."""
    if not isinstance(raw_state, dict) or not own_node_id:
        return False
    runners = raw_state.get("runners") if isinstance(raw_state.get("runners"), dict) else {}
    for wrapper in (raw_state.get("instances") or {}).values():
        if not isinstance(wrapper, dict) or len(wrapper) != 1:
            continue
        body = next(iter(wrapper.values()))
        assignments = body.get("shardAssignments") if isinstance(body, dict) else None
        node_to_runner = assignments.get("nodeToRunner") if isinstance(assignments, dict) else None
        if not isinstance(node_to_runner, dict) or own_node_id not in node_to_runner:
            continue
        runner = runners.get(node_to_runner[own_node_id])
        if not isinstance(runner, dict) or not runner or next(iter(runner)) in _EXO_LOADING:
            return True
    return False


def gate(exo_busy: bool, budget_gb: float, pressure_critical: bool) -> tuple[bool, str]:
    if pressure_critical:
        return False, "waiting for memory"
    if exo_busy:
        return False, "cluster model loading"
    if budget_gb < poster_config.MODEL_NEED_GB:
        return False, "waiting for memory"
    return True, "ready"


def _state_path() -> Path:
    return poster_config.poster_dir() / "state.json"


def read_state() -> dict | None:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(**fields) -> None:
    from omm.atomic import atomic_write_text

    state = read_state() or {}
    state.update(fields, updated_at=time.time())
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_state_path(), json.dumps(state) + "\n")
    except OSError:
        log.warning("could not write the poster worker state", extra={"event": "poster_state_failed"})


@dataclass
class _Backoff:
    failures: int = 0

    def next(self) -> int:
        delay = BACKOFF[min(self.failures, len(BACKOFF) - 1)]
        self.failures += 1
        return delay


class Worker:
    def __init__(self, client, make_drawer, *, budget_gb, pressure_critical, exo_state,
                 now=time.monotonic, sleep=time.sleep):
        self._client = client
        self._make_drawer = make_drawer
        self._budget_gb = budget_gb
        self._pressure_critical = pressure_critical
        self._exo_state = exo_state
        self._now = now
        self._sleep = sleep
        self._backoff = _Backoff()
        self._phase = None
        self._drawer_failures = 0
        self._pauses = 0
        self._paused_until = 0.0

    def _set_phase(self, phase: str, **extra) -> None:
        if phase != self._phase or extra:
            self._phase = phase
            write_state(phase=phase, **extra)

    def step(self) -> float:
        """One pass of the loop; returns how long to wait before the next."""
        if self._now() < self._paused_until:
            self._set_phase(f"drawer failing (see {_LOG_HINT})")
            return self._paused_until - self._now()
        raw, own = self._exo_state()
        budget = self._budget_gb()
        open_, phase = gate(exo_wants_memory(raw, own), budget, self._pressure_critical())
        if not open_:
            self._set_phase(phase, budget_gb=round(budget, 1))
            return IDLE_SECONDS
        self._set_phase("ready")
        try:
            job = self._client.claim()
        except TransientError as error:
            delay = self._backoff.next()
            log.info("poster claim failed (%s); next try in %d s", error, delay,
                     extra={"event": "poster_claim_failed"})
            return delay
        except BadJob as error:
            self._backoff = _Backoff()
            log.warning("poster job %s unreadable", error.job_id or "?", extra={"event": "poster_bad_job"})
            if error.job_id:
                self._client.fail(error.job_id, "bad job")
            return IDLE_SECONDS
        self._backoff = _Backoff()
        if job is None:
            return IDLE_SECONDS
        self.run_job(job, self._now())
        return 0

    def _drawer_failed(self) -> None:
        self._drawer_failures += 1
        if self._drawer_failures >= DRAWER_FAILS_BEFORE_PAUSE:
            pause = DRAWER_PAUSE_SECONDS[min(self._pauses, len(DRAWER_PAUSE_SECONDS) - 1)]
            self._pauses += 1
            self._drawer_failures = DRAWER_FAILS_BEFORE_PAUSE - 1  # one more failure pauses again, longer
            self._paused_until = self._now() + pause
            log.warning("poster drawer failed %d times in a row; pausing %d s", DRAWER_FAILS_BEFORE_PAUSE, pause,
                        extra={"event": "poster_drawer_paused"})

    def _ping_loop(self, wait) -> None:
        # `wait` is Event.wait: True once the job is over.
        while not wait(PING_SECONDS):
            self._client.ping()

    def run_job(self, job: Job, claimed_at: float) -> str:
        deadline = claimed_at + LEASE_SECONDS
        done = threading.Event()
        threading.Thread(target=self._ping_loop, args=(done.wait,), daemon=True).start()
        self._set_phase("drawing a picture")
        tmp = poster_config.poster_dir() / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        out = tmp / f"{job.id}.jpg"
        drawer = self._make_drawer()
        started = self._now()
        outcome = "failed"
        try:
            width, height = poster_contract.fit_size(job.width, job.height)
            drawer.start(timeout=deadline - DRAW_MARGIN_SECONDS - self._now())
            drawer.draw(job.prompt, width, height, secrets.randbelow(2 ** 31), out,
                        timeout=deadline - DRAW_MARGIN_SECONDS - self._now())
            jpeg = out.read_bytes()
            if len(jpeg) > poster_contract.MAX_IMAGE_BYTES:
                self._client.fail(job.id, "image too large")
            else:
                outcome = poster_contract.upload_with_retries(self._client, job, jpeg, deadline,
                                                              now=self._now, sleep=self._sleep)
        except DrawerTimeout:
            self._client.fail(job.id, "timeout")
            self._drawer_failed()
        except (DrawerError, OSError):
            self._client.fail(job.id, "drawer error")
            self._drawer_failed()
        finally:
            done.set()
            drawer.stop()
            out.unlink(missing_ok=True)
        if outcome != "failed":
            self._drawer_failures, self._pauses = 0, 0
        seconds = self._now() - started
        log.info("poster job %s: %s in %.0f s", job.id, outcome, seconds,
                 extra={"event": "poster_job", "job_id": job.id, "outcome": outcome, "seconds": round(seconds, 1)})
        self._phase = None
        self._set_phase("ready", last_job_id=job.id, last_outcome=outcome, last_seconds=round(seconds, 1),
                        last_at=time.time())
        return outcome


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{max(int(seconds), 0)} s ago"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min ago"
    return f"{round(seconds / 3600)} h ago"


def status_line(state: dict | None, installed: bool, running: bool, now: float) -> str:
    if not installed:
        return "Poster art: off"
    if not running:
        return f"Poster art: on — not running (see {_LOG_HINT})"
    state = state or {}
    phase = state.get("phase") or "starting"
    if phase == "waiting for memory" and isinstance(state.get("budget_gb"), (int, float)):
        return (f"Poster art: on — waiting for memory ({int(state['budget_gb'])} GiB free, "
                f"needs {int(poster_config.MODEL_NEED_GB)})")
    if phase == "cluster model loading":
        return "Poster art: on — paused while a cluster model loads on this Mac"
    if phase == "ready" and state.get("last_job_id"):
        last_at = state.get("last_at") if isinstance(state.get("last_at"), (int, float)) else now
        return (f"Poster art: on — ready, last job {str(state['last_job_id'])[:4]}… {state.get('last_outcome')} "
                f"in {round(float(state.get('last_seconds') or 0))} s ({_ago(now - last_at)})")
    return f"Poster art: on — {phase}"


def _budget_gb() -> float:
    from omm import hardware

    return hardware.calculate_memory_budget(hardware.scan_hardware()).model_budget_gb


def _pressure_critical(run=subprocess.run) -> bool:
    try:
        result = run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                     capture_output=True, text=True, check=False, timeout=5)
        return result.stdout.strip() == "4"
    except (OSError, subprocess.SubprocessError):
        return False


def _exo_view():
    """exo's state on loopback, read-only, and this Mac's node id (it changes on every exo restart)."""
    from omm.cluster.errors import ClusterError
    from omm.cluster.exo_client import ExoClient

    client = ExoClient()
    try:
        return client.raw_state(timeout=5), client.node_id()
    except (ClusterError, OSError):
        return None, None


def run_worker() -> int:
    from omm.cluster import poster_drawer, poster_runtime

    while True:
        settings, token = poster_config.load_settings(), poster_config.read_token()
        if settings is None or token is None:
            write_state(phase="not set up; run omm cluster poster enable")
            log.warning("poster worker has no settings or token", extra={"event": "poster_not_set_up"})
            time.sleep(_SETUP_RETRY_SECONDS)
            continue
        snapshot = poster_runtime.model_snapshot()
        if snapshot is None:
            write_state(phase="downloading the model")
            try:
                snapshot = poster_runtime.download_model()
            except Exception:
                log.warning("poster model download failed", exc_info=True, extra={"event": "poster_download_failed"})
                time.sleep(_SETUP_RETRY_SECONDS)
                continue
        break
    venv = poster_runtime.venv_dir()
    worker = Worker(poster_contract.SiteClient(settings, token),
                    lambda: poster_drawer.Drawer(poster_drawer.drawer_argv(venv, snapshot), poster_drawer.drawer_env()),
                    budget_gb=_budget_gb, pressure_critical=_pressure_critical, exo_state=_exo_view)
    log.info("poster worker started", extra={"event": "poster_started"})
    while True:
        try:
            delay = worker.step()
        except Exception:
            log.warning("poster worker step failed", exc_info=True, extra={"event": "poster_step_failed"})
            delay = BACKOFF[-1]
        if delay:
            time.sleep(delay)
