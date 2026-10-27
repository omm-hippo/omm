"""Moving the head role to another Mac already in the cluster (`omm cluster handover`).

The old head records an instruction for the target (members.py); the
target's member link picks it up, pulls one last document snapshot, and
starts its own gateway with a higher head_epoch. Once the target answers as
head, the old head steps down. Employee tokens and the gateway's TLS CA are
never carried: the admin issues new tokens on the new head (a user decision).

A head whose gateway sees a peer head with a higher epoch steps down too, so
an old head that comes back after a handover does not keep serving.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
import time

from omm import config
from omm.cluster import (document_search, member_auth, member_link, member_service, members, rag_backup, service,
                         settings, tokens)
from omm.cluster.errors import ClusterError

log = logging.getLogger("omm.cluster.handover")

WAIT_SECONDS = 180
STALE_CHECK_SECONDS = 300
# A ready target polled within this long; it polls every member_link.POLL_SECONDS.
FRESH_SECONDS = 120


def stepped_down_path() -> Path:
    return config.OMM_HOME / "cluster" / "stepped-down.json"


def _state():
    from omm.cluster import exo_client

    raw = member_link._raw_state()
    return raw, exo_client.parse_state(raw)


def _own_node(raw: dict) -> str | None:
    return member_link.own_node_id(raw, member_link.local_ipv4s())


def preflight(target_name: str, *, now: datetime) -> tuple[str | None, list[str]]:
    """(target node id, problems); any problem stops the handover before anything changes."""
    from omm.cluster import version_follow

    current = settings.load()
    if current is None or current.role != "head":
        return None, ["this Mac is not the cluster head"]
    raw, state = _state()
    node = next((n for n in state.nodes if n.name == target_name), None)
    if node is None:
        return None, [f"no Mac named {target_name} in the cluster; omm cluster status lists them"]
    if node.node_id == _own_node(raw):
        return None, [f"{target_name} is already the head"]
    record = members.load().get(node.node_id)
    try:
        seen = datetime.fromisoformat(record["seen_at"]) if record else None
    except (KeyError, TypeError, ValueError):
        seen = None
    if seen is None or (now - seen).total_seconds() > FRESH_SECONDS:
        return node.node_id, [f"{target_name} has not checked in in the last 2 minutes; check it with omm cluster "
                              f"status, or run omm cluster join {current.namespace} again on it"]
    problems = []
    theirs = (record.get("version") or {}).get("commit")
    ours = version_follow.local_version().get("commit")
    if not ours or theirs != ours:
        problems.append(f"{target_name} runs omm {(theirs or '?')[:7]} but this Mac runs {(ours or '?')[:7]}; "
                        "wait until omm cluster status shows the same version")
    if not record.get("prepared"):
        problems.append(f"run omm cluster join {current.namespace} again on {target_name} first")
    report, _ = document_search.check()
    if report["in_use"] and not record.get("documents_ready"):
        problems.append(f"{target_name} cannot answer document questions yet: install Ollama and run "
                        f"ollama pull {report['model']} on it")
    return node.node_id, problems


def start(node_id: str, *, now: datetime) -> int:
    current = settings.load()
    epoch = current.head_epoch + 1
    carried = {"models": [dataclasses.asdict(entry) for entry in current.models],
               "known_nodes": list(current.known_nodes), "gateway_port": current.gateway_port}
    members.start_handover(node_id, epoch, carried, rag_backup.store_marker(), now=now)
    return epoch


def wait_for_new_head(node_id: str, epoch: int, *, deadline: float, sleep=time.sleep) -> bool:
    """True once the target answers, with the head's proof, as head at `epoch`."""
    from omm.cluster.gateway import GATEWAY_PORT, MEMBER_POLL_ROUTE

    current = settings.load()
    key = member_auth.member_key(current.namespace)
    while True:
        try:
            raw = member_link._raw_state()
            own = _own_node(raw) or "head"
            for node, address in member_link.ordered_addresses(raw, own):
                if node != node_id:
                    continue
                try:
                    status, data, _ = member_link.request(f"{address}:{GATEWAY_PORT}", *MEMBER_POLL_ROUTE, key, own,
                                                          body=b"{}")
                    body = json.loads(data)
                except (ClusterError, ValueError):
                    continue
                if status == 200 and body.get("head") is True and body.get("epoch") == epoch:
                    return True
                break
        except ClusterError:
            pass
        if time.monotonic() >= deadline:
            return False
        sleep(3)


