"""What `omm cluster ...` does, kept out of the cli.py monolith.

Each function takes an `out` callback for progress lines and raises
ClusterError (exit 1) or UsageProblem (exit 2); cli.py only maps those to
exit codes and renders output.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import dataclasses
import platform
import socket
import time
from typing import Callable

from datetime import datetime, timezone

from omm.cluster import (appfw, document_search, exo_client, exo_service, firewall, member_service, members, netconfig,
                         power, preflight, rag_backup, recommend, service, settings, tls, tokens)
from omm.cluster import problems as problems_mod
from omm.cluster.errors import ClusterError, UsageProblem
from omm.cluster.problems import Problem
from omm.cluster.exo_client import ExoClient
from omm.cluster.gateway import GATEWAY_PORT

Out = Callable[[str], None]

NOT_MACOS = ("omm cluster runs on the Macs that form the cluster (macOS 26.2 or later). "
             "Employees connect from any OS through the gateway (#502).")
# A first load downloads the whole model on every Mac (about 85 minutes for a
# 130 GB model on a 25 MB/s link), so loading is judged by progress, not by a
# deadline: give up only when nothing has moved for this long.
MODEL_STALL_TIMEOUT = 900
# Each preview can take up to a minute; a handful at once keeps `omm cluster
# model` short without flooding exo's loopback API.
PREVIEW_WORKERS = 8
_GIB = 1024**3


def require_macos() -> None:
    if platform.system() != "Darwin":
        raise UsageProblem(NOT_MACOS)


def lan_ipv4() -> str:
    """The address employees' PCs use to reach this Mac (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "<this Mac's IP address>"
    finally:
        probe.close()


def _prepare_this_mac(namespace: str, out: Out) -> None:
    preflight.require_supported_macos()
    if not preflight.exo_installed():
        preflight.install_exo(out)
    preflight.stage_runtime()
    out("omm will ask for this Mac's password to set up the Thunderbolt network, install the cluster "
        "firewall and turn off idle sleep.")
    # exo only looks for peers on addresses that exist when it starts.
    netconfig.apply()
    netconfig.wait_for_addresses(out)
    exo_service.install(namespace)
    if _exo_answers(out):
        out(f"exo is running on this Mac (cluster {namespace}).")
    else:
        out(f"exo has not answered for {EXO_START_SECONDS} seconds. {EXO_HELD_FIX}")
    rdma = preflight.rdma_status()
    if rdma == "enabled":
        out("RDMA over Thunderbolt: enabled.")
    else:
        out(f"RDMA over Thunderbolt: {rdma}. To enable it on this Mac:")
        for line in preflight.RDMA_GUIDE:
            out(f"  {line}")


# exo's binary is fresh from the internet; macOS can hold its first start on a
# prompt that only shows on the Mac's own screen (real Mac mini, 2026-10-23).
# The Local Network prompt does not stop exo; it only keeps it from finding the
# other Macs (node4 saw nobody for 30+ minutes, 2026-10-27).
SCREEN_APPROVAL_HINT = (
    "The first time exo starts, macOS may show two prompts on this Mac's screen: one to open exo "
    "and one to let it find devices on the local network. Click Allow (or Open) on both; "
    "omm cluster status tells you if one is still waiting."
)
# Gatekeeper holds a quarantined exo before its first instruction: launchd
# shows it running, but its API never answers (node1, 2026-10-27).
EXO_HELD_FIX = (
    "macOS may be holding exo until someone approves it on this Mac's screen: click Allow or Open there "
    "(or System Settings > Privacy & Security > Open Anyway), then run omm cluster restart."
)
LOCAL_NETWORK_FIX = "allow exo under System Settings > Privacy & Security > Local Network"
# How long init/join wait for exo's API after starting it. A normal start
# answers within seconds; only a Mac held on a screen prompt takes longer.
EXO_START_SECONDS = 60
EXO_START_POLL = 2


def _exo_answers(out: Out, *, timeout: float = EXO_START_SECONDS, sleep=time.sleep, clock=time.monotonic) -> bool:
    deadline = clock() + timeout
    told = False
    while True:
        try:
            ExoClient().raw_state(timeout=EXO_START_POLL)
            return True
        except exo_client.ExoUnavailable:
            pass
        except ClusterError:
            # It answered, only with an error: it is not held.
            return True
        if clock() >= deadline:
            return False
        if not told:
            out("Waiting for exo to start...")
            told = True
        sleep(EXO_START_POLL)


