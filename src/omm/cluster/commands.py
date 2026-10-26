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
from typing import Callable

from omm.cluster import appfw, exo_client, exo_service, firewall, netconfig, power, preflight, recommend, service, settings, tokens
from omm.cluster.errors import ClusterError
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


class UsageProblem(ClusterError):
    """A request that can never succeed as typed (exit 2)."""


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
    out(f"exo is running on this Mac (cluster {namespace}).")
    rdma = preflight.rdma_status()
    if rdma == "enabled":
        out("RDMA over Thunderbolt: enabled.")
    else:
        out(f"RDMA over Thunderbolt: {rdma}. To enable it on this Mac:")
        for line in preflight.RDMA_GUIDE:
            out(f"  {line}")


# exo's binary is fresh from the internet; macOS can hold its first start on a
# prompt that only shows on the Mac's own screen (real Mac mini, 2026-10-23).
SCREEN_APPROVAL_HINT = (
    "The first time exo starts, macOS may show an Allow prompt on this Mac's screen. "
    "If omm cluster status says exo is not running, click Allow there."
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
    # The model to serve survives a re-run, which restarts exo and so drops it.
    saved = settings.ClusterSettings("head", namespace, GATEWAY_PORT, model=current.model if current else None)
    settings.save(saved)
    _prepare_this_mac(namespace, out)
    firewall.apply(gateway_port=GATEWAY_PORT)
    power.keep_awake()
    # Before the gateway (re)starts, so its first connection is not held.
    appfw.allow([appfw.exo_binary(), appfw.gateway_binary()])
    service.install()
    out(SCREEN_APPROVAL_HINT)
    # On a line of its own: the 128-bit name makes the sentence wrap in an
    # 80-column terminal, and a wrapped command does not paste.
    out("Run this on every other Mac:")
    out(f"  omm cluster join {namespace}")
    out("Then give each employee a token: omm cluster token add <name>")
    return saved


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
    firewall.apply(gateway_port=None)
    power.keep_awake()
    appfw.allow([appfw.exo_binary()])
    out(SCREEN_APPROVAL_HINT)
    out("This Mac joined the cluster. Check it from the head Mac with: omm cluster status")
    return saved


def status() -> dict:
    require_macos()
    current = settings.load()
    problems: list[str] = []
    exo = {"reachable": False, "nodes": [], "instances": [], "links": []}
    try:
        state = ExoClient().state()
    except ClusterError as error:
        problems.append(str(error))
        state = None
    if state is not None:
        exo = {
            "reachable": True,
            "nodes": [{"name": n.name, "memory_total_bytes": n.memory_total_bytes,
                       "memory_available_bytes": n.memory_available_bytes, "rdma": n.rdma,
                       "os_version": n.os_version, "os_build": n.os_build} for n in state.nodes],
            "instances": [{"model_id": i.model_id, "sharding": i.sharding, "node_count": len(i.node_ids)}
                          for i in state.instances],
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
                    problems.append(f"no Thunderbolt link between {first.name} and {second.name}")
        if len(state.nodes) > 1:
            problems += [f"Thunderbolt Bridge is on for {n.name}; run omm cluster init (head) or join on it again"
                         for n in state.nodes if n.thunderbolt_bridge]
        # exo computes these from Bridge-enabled nodes and their Thunderbolt
        # links; a 2-node "cycle" is just one cable seen from both ends.
        for cycle in state.bridge_cycles:
            if len(cycle) > 2:
                members = ", ".join(sorted(names.get(node_id, node_id) for node_id in cycle))
                problems.append(f"Thunderbolt Bridge loop between {members} (packet storm); unplug one of "
                                "those cables or run omm cluster init/join again on each of them")
        seen = [n.name for n in state.nodes]
        if current is not None:
            missing = [name for name in current.known_nodes if name not in seen]
            problems += [f"node {name} missing (seen before)" for name in missing]
            if missing:
                # After a cable is pulled, exo can stop seeing Macs that are still
                # connected until it is restarted (seen once on 3 Mac minis, 2026-10-24).
                problems.append("if those Macs are on, run omm cluster restart on each of them and on this Mac")
                again = "init" if current.role == "head" else f"join {current.namespace}"
                problems.append(f"if a Mac left the cluster for good, run omm cluster {again} again on this Mac to forget it")
        # RDMA needs the exact same build, not just the same version number
        # (a 26.5 beta and 26.5 share "26.5").
        versions = {n.name: f"{n.os_version} ({n.os_build})" if n.os_build else n.os_version
                    for n in state.nodes if n.os_version}
        if len(set(versions.values())) > 1:
            problems.append("macOS versions differ: " + ", ".join(f"{k}={v}" for k, v in sorted(versions.items())))
        problems += [f"RDMA disabled on {n.name}" for n in state.nodes if n.rdma == "disabled"]
        if current is not None:
            merged = tuple(dict.fromkeys([*current.known_nodes, *seen]))
            if merged != current.known_nodes:
                settings.save(dataclasses.replace(current, known_nodes=merged))
    fw = "missing"
    gateway = "not_head"
    if current is not None and settings.is_legacy(current.namespace):
        problems.append("the cluster name is too short to protect exo's network port; run omm cluster init "
                        "on the head Mac and omm cluster join <new name> on every other Mac")
    if current is not None:
        fw = firewall.status(gateway_port=current.gateway_port if current.role == "head" else None)
        if fw not in ("active", "unchecked"):
            problems.append(f"firewall rules are {fw}; run omm cluster {'init' if current.role == 'head' else 'join'} again")
        idle = power.current_idle_sleep_minutes()
        if idle:
            problems.append(f"this Mac sleeps after {idle} min idle; run omm cluster "
                            f"{'init' if current.role == 'head' else 'join'} again")
        if current.role == "head":
            problems += [f"the macOS firewall holds incoming connections to {path}; run omm cluster init again"
                         for path in appfw.not_allowed([appfw.gateway_binary()])]
            gateway = "running" if service.is_running() else "stopped"
            if gateway == "stopped":
                problems.append("gateway is not running")
    return {"role": current.role if current else None, "namespace": current.namespace if current else None,
            "exo": exo, "firewall": fw, "gateway": gateway,
            "kept_model": current.model if current is not None and current.role == "head" else None,
            "problems": problems}


def exposure() -> dict | None:
    """The cluster's row for `omm engine security`; None when this Mac is not in a cluster."""
    if platform.system() != "Darwin":
        return None
    current = settings.load()
    if current is None:
        return None
    from omm import engine_security

    head = current.role == "head"
    ports = [*firewall.EXO_PORTS, *([current.gateway_port] if head else [])]
    listeners = []
    for port in ports:
        rows, _error = engine_security._listener_rows(port)
        listeners += [{"address": row["address"], "port": port} for row in rows]
    fw = firewall.status(gateway_port=current.gateway_port if head else None)
    again = "init" if head else f"join {current.namespace}"
    if fw == "active":
        status = "gateway_only"
        reason = ("pf blocks exo's API and model ports from other devices; "
                  + (f"the gateway (port {current.gateway_port}) needs an employee token and " if head else "")
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
    require_macos()
    client = client or ExoClient()
    state = client.state()
    models = recommend.prefilter(client.models(), state)
    ids = [m.model_id for m in models]
    # A client per call: requests.Session (inside every ExoClient) is not
    # documented as thread-safe.
    pool = ThreadPoolExecutor(max_workers=PREVIEW_WORKERS)
    try:
        previews = dict(zip(ids, pool.map(lambda model_id: ExoClient().previews(model_id), ids)))
    finally:
        # After a failed preview, do not wait for the ones still queued.
        pool.shutdown(cancel_futures=True)
    return recommend.rank(models, previews, state)


def load_model(model_id: str, out: Out, client: ExoClient | None = None) -> str:
    require_macos()
    client = client or ExoClient()
    if any(i.model_id == model_id for i in client.state().instances):
        # exo would place a second full copy next to the first one.
        out(f"{model_id} is already loaded or loading on this cluster; waiting for it instead of starting another copy...")
    else:
        placement = recommend.choose_placement(client.previews(model_id))
        if placement is None:
            raise ClusterError(f"{model_id} does not fit in this cluster's free memory")
        client.create_instance(placement)
        out(f"Loading {model_id} ({placement.sharding}); the first load downloads the model...")
    last: dict = {"key": None, "line": "", "progress": None}

    def report(progress: exo_client.LoadProgress) -> None:
        last["progress"] = progress
        if progress.phase == "loading":
            line, key = f"Every Mac has {model_id}; loading it into memory...", ("loading",)
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
                f"restart on {failed}, then run omm cluster model {model_id} again.")
        detail = f" Last status: {last['line']}" if last["line"] else ""
        raise ClusterError(f"{model_id} made no progress for {MODEL_STALL_TIMEOUT // 60} minutes.{detail}")
    current = settings.load()
    if current is None:
        return f"Serving {model_id}"
    if current.role != "head":
        # Only the head runs the gateway; this Mac's own address serves nothing.
        return f"Serving {model_id}; employees reach it through the head Mac's gateway"
    if current.model != model_id:
        settings.save(dataclasses.replace(current, model=model_id))
    return f"Serving {model_id} at http://{lan_ipv4()}:{current.gateway_port}"


def unload_model(model_id: str, client: ExoClient | None = None) -> int:
    require_macos()
    client = client or ExoClient()
    matching = [i.instance_id for i in client.state().instances if i.model_id == model_id]
    for instance_id in matching:
        client.delete_instance(instance_id)
    current = settings.load()
    if current is not None and current.model == model_id:
        # Otherwise the head would load it again within a minute.
        settings.save(dataclasses.replace(current, model=None))
    return len(matching)


def require_head() -> None:
    require_macos()
    current = settings.load()
    if current is None or current.role != "head":
        raise UsageProblem("tokens are managed on the head Mac")


def leave(out: Out) -> None:
    require_macos()
    current = settings.load()
    steps = [firewall.remove, netconfig.restore, power.restore, appfw.remove]
    if current is None or current.role == "head":
        steps.append(service.uninstall)
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
