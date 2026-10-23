"""What `omm cluster ...` does, kept out of the cli.py monolith.

Each function takes an `out` callback for progress lines and raises
ClusterError (exit 1) or UsageProblem (exit 2); cli.py only maps those to
exit codes and renders output.
"""

from __future__ import annotations

import platform
import socket
from typing import Callable

from omm.cluster import appfw, exo_service, firewall, power, preflight, recommend, service, settings, tokens
from omm.cluster.errors import ClusterError
from omm.cluster.exo_client import ExoClient
from omm.cluster.gateway import GATEWAY_PORT

Out = Callable[[str], None]

NOT_MACOS = ("omm cluster runs on the Macs that form the cluster (macOS 26.2 or later). "
             "Employees connect from any OS through the gateway (#502).")
MODEL_READY_TIMEOUT = 3600


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
    namespace = current.namespace if current else settings.new_namespace()
    # Saved before anything can be interrupted: the name is printed below and
    # may already be typed into `join` on other Macs, so a re-run must reuse it.
    saved = settings.ClusterSettings("head", namespace, GATEWAY_PORT, current.known_nodes if current else ())
    settings.save(saved)
    _prepare_this_mac(namespace, out)
    out("omm will ask for this Mac's password to install the cluster firewall and turn off idle sleep.")
    firewall.apply(gateway_port=GATEWAY_PORT)
    power.keep_awake()
    # Before the gateway (re)starts, so its first connection is not held.
    appfw.allow([appfw.exo_binary(), appfw.gateway_binary()])
    service.install()
    out(SCREEN_APPROVAL_HINT)
    out(f"Run this on every other Mac: omm cluster join {namespace}")
    out("Then give each employee a token: omm cluster token add <name>")
    return saved


def join(namespace: str, out: Out) -> settings.ClusterSettings:
    require_macos()
    if not settings.NAMESPACE_RE.match(namespace):
        raise UsageProblem("the cluster name looks like omm-1a2b3c4d; copy it from omm cluster init on the head Mac")
    current = settings.load()
    if current is not None and current.role == "head":
        raise UsageProblem("this Mac is the cluster head; run omm cluster leave first")
    saved = settings.ClusterSettings("member", namespace, GATEWAY_PORT, current.known_nodes if current else ())
    settings.save(saved)
    _prepare_this_mac(namespace, out)
    out("omm will ask for this Mac's password to install the cluster firewall and turn off idle sleep.")
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
                       "os_version": n.os_version} for n in state.nodes],
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
            problems += [f"Thunderbolt Bridge is on for {n.name}; turn it off for an RDMA cluster"
                         for n in state.nodes if n.thunderbolt_bridge]
        seen = [n.name for n in state.nodes]
        if current is not None:
            for name in current.known_nodes:
                if name not in seen:
                    problems.append(f"node {name} missing (seen before)")
        versions = {n.name: n.os_version for n in state.nodes if n.os_version}
        if len(set(versions.values())) > 1:
            problems.append("macOS versions differ: " + ", ".join(f"{k}={v}" for k, v in sorted(versions.items())))
        problems += [f"RDMA disabled on {n.name}" for n in state.nodes if n.rdma == "disabled"]
        if current is not None:
            merged = tuple(dict.fromkeys([*current.known_nodes, *seen]))
            if merged != current.known_nodes:
                settings.save(settings.ClusterSettings(current.role, current.namespace, current.gateway_port, merged))
    fw = "missing"
    gateway = "not_head"
    if current is not None:
        fw = firewall.status(gateway_port=current.gateway_port if current.role == "head" else None)
        if fw != "active":
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
            "exo": exo, "firewall": fw, "gateway": gateway, "problems": problems}


def candidates(client: ExoClient | None = None) -> list[recommend.Candidate]:
    require_macos()
    client = client or ExoClient()
    state = client.state()
    models = recommend.prefilter(client.models(), state)
    previews = {m.model_id: client.previews(m.model_id) for m in models}
    return recommend.rank(models, previews, state)


def load_model(model_id: str, out: Out, client: ExoClient | None = None) -> str:
    require_macos()
    client = client or ExoClient()
    placement = recommend.choose_placement(client.previews(model_id))
    if placement is None:
        raise ClusterError(f"{model_id} does not fit in this cluster's free memory")
    client.create_instance(placement)
    out(f"Loading {model_id} ({placement.sharding}); the first load downloads the model...")
    if not client.await_instance(model_id, MODEL_READY_TIMEOUT):
        raise ClusterError(f"{model_id} did not become ready within {MODEL_READY_TIMEOUT // 60} minutes")
    current = settings.load()
    port = current.gateway_port if current else GATEWAY_PORT
    return f"Serving {model_id} at http://{lan_ipv4()}:{port}"


def unload_model(model_id: str, client: ExoClient | None = None) -> int:
    require_macos()
    client = client or ExoClient()
    matching = [i.instance_id for i in client.state().instances if i.model_id == model_id]
    for instance_id in matching:
        client.delete_instance(instance_id)
    return len(matching)


def require_head() -> None:
    require_macos()
    current = settings.load()
    if current is None or current.role != "head":
        raise UsageProblem("tokens are managed on the head Mac")


def leave(out: Out) -> None:
    require_macos()
    current = settings.load()
    firewall.remove()
    power.restore()
    appfw.remove()
    if current is None or current.role == "head":
        service.uninstall()
    exo_service.uninstall()
    settings.clear()
    out("This Mac left the cluster. Employee tokens were kept.")
    out("exo itself stays installed; remove it from its menu bar icon -> Advanced -> Uninstall")


__all__ = ["UsageProblem", "ClusterError", "tokens", "init", "join", "status", "candidates", "load_model",
           "unload_model", "require_head", "leave", "lan_ipv4", "ExoClient"]