# The MLX ring listens on a random port in this range, so pf closes all of it
# to the office network, which also closes it for any other server on the Mac.
HIGH_PORTS_HINT = (
    "The cluster firewall blocks TCP ports 49153-65535 from the office network, for every app "
    "on this Mac, not only exo. Keep other servers that employees use off this Mac."
)


def init(out: Out) -> settings.ClusterSettings:
    require_macos()
    current = settings.load()
    if current is not None and current.role == "member":
        raise UsageProblem("this Mac already joined a cluster as a member; run omm cluster leave first")
    reuse = current is not None and not settings.is_legacy(current.namespace)
    namespace = current.namespace if reuse else settings.new_namespace()
    # Saved before anything can be interrupted: the name is printed below and
    # may already be typed into `join` on other Macs, so a re-run must reuse it.
    # The remembered Macs start over: running init again is how a Mac that
    # left for good stops being reported missing (status says so).
    # The models to serve survive a re-run, which restarts exo and so drops them.
    # The epoch survives too: a reset one would tie with a head that took over meanwhile.
    saved = settings.ClusterSettings("head", namespace, GATEWAY_PORT, models=current.models if current else (),
                                     head_epoch=current.head_epoch if current else 0,
                                     employee_addresses=tuple(tls.lan_ipv4s()))
    settings.save(saved)
    _prepare_this_mac(namespace, out)
    firewall.apply(gateway_port=GATEWAY_PORT)
    power.keep_awake()
    # Before the gateway (re)starts, so its first connection is not held.
    appfw.allow([appfw.exo_binary(), appfw.gateway_binary()])
    # The CA is made once and kept (a re-run and leave keep it too), so
    # employees who already trust it never have to again.
    tls.ensure()
    service.install()
    out(SCREEN_APPROVAL_HINT)
    out(HIGH_PORTS_HINT)
    # On a line of its own: the 128-bit name makes the sentence wrap in an
    # 80-column terminal, and a wrapped command does not paste.
    out("Run this on every other Mac:")
    out(f"  omm cluster join {namespace}")
    out("Then give each employee a token: omm cluster token add <name>")
    for line in employee_setup_lines():
        out(line)
    out(DHCP_HINT)
    return saved


def gateway_url() -> str:
    return f"https://{lan_ipv4()}:{GATEWAY_PORT}"


def employee_setup_lines() -> list[str]:
    """How an employee's PC connects, and the fingerprint they must see when it does."""
    lines = ["Employees connect with:", f"  omm rag remote set {gateway_url()}"]
    fingerprint = tls.head_fingerprint()
    if fingerprint:
        lines += ["It shows the gateway certificate's fingerprint; it must be exactly:", f"  {fingerprint}"]
    return lines


def join(namespace: str, out: Out) -> settings.ClusterSettings:
    require_macos()
    if settings.is_legacy(namespace):
        raise UsageProblem("this cluster name is from an older omm and too short to protect the cluster; "
                           "run omm cluster init again on the head Mac and join with the new name")
    if not settings.NAMESPACE_RE.match(namespace):
        raise UsageProblem("the cluster name is omm- followed by 32 letters and digits; "
                           "copy it from omm cluster init on the head Mac")
    current = settings.load()
    if current is not None and current.role == "head":
        raise UsageProblem("this Mac is the cluster head; run omm cluster leave first")
    saved = settings.ClusterSettings("member", namespace, GATEWAY_PORT)
    settings.save(saved)
    _prepare_this_mac(namespace, out)
    # Opened like on the head, so `omm cluster handover` needs no sudo later;
    # nothing listens on the gateway port until this Mac becomes head.
    firewall.apply(gateway_port=GATEWAY_PORT)
    power.keep_awake()
    appfw.allow([appfw.exo_binary(), appfw.gateway_binary()])
    member_service.install()
    out(SCREEN_APPROVAL_HINT)
    out(HIGH_PORTS_HINT)
    out("This Mac joined the cluster. Check it from the head Mac with: omm cluster status")
    return saved


def _instance_load(raw: dict, model_id: str) -> dict:
    # exo lists an instance as soon as it is placed, long before it serves: a
    # first load downloads the whole model on every Mac (over an hour for a big
    # one), then loads it into memory.
    if exo_client.instance_ready(raw, model_id):
        return {"state": "ready", "downloaded_bytes": 0, "total_bytes": 0, "failed_on": None, "error": None}
    progress = exo_client.load_progress(raw, model_id)
    state = "failed" if progress.failed else progress.phase
    return {"state": state, "downloaded_bytes": progress.downloaded_bytes, "total_bytes": progress.total_bytes,
            "failed_on": progress.failed_on, "error": progress.failed}


