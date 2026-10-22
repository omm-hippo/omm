"""pf rules that keep exo off the office network on every cluster Mac.

exo's API (52415), libp2p (52416) and, once a model spans several Macs, its
MLX ring (a random TCP port in 49153-65535 bound to 0.0.0.0) all accept
unauthenticated connections. The anchor trusts loopback and Thunderbolt only,
blocks those ports and every high TCP port arriving from anywhere else, and
lets outbound traffic keep state so replies still get back in. Verified on a
real Mac mini 2026-10-22 (spike findings §2-3). The anchor lives under
com.apple/*, which /etc/pf.conf already evaluates, so pf.conf is never edited.
"""

from __future__ import annotations

from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
from typing import Literal, Sequence

from omm.cluster.errors import ClusterError

EXO_PORTS: tuple[int, ...] = (52415, 52416)
PF_ANCHOR = "com.apple/omm-cluster"
RULES_PATH = Path("/Library/Application Support/omm/cluster-pf.conf")
DAEMON_LABEL = "com.omm.cluster-pf"
DAEMON_PLIST = Path("/Library/LaunchDaemons/com.omm.cluster-pf.plist")
_HIGH_PORTS = "49153:65535"

# Run as root with the rendered rules as $1 and the daemon plist as $2.
SCRIPT = f"""set -e
mkdir -p "{RULES_PATH.parent}"
install -m 644 "$1" "{RULES_PATH}"
install -m 644 "$2" "{DAEMON_PLIST}"
launchctl bootout system/{DAEMON_LABEL} 2>/dev/null || true
for attempt in 1 2 3 4 5; do
  launchctl bootstrap system "{DAEMON_PLIST}" && exit 0
  sleep 1
done
exit 1
"""

REMOVE_SCRIPT = f"""launchctl bootout system/{DAEMON_LABEL} 2>/dev/null || true
rm -f "{DAEMON_PLIST}" "{RULES_PATH}"
/sbin/pfctl -a {PF_ANCHOR} -F all 2>/dev/null || true
"""


def _hardware_ports(run) -> str:
    result = run(["/usr/sbin/networksetup", "-listallhardwareports"], capture_output=True, text=True, check=False)
    return result.stdout or ""


def _devices(text: str, wanted) -> list[str]:
    devices, port = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port:
            if wanted(port):
                devices.append(line.split(":", 1)[1].strip())
            port = None
    return devices


def thunderbolt_interfaces(text: str | None = None, *, run=subprocess.run) -> list[str]:
    if text is None:
        text = _hardware_ports(run)
    return sorted(_devices(text, lambda port: port.startswith("Thunderbolt ") and port != "Thunderbolt Bridge"))


def trusted_interfaces(text: str | None = None, *, run=subprocess.run) -> list[str]:
    """Thunderbolt ports plus the Thunderbolt Bridge: while the bridge is on,
    the ports are its members and pf sees their traffic arrive on bridge0."""
    if text is None:
        text = _hardware_ports(run)
    bridge = _devices(text, lambda port: port == "Thunderbolt Bridge")
    return sorted(set(thunderbolt_interfaces(text)) | set(bridge))


def render_rules(exo_ports: Sequence[int], tb_ifaces: Sequence[str], *, gateway_port: int | None) -> str:
    lines = [
        "# managed by omm cluster - do not edit",
        f'exo_ports = "{{ {", ".join(str(p) for p in exo_ports)} }}"',
        "pass out quick all keep state",
        "pass in quick on lo0 all",
    ]
    if tb_ifaces:
        lines.append(f"pass in quick on {{ {' '.join(tb_ifaces)} }} all")
    if gateway_port is not None:
        lines.append(f"pass in quick proto tcp to port {gateway_port}")
    lines += [
        "block drop in quick proto { tcp udp } to port $exo_ports",
        f"block drop in quick proto tcp to port {_HIGH_PORTS}",
    ]
    return "\n".join(lines) + "\n"


def _daemon_plist() -> bytes:
    return plistlib.dumps({
        "Label": DAEMON_LABEL,
        "ProgramArguments": ["/sbin/pfctl", "-E", "-a", PF_ANCHOR, "-f", str(RULES_PATH)],
        "RunAtLoad": True,
    })


def apply(*, gateway_port: int | None, run=subprocess.run) -> None:
    """Install the rules and the boot-time loader with one sudo prompt."""
    rules = render_rules(EXO_PORTS, trusted_interfaces(run=run), gateway_port=gateway_port)
    workdir = Path(tempfile.mkdtemp(prefix="omm-cluster-pf-"))
    try:
        rules_tmp, plist_tmp = workdir / "cluster-pf.conf", workdir / "cluster-pf.plist"
        rules_tmp.write_text(rules, encoding="utf-8")
        plist_tmp.write_bytes(_daemon_plist())
        result = run(["sudo", "/bin/sh", "-c", SCRIPT, "omm-cluster-pf", str(rules_tmp), str(plist_tmp)], check=False)
        if result.returncode != 0:
            raise ClusterError("could not install the cluster firewall rules (sudo or launchctl failed)")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def remove(*, run=subprocess.run) -> None:
    result = run(["sudo", "/bin/sh", "-c", REMOVE_SCRIPT], check=False)
    if result.returncode != 0:
        raise ClusterError("could not remove the cluster firewall rules (sudo failed); nothing else was changed")


def status(*, gateway_port: int | None, run=subprocess.run) -> Literal["active", "missing", "drift"]:
    try:
        on_disk = RULES_PATH.read_text(encoding="utf-8")
    except OSError:
        return "missing"
    loaded = run(["sudo", "-n", "/sbin/pfctl", "-a", PF_ANCHOR, "-sr"], capture_output=True, text=True, check=False)
    if loaded.returncode == 0 and "block drop" not in (loaded.stdout or ""):
        return "missing"
    expected = render_rules(EXO_PORTS, trusted_interfaces(run=run), gateway_port=gateway_port)
    return "active" if on_disk == expected else "drift"
