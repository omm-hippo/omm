"""Bare `omm cluster`: show this Mac's next step and, after a yes, run it.

The steps come from the same problem list `omm cluster status` prints
(problems.Problem), so the two never disagree. Fixes are narrow: one fix,
never a whole init/join, which would restart exo (and lose a catching-up
Mac's replay progress). Nothing runs on another Mac; those steps only say
what to run there. Design: docs/superpowers/specs/2026-10-29-cluster-guided-next-step-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
import platform
import time
from typing import Callable, Protocol

from omm.cluster import commands, preflight, settings
from omm.cluster.commands import Out
from omm.cluster.errors import ClusterError, UsageProblem
from omm.cluster.problems import Kind, of

# A wait step offers to look again after this long.
WAIT_SECONDS = 30
_ORDER = {"run": 0, "manual": 1, "wait": 2, "tell": 3}


@dataclass(frozen=True)
class Step:
    kind: Kind
    text: str
    where: str | None = None
    fix: str | None = None
    after: str | None = None

    def as_json(self) -> dict:
        return {"kind": self.kind, "text": self.text, "where": self.where, "fix": self.fix}


class Ui(Protocol):
    def confirm(self, message: str, default: bool) -> bool: ...

    def text(self, message: str) -> str | None: ...

    def choose(self, message: str, choices: list[tuple[str, str]]) -> str | None: ...

    def pause(self, message: str) -> bool: ...


def steps(problems: list[str]) -> list[Step]:
    """Problems as steps: what omm runs here first, then what a person does here, waits, other Macs."""
    seen, found = set(), []
    for problem in map(of, problems):
        if str(problem) in seen:
            continue
        seen.add(str(problem))
        found.append(Step(problem.kind, str(problem), problem.where, problem.fix, problem.after))
    return sorted(found, key=lambda step: _ORDER[step.kind])


def label(step: Step) -> str:
    if step.kind == "run":
        return "omm can do this"
    if step.kind == "manual":
        return "on this Mac"
    if step.kind == "wait":
        return "wait"
    return f"on {step.where}" if step.where else "elsewhere"


def _update_omm(out: Out) -> None:
    import subprocess
    import sys

    # The entry point that is running now (an `omm_verify` install updates itself, not another omm).
    result = subprocess.run([sys.argv[0], "update"], check=False)
    if result.returncode != 0:
        raise ClusterError("omm update did not finish; run omm update to see why")


def _restart_exo(out: Out) -> None:
    commands.restart(out)
    # Looking again at once would find exo still starting and call it stopped
    # (Node2, 2026-10-29); a Mac held on a screen prompt shows up here too.
    if not commands.exo_answers(out):
        out(f"exo has not answered for {commands.EXO_START_SECONDS} seconds. {commands.EXO_HELD_FIX}")


def _restart_gateway(out: Out) -> None:
    from omm.cluster import service

    service.install()
    out("Gateway started.")


def _fix_firewall(out: Out) -> None:
    from omm.cluster import firewall

    firewall.apply(gateway_port=settings.load().gateway_port)
    out("Cluster firewall rules loaded.")


def _fix_sleep(out: Out) -> None:
    from omm.cluster import power

    power.keep_awake()
    out("Idle sleep turned off.")


def _fix_appfw(out: Out) -> None:
    from omm.cluster import appfw

    appfw.allow([appfw.exo_binary(), appfw.gateway_binary()])
    out("exo and the gateway are allowed in the macOS firewall.")


def _fix_member_link(out: Out) -> None:
    from omm.cluster import member_service

    member_service.install()
    out("Member link started.")


def _fix_bridge(out: Out) -> None:
    from omm.cluster import exo_service, netconfig

    netconfig.apply()
    netconfig.wait_for_addresses(out)
    # exo only looks for peers on addresses that exist when it starts.
    exo_service.restart()
    out("Thunderbolt Bridge replaced by one network per Thunderbolt port; exo restarted.")


FIXES: dict[str, Callable[[Out], None]] = {
    "restart": _restart_exo,
    "firewall": _fix_firewall,
    "keep_awake": _fix_sleep,
    "appfw": _fix_appfw,
    "gateway": _restart_gateway,
    "member_link": _fix_member_link,
    "netconfig": _fix_bridge,
    "update": _update_omm,
}


def omm_behind(timeout: float = 5) -> bool | None:
    """True when this Mac's omm is behind its update channel; None when that cannot be checked."""
    from omm import cli, package_metadata

    if package_metadata.install_source() is not package_metadata.InstallSource.GIT:
        return None
    local = cli._src_head_commit()
    remote = cli._remote_head_commit(cli._channel_branch(), timeout=timeout)
    if not local or not remote:
        return None
    if local == remote:
        return False
    from omm.cluster import version_follow

    # A remote commit this clone has never fetched cannot be an ancestor of it.
    return version_follow._is_ancestor(remote, local) is not True