def status() -> dict:
    require_macos()
    current = settings.load()
    problems: list[Problem] = []
    exo = {"reachable": False, "nodes": [], "instances": [], "links": []}
    try:
        raw = ExoClient().raw_state()
    except exo_client.ExoUnavailable as error:
        # launchd has it running but nothing listens: held before its first
        # instruction, or (for a few seconds) still starting.
        if current is not None and exo_service.is_running():
            problems.append(Problem(f"exo is running but does not answer. If that lasts more than a minute, "
                                    f"{EXO_HELD_FIX}", "manual", after="restart"))
        else:
            problems.append(Problem(str(error), "run" if current is not None else "manual",
                                    fix="restart" if current is not None else None))
        raw = None
    except ClusterError as error:
        problems.append(Problem(str(error)))
        raw = None
    state = None if raw is None else exo_client.parse_state(raw)
    own = None if raw is None else (_own_node(raw) or ExoClient().node_id())
    # Until exo has replayed the cluster's history its state is an old view
    # (no entry for this Mac, old ids and addresses): nothing in it is worth
    # acting on (Node2, 2026-10-29).
    behind = (state is not None and own is not None and current is not None and current.role == "member"
              and member_link_catching_up(raw, own))
    own_name = next((n.name for n in state.nodes if n.node_id == own), None) if state is not None else None
    member_rows: list[dict] = []
    if state is not None and current is not None and current.role == "head":
        member_rows, found = _member_rows(raw, state)
        problems += found
    if behind:
        index = raw.get("lastEventAppliedIdx")
        problems.append(Problem(f"exo on this Mac is still catching up on the cluster's history (event "
                                f"{index if isinstance(index, int) else '?'}); wait, nothing to fix. The head "
                                "shows how far it is.", "wait"))
    if state is not None and not behind:
        instances = []
        for i in state.instances:
            load = _instance_load(raw, i.model_id)
            instances.append({"model_id": i.model_id, "sharding": i.sharding, "node_count": len(i.node_ids), **load})
            if load["state"] == "failed":
                problems.append(Problem(
                    f"{load['failed_on']} could not download {i.model_id}; exo does not try a failed download "
                    f"again until it restarts. Fix the cause, run omm cluster restart on {load['failed_on']}, "
                    f"then run omm cluster load {i.model_id} again.", "tell", where=load["failed_on"]))
        exo = {
            "reachable": True,
            "nodes": [{"name": n.name, "memory_total_bytes": n.memory_total_bytes,
                       "memory_available_bytes": n.memory_available_bytes, "rdma": n.rdma,
                       "os_version": n.os_version, "os_build": n.os_build} for n in state.nodes],
            "instances": instances,
            "links": [],
        }
        names = {n.node_id: n.name for n in state.nodes}
        exo["links"] = [{"from": names.get(l.source_id, l.source_id), "to": names.get(l.sink_id, l.sink_id),
                         "kind": l.kind} for l in state.links]
        linked = {frozenset((l.source_id, l.sink_id)) for l in state.links}
        ordered = sorted(state.nodes, key=lambda n: n.name)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                if frozenset((first.node_id, second.node_id)) not in linked:
                    problems.append(Problem(f"no Thunderbolt link between {first.name} and {second.name}",
                                            "manual", after="restart"))
        if len(state.nodes) > 1:
            problems += [_on_mac(n.name, own_name, f"Thunderbolt Bridge is on for {n.name}; run omm cluster on it",
                                 fix="netconfig")
                         for n in state.nodes if n.thunderbolt_bridge]
        # exo computes these from Bridge-enabled nodes and their Thunderbolt
        # links; a 2-node "cycle" is just one cable seen from both ends.
        for cycle in state.bridge_cycles:
            if len(cycle) > 2:
                members = ", ".join(sorted(names.get(node_id, node_id) for node_id in cycle))
                problems.append(Problem(f"Thunderbolt Bridge loop between {members} (packet storm); unplug one of "
                                        "those cables or run omm cluster on each of them", "manual"))
        if current is not None and len(state.nodes) == 1:
            # A pending Local Network prompt leaves exo up but alone: no peer
            # is ever dialed and nothing else looks wrong (node4, 2026-10-27).
            if current.role == "member":
                problems.append(Problem(f"exo on this Mac sees no other Mac. On this Mac's screen, "
                                        f"{LOCAL_NETWORK_FIX}, then run omm cluster restart.", "manual",
                                        after="restart"))
            else:
                problems.append(Problem("exo on this Mac sees no other Mac. If other Macs already ran omm cluster "
                                        f"join: on each Mac's screen, this one included, {LOCAL_NETWORK_FIX}, then "
                                        "run omm cluster restart on that Mac.", "manual", after="restart"))
        seen = [n.name for n in state.nodes]
        if current is not None:
            missing = [name for name in current.known_nodes if name not in seen]
            problems += [Problem(f"node {name} missing (seen before)", "tell", where=name) for name in missing]
            if missing:
                # After a cable is pulled, exo can stop seeing Macs that are still
                # connected until it is restarted (seen once on 3 Mac minis, 2026-10-24).
                problems.append(Problem("if those Macs are on, run omm cluster restart on each of them and on "
                                        "this Mac", "tell"))
                again = "init" if current.role == "head" else f"join {current.namespace}"
                problems.append(Problem(f"if a Mac left the cluster for good, run omm cluster {again} again on "
                                        "this Mac to forget it", "tell"))
        problems += _macos_problems(state.nodes, own_name)
        problems += [_on_mac(n.name, own_name, f"RDMA disabled on {n.name}")
                     for n in state.nodes if n.rdma == "disabled"]
        if current is not None:
            merged = tuple(dict.fromkeys([*current.known_nodes, *seen]))
            if merged != current.known_nodes:
                settings.save(dataclasses.replace(current, known_nodes=merged))
    fw = "missing"
    gateway = "not_head"
    documents = None
    if current is not None and settings.is_legacy(current.namespace):
        problems.append(Problem("the cluster name is too short to protect exo's network port; run omm cluster "
                                "init on the head Mac and omm cluster join <new name> on every other Mac"))
    if current is not None:
        fw = firewall.status(gateway_port=current.gateway_port)
        if fw not in ("active", "unchecked"):
            problems.append(Problem(f"firewall rules are {fw}; {FIX_HERE}", "run", fix="firewall"))
        idle = power.current_idle_sleep_minutes()
        if idle:
            problems.append(Problem(f"this Mac sleeps after {idle} min idle; {FIX_HERE}", "run", fix="keep_awake"))
        problems += [Problem(f"the macOS firewall holds incoming connections to {path}; {FIX_HERE}", "run",
                             fix="appfw")
                     for path in appfw.not_allowed([appfw.gateway_binary()])]
        if current.role == "member" and not member_service.is_running():
            problems.append(Problem(f"the member link is not running; {FIX_HERE}", "run", fix="member_link"))
        if current.role == "head":
            gateway = "running" if service.is_running() else "stopped"
            if gateway == "stopped":
                problems.append(Problem(f"gateway is not running; {FIX_HERE}", "run", fix="gateway"))
            # The head answers employees' document questions (#534).
            documents, found = document_search.check()
            problems += [problems_mod.of(text) for text in found]
            problems += _address_problems()
    head = current is not None and current.role == "head"
    return {"role": current.role if current else None, "namespace": current.namespace if current else None,
            "exo": exo, "firewall": fw, "gateway": gateway,
            "gateway_url": gateway_url() if head else None,
            "gateway_ca_sha256": tls.head_fingerprint() if head else None,
            "kept_models": [dataclasses.asdict(entry) for entry in current.models] if head else [],
            "document_search": documents, "members": member_rows, "problems": problems,
            "catching_up": behind}


