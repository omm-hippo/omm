"""The member side of the member link: a background loop on every member Mac.

Every POLL_SECONDS it finds the head among the peers exo can see, reports
this Mac's state, and then does at most one thing: take over as head when the
head hands over to it, follow the head's omm commit (then exit, so launchd
restarts it on the new code), or refresh the backup of the head's documents.
Every reply must carry the head's proof (member_auth.py) or it is ignored.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import time
from typing import NamedTuple

from omm.cluster import exo_client, member_auth, rag_backup, settings
from omm.cluster.errors import ClusterError
from omm.cluster.gateway import GATEWAY_PORT, MEMBER_POLL_ROUTE, MEMBER_SNAPSHOT_ROUTE

log = logging.getLogger("omm.cluster.member")

POLL_SECONDS = 60
_EXIT_STATES = frozenset({"updated", "switched", "promoted"})


class HeadReply(NamedTuple):
    node_id: str
    address: str
    epoch: int
    body: dict


def local_ipv4s(run=subprocess.run) -> list[str]:
    """This Mac's IPv4 addresses, Thunderbolt link-local included (exo lists those too)."""
    try:
        text = run(["/sbin/ifconfig"], capture_output=True, text=True, check=False, timeout=5).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet":
            try:
                address = ipaddress.IPv4Address(parts[1])
            except ValueError:
                continue
            if not address.is_loopback:
                found.append(str(address))
    return found


def own_node_id(raw_state: dict, local: list[str]) -> str | None:
    mine = set(local)
    matches = [node for node, addresses in exo_client.node_ipv4s(raw_state).items() if mine & set(addresses)]
    return matches[0] if len(matches) == 1 else None


def ordered_addresses(raw_state: dict, own_node: str | None) -> list[tuple[str, str]]:
    """Every peer's (node id, IPv4), Thunderbolt link-local first: those never cross the office network."""
    rows = [(node, address) for node, addresses in exo_client.node_ipv4s(raw_state).items()
            if node != own_node for address in addresses]
    return sorted(rows, key=lambda row: not row[1].startswith("169.254."))


def _blind_context() -> ssl.SSLContext:
    # The head's CA is not pinned (it changes on handover); the reply proof
    # covers the leaf certificate this connection presented instead.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def request(address: str, method: str, path: str, key: bytes, own_node: str, *, body: bytes | None = None,
            timeout: float = 5) -> tuple[int, bytes, dict]:
    """One member-route call; raises ClusterError unless the reply carries the head's proof."""
    host, _, port = address.rpartition(":")
    nonce = secrets.token_hex(16)
    headers = member_auth.sign_request(key, method, path, own_node, now=time.time(), nonce=nonce)
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPSConnection(host, int(port), timeout=timeout, context=_blind_context())
    try:
        conn.request(method, path, body=body, headers=headers)
        der = conn.sock.getpeercert(binary_form=True)
        response = conn.getresponse()
        data = response.read()
        reply_headers = {k: v for k, v in response.getheaders()}
    except (OSError, http.client.HTTPException, ssl.SSLError) as error:
        raise ClusterError(f"no gateway at {address}: {error}") from error
    finally:
        conn.close()
    if not der or not member_auth.verify_head(key, nonce, member_auth.leaf_sha256(der),
                                              hashlib.sha256(data).hexdigest(),
                                              reply_headers.get(member_auth.HEADER_HEAD_PROOF)):
        raise ClusterError(f"the reply from {address} was not proven by this cluster's head")
    return response.status, data, reply_headers


