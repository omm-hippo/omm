"""Load the cluster's model again after exo forgot it (reboot or restart).

exo keeps its instances only in memory, so after a reboot every Mac comes back
with exo running and nothing served (node1 reboot test, 2026-10-25). The head
Mac's gateway service is the one process that is always up, so it runs this
check every POLL_SECONDS in a background thread.

It waits until every Mac the cluster has seen is back (a placement made while
a member is still booting would spread the model over fewer Macs), but at most
GRACE_SECONDS, so a Mac that is gone for good does not keep the model down.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time

from omm.cluster import recommend, settings
from omm.cluster.errors import ClusterError
from omm.cluster.exo_client import ExoClient

POLL_SECONDS = 30
GRACE_SECONDS = 600
# exo sizes a new instance for a while (and a first load downloads for
# minutes or hours); do not stack another request on top of one in flight.
RETRY_SECONDS = 600

log = logging.getLogger("omm.cluster.autoload")


@dataclass
class Tracker:
    """What one gateway process remembers between checks."""

    missing_since: float | None = None
    requested_at: float | None = None
    reported_no_fit: bool = False


def check_once(tracker: Tracker, *, client: ExoClient | None = None, now: float | None = None) -> str:
    """One pass; returns what it did (for tests and the log)."""
    now = time.monotonic() if now is None else now
    current = settings.load()
    if current is None or current.role != "head" or not current.model:
        tracker.missing_since = None
        return "nothing to keep"
    client = client or ExoClient()
    try:
        state = client.state()
    except ClusterError:
        return "exo not reachable"
    if any(instance.model_id == current.model for instance in state.instances):
        tracker.missing_since = None
        tracker.reported_no_fit = False
        return "serving"
    if tracker.missing_since is None:
        tracker.missing_since = now
    seen = {node.name for node in state.nodes}
    waiting = [name for name in current.known_nodes if name not in seen]
    if waiting and now - tracker.missing_since < GRACE_SECONDS:
        return "waiting for " + ", ".join(waiting)
    if tracker.requested_at is not None and now - tracker.requested_at < RETRY_SECONDS:
        return "requested recently"
    try:
        placement = recommend.choose_placement(client.previews(current.model))
    except ClusterError:
        return "exo not reachable"
    if placement is None:
        # Right after a restart exo may not have every Mac's memory yet; try
        # again on the next pass, but say so in the log only once.
        if not tracker.reported_no_fit:
            log.warning("cannot reload %s yet: it does not fit the free memory exo reports",
                        current.model, extra={"event": "cluster_autoload_no_fit", "model": current.model})
            tracker.reported_no_fit = True
        return "does not fit"
    try:
        client.create_instance(placement)
    except ClusterError as error:
        log.warning("reloading %s failed: %s", current.model, error,
                    extra={"event": "cluster_autoload_failed", "model": current.model})
        return "request failed"
    tracker.requested_at = now
    log.info("reloading %s (%s) after exo restarted", current.model, placement.sharding,
             extra={"event": "cluster_autoload", "model": current.model, "sharding": placement.sharding})
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