# What a problem omm can fix itself tells the user (the fix is narrow, never a whole init/join).
FIX_HERE = "run omm cluster on this Mac to fix it"


def member_link_catching_up(raw: dict, own: str) -> bool:
    from omm.cluster import member_link

    return member_link.catching_up(raw, own)


def _on_mac(name: str, own_name: str | None, text: str, *, fix: str | None = None) -> Problem:
    """A problem on the named Mac: this Mac's own (run or manual) or another Mac's (tell)."""
    if name == own_name:
        return Problem(text, "run" if fix else "manual", fix=fix)
    return Problem(text, "tell", where=name)


def _build_key(version: str, build: str | None) -> tuple:
    parts = tuple(int(p) if p.isdigit() else 0 for p in version.split("."))
    return parts, build or ""


def _macos_problems(nodes, own_name: str | None) -> list[Problem]:
    # RDMA needs the exact same build, not just the same version number
    # (a 26.5 beta and 26.5 share "26.5").
    versions = {n.name: (n.os_version, n.os_build) for n in nodes if n.os_version}
    if len({v for v in versions.values()}) <= 1:
        return []
    label = {name: f"{v} ({b})" if b else v for name, (v, b) in versions.items()}
    newest = max(versions.values(), key=lambda pair: _build_key(*pair))
    behind = sorted(name for name, pair in versions.items() if pair != newest)
    target = f"{newest[0]} ({newest[1]})" if newest[1] else newest[0]
    # A Mac cannot go back to an older macOS, so the others catch up.
    text = ("macOS versions differ: " + ", ".join(f"{k}={v}" for k, v in sorted(label.items()))
            + f". Update macOS to {target} on {', '.join(behind)}, one Mac at a time")
    return [Problem(text, "manual" if own_name in behind and len(behind) == 1 else "tell",
                    where=None if own_name in behind and len(behind) == 1 else ", ".join(behind))]