def find_head(raw_state: dict, key: bytes, own_node: str, *, report: dict) -> HeadReply | None:
    """Poll every peer address at once; the proven head with the highest epoch wins."""
    targets = [(node, f"{address}:{GATEWAY_PORT}") for node, address in ordered_addresses(raw_state, own_node)]
    if not targets:
        return None
    payload = json.dumps(report).encode()

    def ask(target):
        node, address = target
        try:
            status, data, _ = request(address, *MEMBER_POLL_ROUTE, key, own_node, body=payload)
            body = json.loads(data)
        except (ClusterError, ValueError):
            return None
        if status != 200 or not isinstance(body, dict) or body.get("head") is not True:
            return None
        epoch = body.get("epoch")
        return HeadReply(node, address, epoch if isinstance(epoch, int) else 0, body)

    with ThreadPoolExecutor(max_workers=min(len(targets), 16)) as pool:
        replies = [reply for reply in pool.map(ask, targets) if reply is not None]
    # One reply per head: an earlier (Thunderbolt) address beats a later one.
    best: dict[str, HeadReply] = {}
    for reply in replies:
        best.setdefault(reply.node_id, reply)
    return max(best.values(), key=lambda reply: reply.epoch, default=None)


def fetch_snapshot(address: str, key: bytes, own_node: str) -> None:
    """Download the head's document snapshot and keep it as this Mac's backup."""
    status, data, headers = request(address, *MEMBER_SNAPSHOT_ROUTE, key, own_node, timeout=120)
    if status != 200:
        raise ClusterError(f"the head did not send its documents (HTTP {status})")
    sha, marker = headers.get("X-Omm-Rag-Sha256"), headers.get("X-Omm-Rag-Marker")
    if not sha or not marker:
        raise ClusterError("the head's document snapshot came without its checksum")
    directory = rag_backup.backup_path().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".incoming.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        rag_backup.install_backup(Path(name), sha256=sha, marker=marker)
    finally:
        Path(name).unlink(missing_ok=True)


def report() -> dict:
    """What the head records about this Mac (members.py) on every poll."""
    from omm.cluster import appfw, document_search, firewall, version_follow

    try:
        prepared = (firewall.status(gateway_port=GATEWAY_PORT) in ("active", "unchecked")
                    and not appfw.not_allowed([appfw.gateway_binary()]))
    except ClusterError:
        prepared = False
    embedding = rag_backup.backup_embedding_settings()
    return {
        "version": version_follow.local_version(),
        "version_state": _last["version_state"],
        "version_error": _last["version_error"],
        "rag_marker": rag_backup.backup_marker(),
        "prepared": prepared,
        "documents_ready": True if embedding is None else document_search.ready_for(embedding["model"],
                                                                                     embedding["base_url"]),
    }


# The outcome of the last version check, reported on the next poll.
_last = {"version_state": None, "version_error": None}


def _raw_state() -> dict:
    return exo_client.ExoClient().raw_state(timeout=5)


def run_once(*, first: bool = False) -> str:
    current = settings.load()
    if current is None or current.role != "member":
        return "not_member"
    from omm.cluster import handover, version_follow

    handover.finish_step_down()
    if first:
        # A changed omm may run exo differently; an unchanged plist leaves exo running.
        from omm.cluster import exo_service

        exo_service.install(current.namespace, keep_if_same=True)
    raw = _raw_state()
    own = own_node_id(raw, local_ipv4s())
    if own is None:
        log.warning("could not tell which exo node is this Mac", extra={"event": "cluster_member_no_node"})
        return "no_node"
    key = member_auth.member_key(current.namespace)
    head = find_head(raw, key, own, report=report())
    if head is None:
        return "no_head"
    instruction = head.body.get("handover")
    if isinstance(instruction, dict) and instruction.get("to") == own:
        handover.promote(head, own)
        return "promoted"
    state, error = version_follow.apply(head.body.get("version") or {})
    _last.update(version_state=state, version_error=error)
    if state in _EXIT_STATES:
        return state
    marker = head.body.get("rag_marker")
    if marker and marker != rag_backup.backup_marker():
        fetch_snapshot(head.address, key, own)
    return state


def run_member_link() -> int:
    first = True
    while True:
        try:
            state = run_once(first=first)
            first = False
        except Exception:
            log.warning("member link cycle failed", exc_info=True, extra={"event": "cluster_member_failed"})
            state = "failed"
        log.info("member link: %s", state, extra={"event": "cluster_member_cycle", "state": state})
        if state in _EXIT_STATES:
            # launchd (KeepAlive) starts this again on the new code or as the head's gateway.
            return 0
        time.sleep(POLL_SECONDS)