def new_mac_steps() -> list[Step]:
    """What this Mac needs before it can be in a cluster (it is in none yet)."""
    found: list[Step] = []
    try:
        preflight.require_supported_macos()
    except ClusterError as error:
        return [Step("manual", f"{error}; update macOS in System Settings > General > Software Update")]
    rdma = preflight.rdma_status()
    if rdma == "disabled":
        found.append(Step("manual", "RDMA over Thunderbolt is off. To turn it on: " + " ".join(preflight.RDMA_GUIDE)))
    elif rdma == "unavailable":
        found.append(Step("manual", "this macOS has no RDMA over Thunderbolt (rdma_ctl is missing); update macOS "
                                    "to the build the other Macs run"))
    return found


def show(out: Out, report: dict, found: list[Step]) -> None:
    role = report.get("role")
    nodes = report.get("exo", {}).get("nodes", [])
    out(f"This Mac: {role or 'not in a cluster'}" + (f" in {report['namespace']}" if report.get("namespace") else "")
        + (f", {len(nodes)} Mac(s) in the cluster" if nodes else ""))
    if role == "head" and report.get("namespace"):
        out("To add a Mac, run omm cluster on it and join this cluster:")
        out(f"  omm cluster join {report['namespace']}")
    if not found:
        out("Nothing to do: every check passed.")
        return
    out("Next steps:")
    for number, step in enumerate(found, 1):
        out(f"  {number}. [{label(step)}] {step.text}")


def _join_or_init(ui: Ui, out: Out) -> bool:
    choice = ui.choose("This Mac is not in a cluster yet. What do you want to do?",
                       [("join", "Join an existing cluster (another Mac is already its head)"),
                        ("init", "Create a new cluster with this Mac as its head"),
                        ("quit", "Nothing for now")])
    if choice == "join":
        while True:
            name = ui.text("Cluster name (run omm cluster on the head Mac to see it):")
            if name is None:
                return False
            name = name.strip()
            if settings.NAMESPACE_RE.match(name):
                break
            out("That is not a cluster name: it is omm- followed by 32 letters and digits.")
        if not ui.confirm(f"Join {name}? omm asks for this Mac's password to set up the network and firewall.", True):
            return False
        commands.join(name, out)
    elif choice == "init":
        if not ui.confirm("Make this Mac the head of a new cluster? omm asks for this Mac's password.", True):
            return False
        commands.init(out)
    else:
        return False
    out("Now connect this Mac to every other Mac with a Thunderbolt 5 cable (each Mac needs one port per other Mac).")
    if ui.pause("Press Enter once the cables are in (Esc to stop here)"):
        _restart_exo(out)
    return True


def run(ui: Ui, out: Out, *, interactive: bool, status: Callable[[], dict] = commands.status,
        sleep: Callable[[float], None] = time.sleep, check_updates: bool = True) -> int:
    """The `omm cluster` loop; returns the exit code."""
    if platform.system() != "Darwin":
        out("omm cluster runs on Macs only (macOS 26.2 or later).")
        return 0
    if settings.load() is None:
        found = new_mac_steps()
        show(out, {"role": None}, found)
        if not interactive:
            out("Then run omm cluster join <name> (the name is shown by omm cluster on the head Mac), "
                "or omm cluster init to start a new cluster.")
            return 0
        if any(step.text.startswith("exo needs macOS") for step in found):
            return 0
        if not _join_or_init(ui, out):
            return 0
    tried: set[str] = set()
    cables_changed = False
    while True:
        report = status()
        found = [step for step in steps(report["problems"]) if not (step.kind == "run" and step.text in tried)]
        if check_updates and interactive and report.get("role") == "head" and omm_behind():
            found.insert(0, Step("run", "omm on this Mac is behind its update channel; the other Macs follow it "
                                        "once it is updated", fix="update"))
            check_updates = False
        show(out, report, found)
        if not interactive or not found:
            return 0
        step = found[0]
        if step.kind == "run" and step.fix in FIXES:
            if not ui.confirm(f"Run it now? ({step.text})", True):
                return 0
            tried.add(step.text)
            try:
                FIXES[step.fix](out)
            except (ClusterError, UsageProblem) as error:
                out(f"That did not work: {error}")
                return 1
            if step.fix == "update":
                out("omm was updated; run omm cluster again to continue with the new version.")
                return 0
            continue
        if step.kind == "manual":
            if not ui.pause("Press Enter when that is done (Esc to stop here)"):
                return 0
            cables_changed = cables_changed or step.text.startswith("no Thunderbolt link")
            # A Mac still replaying the cluster's history would start over.
            if step.after == "restart" and (cables_changed or not report.get("catching_up")):
                if ui.confirm("Restart exo on this Mac now so it looks for the other Macs again?", True):
                    _restart_exo(out)
            continue
        if step.kind == "wait":
            if not ui.confirm(f"Check again in {WAIT_SECONDS} s?", True):
                return 0
            sleep(WAIT_SECONDS)
            continue
        if step.fix == "addresses_seen":
            if ui.confirm("Have all employee PCs switched to the new address?", False):
                commands.acknowledge_addresses()
                continue
        return 0