def _address_problems() -> list[Problem]:
    """The head's office-network address changed since employees were told it (DHCP, 2026-10-29)."""
    # Loaded again: status may have just saved newly seen Macs.
    current = settings.load()
    now = tuple(tls.lan_ipv4s())
    if current is None:
        return []
    if not now:
        return []
    if not current.employee_addresses:
        settings.save(dataclasses.replace(current, employee_addresses=now))
        return []
    if set(now) & set(current.employee_addresses):
        return []
    old = ", ".join(current.employee_addresses)
    return [Problem(f"the head's address changed from {old} to {now[0]}; on each employee PC run omm rag remote set "
                    f"https://{now[0]}:{GATEWAY_PORT} . {DHCP_HINT}", "tell", where="employee PCs",
                    fix="addresses_seen")]


# omm cannot reserve an address; the office router can (2026-10-29: 10.80.119.212 -> 10.80.117.117 overnight).
DHCP_HINT = ("The address comes from the office router (DHCP) and can change again; ask the network admin to "
             "reserve one for this Mac.")


def acknowledge_addresses() -> None:
    """The admin says every employee PC uses the current address."""
    current = settings.load()
    if current is not None:
        settings.save(dataclasses.replace(current, employee_addresses=tuple(tls.lan_ipv4s())))


# A member polls every minute; this long without a poll means its link is not running.
MEMBER_SILENT_SECONDS = 10 * 60
# A Mac exo only just found has not had its first poll yet (its link may have
# just failed to see the head), so re-join advice waits this long after status
# first sees it silent (real case: 3 Mac minis, 2026-10-27).
MEMBER_CHECKIN_GRACE_SECONDS = 3 * 60


def _own_node(raw: dict) -> str | None:
    from omm.cluster import member_link

    return member_link.own_node_id(raw, member_link.local_ipv4s())


def _catching_up_text(name: str, record: dict, head_index) -> str:
    index, rate = record.get("event_index"), record.get("event_rate")
    if not isinstance(index, int) or not isinstance(head_index, int) or head_index <= 0:
        return f"{name} is catching up on the cluster's history; wait, it checks in when done"
    text = f"{name} is catching up on the cluster's history: {index:,} of {head_index:,} events ({index * 100 // head_index}%)"
    if isinstance(rate, (int, float)) and rate > 0 and head_index > index:
        text += f", about {max(1, round((head_index - index) / rate / 60))} min left"
    return text + "; wait, nothing to fix"