def step_down(new_epoch: int) -> list[str]:
    """Make this head a member; returns the employee token names it had, to issue again on the new head."""
    from omm.atomic import atomic_write_text

    names = [record.name for record in tokens.list_tokens()]
    current = settings.load()
    settings.save(dataclasses.replace(current, role="member", head_epoch=new_epoch, models=()))
    # The member link moves the documents into the backup once the gateway is gone.
    atomic_write_text(stepped_down_path(), json.dumps({"epoch": new_epoch,
                                                       "at": datetime.now(timezone.utc).isoformat()}))
    members.clear_handover()
    members.clear()
    tokens.tokens_path().unlink(missing_ok=True)
    member_service.install()
    # Last: on a stale head this runs inside the gateway, and ends it.
    service.uninstall()
    return names


def finish_step_down() -> None:
    marker = stepped_down_path()
    if not marker.exists():
        return
    rag_backup.demote_live_to_backup()
    marker.unlink(missing_ok=True)


def promote(reply: member_link.HeadReply, own_node: str) -> None:
    """On the target: take the head's settings and documents and start the gateway."""
    instruction = reply.body["handover"]
    current = settings.load()
    key = member_auth.member_key(current.namespace)
    expected = instruction.get("rag_marker")
    if expected:
        member_link.fetch_snapshot(reply.address, key, own_node)
        if rag_backup.backup_marker() != expected:
            raise ClusterError("the head's documents changed during the handover; run omm cluster handover again")
        aside = rag_backup.adopt_backup_as_live(datetime.now())
        if aside is not None:
            log.warning("kept this Mac's own documents at %s", aside, extra={"event": "cluster_handover_aside"})
    carried = instruction.get("settings") or {}
    kept = tuple(settings.KeptModel(row["model"], row.get("sharding"), row.get("macs"))
                 for row in carried.get("models") or [] if isinstance(row, dict) and row.get("model"))
    settings.save(settings.ClusterSettings(
        "head", current.namespace, carried.get("gateway_port") or current.gateway_port,
        tuple(carried.get("known_nodes") or ()), kept, head_epoch=instruction["epoch"]))
    service.install()
    # Last: this runs inside the member link, and ends it.
    member_service.uninstall()


def check_for_newer_head() -> bool:
    """On a head: step down when a peer head has a higher epoch. True when it did."""
    current = settings.load()
    if current is None or current.role != "head":
        return False
    raw = member_link._raw_state()
    own = _own_node(raw)
    if own is None:
        return False
    found = member_link.find_head(raw, member_auth.member_key(current.namespace), own, report={})
    if found is None or found.epoch <= current.head_epoch:
        return False
    log.warning("another Mac is the head (epoch %s > %s); stepping down", found.epoch, current.head_epoch,
                extra={"event": "cluster_stale_head"})
    step_down(found.epoch)
    return True


def stale_head_loop(stop: threading.Event) -> None:
    """The gateway's background check: at start, then every STALE_CHECK_SECONDS."""
    while not stop.is_set():
        try:
            if check_for_newer_head():
                return
        except Exception:
            log.warning("could not check for a newer head", exc_info=True, extra={"event": "cluster_stale_check"})
        stop.wait(STALE_CHECK_SECONDS)


def lan_address(node_id: str) -> str | None:
    """The new head's office-network IPv4, for the employees' `omm rag remote set`."""
    from omm.cluster import exo_client

    for address in exo_client.node_ipv4s(member_link._raw_state()).get(node_id, []):
        if not address.startswith("169.254."):
            return address
    return None
