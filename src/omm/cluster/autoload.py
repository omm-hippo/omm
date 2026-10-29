"""Load the cluster's models again after exo forgot them (reboot or restart).

exo keeps its instances only in memory, so after a reboot every Mac comes back
with exo running and nothing served (node1 reboot test, 2026-10-25). The head
Mac's gateway service is the one process that is always up, so it runs this
check every POLL_SECONDS in a background thread.

It waits until every Mac the cluster has seen is back (a placement made while
a member is still booting would spread the model over fewer Macs), but at most
GRACE_SECONDS, so a Mac that is gone for good does not keep the models down.
Each remembered model is loaded the way the admin placed it (settings.KeptModel),
one at a time. It only ever loads; it never unloads anything to make room.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
import time

from omm.cluster import exo_client, recommend, settings
from omm.cluster.errors import ClusterError, PlacementError
from omm.cluster.exo_client import ExoClient

POLL_SECONDS = 30
GRACE_SECONDS = 600
# exo sizes a new instance for a while (and a first load downloads for
# minutes or hours); do not stack another request on top of one in flight
# until it serves or this long has passed.
RETRY_SECONDS = 600

log = logging.getLogger("omm.cluster.autoload")


@dataclass
class Tracker:
    """What one gateway process remembers between checks."""

    missing_since: float | None = None
    requested_at: float | None = None
    requested: str | None = None
    # Models whose "does not fit" is already in the log.
    reported_no_fit: set = field(default_factory=set)


def check_once(tracker: Tracker, *, client: ExoClient | None = None, now: float | None = None) -> str:
    """One pass; returns what it did (for tests and the log)."""
    now = time.monotonic() if now is None else now
    current = settings.load()
    if current is None or current.role != "head" or not current.models:
        tracker.missing_since = None
        return "nothing to keep"
    client = client or ExoClient()
    try:
        raw = client.raw_state()
    except ClusterError:
        return "exo not reachable"
    state = exo_client.parse_state(raw)
    present = {instance.model_id for instance in state.instances}
    missing = [entry for entry in current.models if entry.model not in present]
    # One that came back and goes missing again is worth a new log line.
    tracker.reported_no_fit &= {entry.model for entry in missing}
    if not missing:
        tracker.missing_since = None
        return "serving"
    if tracker.missing_since is None:
        tracker.missing_since = now
    seen = {node.name for node in state.nodes}
    waiting = [name for name in current.known_nodes if name not in seen]
    if waiting and now - tracker.missing_since < GRACE_SECONDS:
        return "waiting for " + ", ".join(waiting)
    if (tracker.requested_at is not None and now - tracker.requested_at < RETRY_SECONDS
            and not exo_client.instance_ready(raw, tracker.requested or "")):
        # A model still loading may not show in exo's free memory yet, so a
        # second placement now could be sized against the same memory.
        return "requested recently"
    outcomes = []
    for entry in missing:
        outcome = _reload(entry, tracker, client, now)
        outcomes.append(f"{entry.model}: {outcome}")
        if outcome in ("requested", "exo not reachable"):
            break
    return "; ".join(outcomes)


def _reload(entry: settings.KeptModel, tracker: Tracker, client: ExoClient, now: float) -> str:
    choice = recommend.describe(entry.sharding, entry.macs, entry.on)
    try:
        placement = recommend.choose_placement(client.previews(entry.model, on=entry.on), sharding=entry.sharding,
                                               macs=entry.macs)
    except PlacementError:
        # The Mac is not there (yet) or has no room; the next pass looks again.
        placement = None
    except ClusterError:
        return "exo not reachable"
    if placement is None:
        # Right after a restart exo may not have every Mac's memory yet; try
        # again on the next pass, but say so in the log only once.
        if entry.model not in tracker.reported_no_fit:
            log.warning("cannot reload %s (%s) yet: exo offers no such placement in the free memory it reports",
                        entry.model, choice,
                        extra={"event": "cluster_autoload_no_fit", "model": entry.model, "choice": choice})
            tracker.reported_no_fit.add(entry.model)
        return "does not fit"
    try:
        client.create_instance(placement)
    except ClusterError as error:
        log.warning("reloading %s failed: %s", entry.model, error,
                    extra={"event": "cluster_autoload_failed", "model": entry.model})
        return "request failed"
    tracker.requested_at, tracker.requested = now, entry.model
    log.info("reloading %s (%s, %s) after exo restarted", entry.model, placement.sharding, choice,
             extra={"event": "cluster_autoload", "model": entry.model, "sharding": placement.sharding,
                    "choice": choice})
    return "requested"


def run_forever(stop: threading.Event) -> None:
    tracker = Tracker()
    while not stop.wait(POLL_SECONDS):
        try:
            check_once(tracker)
        except Exception:  # never take the gateway down with it
            log.exception("cluster autoload check failed", extra={"event": "cluster_autoload_error"})


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run_forever, args=(stop,), name="omm-cluster-autoload", daemon=True).start()
    return stop