def _member_rows(raw: dict, state) -> tuple[list[dict], list[Problem]]:
    """Each other Mac's omm version and backup, as its member link last reported them."""
    from omm.cluster import version_follow

    own = _own_node(raw)
    if own is None:
        # Without knowing which node is this Mac, every Mac would look silent.
        return [], []
    head_commit = version_follow.local_version().get("commit")
    marker = rag_backup.store_marker()
    records = members.load()
    now = datetime.now(timezone.utc)
    others = [node for node in state.nodes if node.node_id != own]
    ages = {}
    for node in others:
        record = records.get(node.node_id)
        try:
            ages[node.node_id] = (now - datetime.fromisoformat(record["seen_at"])).total_seconds() if record else None
        except (KeyError, TypeError, ValueError):
            ages[node.node_id] = None
    silent = members.silent_since([node_id for node_id, ago in ages.items()
                                   if ago is None or ago > MEMBER_SILENT_SECONDS], now=now)
    rows, problems = [], []
    head_index = raw.get("lastEventAppliedIdx")
    for node in others:
        record = records.get(node.node_id)
        ago = ages[node.node_id]
        if record is not None and record.get("catching_up") and ago is not None and ago <= MEMBER_SILENT_SECONDS:
            problems.append(Problem(_catching_up_text(node.name, record, head_index), "wait", where=node.name))
            continue
        if node.node_id in silent:
            if (now - silent[node.node_id]).total_seconds() < MEMBER_CHECKIN_GRACE_SECONDS:
                which = "first" if record is None else "next"
                problems.append(Problem(f"waiting for {node.name}'s {which} check-in (members check in every 60 s)",
                                        "wait", where=node.name))
            else:
                # Its own `omm cluster` says why: a stopped link, a screen
                # prompt, or an old omm that cannot report catching up.
                problems.append(Problem(f"{node.name} has not checked in; run omm cluster on it to see why",
                                        "tell", where=node.name))
            if record is None:
                continue
        version = record.get("version") if isinstance(record.get("version"), dict) else {}
        commit, state_name = version.get("commit"), record.get("version_state")
        backup = record.get("rag_marker")
        rows.append({"name": node.name, "version": commit[:7] if isinstance(commit, str) else None,
                     "version_state": state_name, "version_error": record.get("version_error"),
                     "backup": "none" if not backup else "current" if backup == marker else "behind",
                     "prepared": bool(record.get("prepared")), "seen_seconds_ago": None if ago is None else int(ago)})
        if state_name == "ahead":
            problems.append(Problem(f"{node.name} is newer than the head; run omm update on this Mac", "run",
                                    fix="update"))
        elif state_name == "blocked":
            problems.append(Problem(f"{node.name} could not update: {record.get('version_error') or 'unknown error'}",
                                    "tell", where=node.name))
        elif state_name == "manual" or not version.get("git"):
            problems.append(Problem(f"{node.name} is not a git install; update omm on it by hand", "tell",
                                    where=node.name))
        elif head_commit and commit != head_commit:
            problems.append(Problem(f"{node.name} runs omm {(commit or '?')[:7]} but the head runs {head_commit[:7]}; "
                                    "it updates itself within a few minutes", "wait", where=node.name))
    return rows, problems


def exposure() -> dict | None:
    """The cluster's row for `omm engine security`; None when this Mac is not in a cluster."""
    if platform.system() != "Darwin":
        return None
    current = settings.load()
    if current is None:
        return None
    from omm import engine_security

    head = current.role == "head"
    # Open on members too (so a handover needs no sudo); nothing listens there.
    ports = [*firewall.EXO_PORTS, current.gateway_port]
    listeners = []
    for port in ports:
        rows, _error = engine_security._listener_rows(port)
        listeners += [{"address": row["address"], "port": port} for row in rows]
    fw = firewall.status(gateway_port=current.gateway_port)
    again = "init" if head else f"join {current.namespace}"
    if fw == "active":
        status = "gateway_only"
        reason = ("pf blocks exo's API and model ports from other devices; "
                  + (f"the gateway (port {current.gateway_port}) needs an employee token and " if head else
                     f"the gateway port {current.gateway_port} is closed until this Mac becomes head, and ")
                  + f"exo's peer port {firewall.LIBP2P_PORT} needs the cluster key")
    elif fw == "unchecked":
        status = "unknown"
        reason = "the cluster firewall rules file is in place, but reading what pf loaded needs sudo; run sudo -v, then this again"
    elif not listeners:
        status = "not_running"
        reason = f"exo is not listening and the cluster firewall rules are {fw}; run omm cluster {again} again"
    else:
        status = "external_allowed"
        reason = f"the cluster firewall rules are {fw}, so other devices can reach exo directly; run omm cluster {again} again"
    return {"engine": "cluster", "status": status, "reason": reason, "port": None,
            "listeners": listeners, "owned_by_omm": True}


def restart(out: Out) -> None:
    require_macos()
    if settings.load() is None:
        raise UsageProblem("this Mac is not in a cluster; run omm cluster init or join first")
    exo_service.restart()
    out("exo restarted on this Mac; it finds the other Macs again in about 15 seconds")
    out("Check with: omm cluster status")


def candidates(client: ExoClient | None = None) -> list[recommend.Candidate]:
    # The gateway passes the client for its own exo; only a direct call needs this Mac to be one.
    if client is None:
        require_macos()
        client = ExoClient()
    state = client.state()
    models = recommend.prefilter(client.models(), state)
    ids = [m.model_id for m in models]
    base_url = getattr(client, "base_url", None)
    # A client per call: requests.Session (inside every ExoClient) is not
    # documented as thread-safe.
    pool = ThreadPoolExecutor(max_workers=PREVIEW_WORKERS)
    try:
        previews = dict(zip(ids, pool.map(
            lambda model_id: (ExoClient(base_url) if base_url else ExoClient()).previews(model_id), ids)))
    finally:
        # After a failed preview, do not wait for the ones still queued.
        pool.shutdown(cancel_futures=True)
    return recommend.rank(models, previews, state)


def load_model(model_id: str, out: Out, client: ExoClient | None = None, placement=None, *,
               sharding: str | None = None, macs: int | None = None) -> str:
    """Load `model_id` (on `placement`, else omm's pick) and remember it with the admin's choice."""
    require_macos()
    client = client or ExoClient()
    raw = client.raw_state()
    state = exo_client.parse_state(raw)
    created = False
    if any(i.model_id == model_id for i in state.instances):
        # exo would place a second full copy next to the first one.
        out(f"{model_id} is already loaded or loading on this cluster; waiting for it instead of starting another copy...")
    else:
        placement = placement or recommend.choose_placement(client.previews(model_id),
                                                            sharding=sharding, macs=macs)
        if placement is None:
            raise ClusterError(f"{model_id} does not fit in this cluster's free memory")
        client.create_instance(placement)
        created = True
        installed = {row.node_id for row in exo_client.parse_downloads(raw)
                     if row.model_id == model_id and row.kind == "Completed"}
        # Only the Macs exo placed it on fetch anything (Tensor on 2 of 3 Macs, 2026-10-27).
        used = set(placement.memory_delta_by_node) or {node.node_id for node in state.nodes}
        if used <= installed:
            out(f"Loading {model_id} ({placement.sharding})...")
        else:
            out(f"Loading {model_id} ({placement.sharding}); Macs without it download it first...")
    last: dict = {"key": None, "line": "", "progress": None}

    def report(progress: exo_client.LoadProgress) -> None:
        last["progress"] = progress
        if progress.phase == "loading":
            line, key = f"{model_id} is downloaded; loading it into memory...", ("loading",)
        elif progress.total_bytes:
            percent = progress.downloaded_bytes * 100 // progress.total_bytes
            line = (f"Downloading {model_id}: {percent}% ({progress.downloaded_bytes / _GIB:.1f} of "
                    f"{progress.total_bytes / _GIB:.1f} GiB on the slowest Mac)")
            key = ("downloading", percent // 5)
        else:
            line, key = f"Waiting for exo to start downloading {model_id}...", ("downloading", None)
        if progress.failed:
            line = f"{progress.failed_on or 'A Mac'} could not download {model_id}: {progress.failed}"
            key = (*key, progress.failed)
        if key != last["key"]:
            last["key"], last["line"] = key, line
            out(line)

    if not client.await_instance(model_id, MODEL_STALL_TIMEOUT, on_progress=report):
        failed = last["progress"].failed_on if last["progress"] and last["progress"].failed else None
        if failed:
            # Left in place, the broken instance would make the next run wait on it.
            unload_model(model_id, client)
            raise ClusterError(
                f"exo does not try a failed download again until it restarts. Fix the cause, run omm cluster "
                f"restart on {failed}, then run omm cluster load {model_id} again.")
        detail = f" Last status: {last['line']}" if last["line"] else ""
        raise ClusterError(f"{model_id} made no progress for {MODEL_STALL_TIMEOUT // 60} minutes.{detail}")
    current = settings.load()
    if current is None:
        return f"Serving {model_id}"
    if current.role != "head":
        # Only the head runs the gateway; this Mac's own address serves nothing.
        return f"Serving {model_id}; employees reach it through the head Mac's gateway"
    entry = settings.KeptModel(model_id, sharding, macs)
    # A plain load of a model already serving keeps the choice it was loaded with.
    if current.kept(model_id) != entry and (created or sharding or macs or not current.kept(model_id)):
        settings.save(current.keeping(entry))
    return f"Serving {model_id} at https://{lan_ipv4()}:{current.gateway_port}"


def unload_model(model_id: str, client: ExoClient | None = None) -> int:
    require_macos()
    client = client or ExoClient()
    matching = [i.instance_id for i in client.state().instances if i.model_id == model_id]
    for instance_id in matching:
        client.delete_instance(instance_id)
    current = settings.load()
    if current is not None and current.kept(model_id) is not None:
        # Otherwise the head would load it again within a minute.
        settings.save(current.forgetting(model_id))
    return len(matching)


def require_head() -> None:
    require_macos()
    current = settings.load()
    if current is None or current.role != "head":
        raise UsageProblem("tokens are managed on the head Mac")


def _has_remote() -> bool:
    """An employee PC: `omm rag remote set` saved the head's address and token."""
    from omm import rag_remote

    try:
        return rag_remote.load() is not None
    except rag_remote.RemoteError:
        return False


def require_head_for(action: str) -> None:
    # The head keeps the model it reloads after a restart (cluster.json
    # `model`); a change made on a member would be undone by that reload.
    current = settings.load()
    if current is None and _has_remote():
        raise UsageProblem(f"{action} runs on the cluster's head Mac; ask its admin")
    require_macos()
    if current is None:
        raise UsageProblem("this Mac is not in a cluster; run omm cluster init or join first")
    if current.role != "head":
        head = head_name()
        where = f" ({head})" if head else ""
        raise UsageProblem(f"{action} runs on the head Mac{where}; this Mac is a member of the cluster")


# Per address, all probed at once: Thunderbolt and office-network addresses answer in milliseconds.
HEAD_PROBE_TIMEOUT = 1.5


def _is_gateway(address: str, port: int) -> bool:
    """True when an omm cluster gateway answers there (it refuses a request without a token)."""
    import ssl
    from urllib import error as urlerror, request as urlrequest

    # Only asks who serves there and sends nothing secret, so the
    # certificate is not checked (a member Mac has not pinned the head's CA).
    blind = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    blind.check_hostname = False
    blind.verify_mode = ssl.CERT_NONE
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), urlrequest.HTTPSHandler(context=blind))
    try:
        opener.open(f"https://{address}:{port}/omm/status", timeout=HEAD_PROBE_TIMEOUT).close()
    except urlerror.HTTPError as error:
        try:
            return error.code == 401 and b"omm cluster:" in error.read()
        except OSError:
            return False
    except (urlerror.URLError, OSError, ValueError):
        return False
    return False


def head_name(client: ExoClient | None = None) -> str | None:
    """The name exo shows for the Mac running the gateway, for a member's messages; None if unknown."""
    try:
        raw = (client or ExoClient()).raw_state(timeout=3)
    except ClusterError:
        return None
    names = {node.node_id: node.name for node in exo_client.parse_state(raw).nodes}
    probes = [(node_id, address) for node_id, addresses in exo_client.node_ipv4s(raw).items()
              if node_id in names for address in addresses]
    if not probes:
        return None
    with ThreadPoolExecutor(max_workers=min(len(probes), 16)) as pool:
        answers = list(pool.map(lambda probe: _is_gateway(probe[1], GATEWAY_PORT), probes))
    return next((names[node_id] for (node_id, _), found in zip(probes, answers) if found), None)


def leave(out: Out) -> None:
    require_macos()
    current = settings.load()
    # Both agents on either role: a former head's gateway may still be registered.
    steps = [firewall.remove, netconfig.restore, power.restore, appfw.remove, service.uninstall,
             member_service.uninstall, rag_backup.delete_backup, rag_backup.delete_snapshot]
    if current is None or current.role == "head":
        steps += [members.clear, members.clear_handover]
    steps += [exo_service.uninstall, preflight.remove_runtime]
    # Every step is safe to repeat, so one failure (usually a declined sudo
    # prompt) must not leave the rest of this Mac half dismantled.
    failures = []
    for step in steps:
        try:
            step()
        except ClusterError as error:
            failures.append(str(error))
    if failures:
        # Settings stay so the re-run still knows whether to remove the gateway.
        raise ClusterError("This Mac did not fully leave the cluster:\n"
                           + "".join(f"  {failure}\n" for failure in failures)
                           + "Everything else was undone. Run omm cluster leave again to finish.")
    settings.clear()
    out("This Mac left the cluster. Employee tokens were kept.")
    out("exo itself stays installed; remove it from its menu bar icon -> Advanced -> Uninstall")


__all__ = ["UsageProblem", "ClusterError", "tokens", "init", "join", "status", "restart", "candidates",
           "load_model", "unload_model", "require_head", "leave", "lan_ipv4", "ExoClient"]
