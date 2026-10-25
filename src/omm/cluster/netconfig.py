"""Thunderbolt network setup that keeps a Mac mesh from storming (#503, #508).

macOS bridges every Thunderbolt port into one `bridge0` (the "Thunderbolt
Bridge" service). The bridge has no working spanning tree, so cabling three
Macs in a triangle floods it with the same packets for ever: real Mac minis,
2026-10-23, rx rose from ~200 to 38 000+ packets per 5 s about 90 s after the
third cable. exo's own app avoids this by deleting the bridge and giving every
Thunderbolt port its own DHCP service (no DHCP server on a cable, so each port
falls back to a 169.254.x.x link-local address that libp2p finds peers on).
omm never starts the app, so init/join do the same here. Verified on three Mac
minis with all three cables in place: no storm, full mesh, RDMA links up.

Two things the real machines taught us: the order matters (services created
before the bridge entry is removed from preferences.plist vanished), and
`networksetup` inside a `while read` loop swallows the loop's stdin.
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable

from omm.cluster.errors import ClusterError
from omm.cluster.firewall import thunderbolt_interfaces

SERVICE_PREFIX = "omm Thunderbolt"
PREFS = "/Library/Preferences/SystemConfiguration/preferences.plist"
BACKUP_DIR = "/Library/Application Support/omm"

# Run as root, no arguments. Safe to run again: every step checks first.
APPLY_SCRIPT = f"""PREFS="{PREFS}"
mkdir -p "{BACKUP_DIR}"
# Snapshot the original: on a first run, or whenever a live bridge is about to
# go (an init after `leave` put it back would otherwise keep a stale copy).
# A re-run on a Mac already set up has no bridge and keeps the first copy.
if [ ! -e "{BACKUP_DIR}/preferences.plist.pre-netconfig" ] || ifconfig bridge0 >/dev/null 2>&1; then cp "$PREFS" "{BACKUP_DIR}/preferences.plist.pre-netconfig"; fi
if ifconfig bridge0 >/dev/null 2>&1; then
  for member in $(ifconfig bridge0 | awk '/member:/ {{print $2}}'); do ifconfig bridge0 deletem "$member" 2>/dev/null; done
  ifconfig bridge0 destroy 2>/dev/null
fi
/usr/libexec/PlistBuddy -c "Delete :VirtualNetworkInterfaces:Bridge:bridge0" "$PREFS" 2>/dev/null
networksetup -listallnetworkservices | grep -q "Thunderbolt Bridge" && networksetup -setnetworkserviceenabled "Thunderbolt Bridge" off
ports=$(networksetup -listallhardwareports | awk -F': ' '/^Hardware Port: Thunderbolt [0-9]+$/ {{print $2}}')
echo "$ports" | while read -r port; do
  [ -n "$port" ] || continue
  service="omm $port"
  networksetup -listallnetworkservices | grep -qx "$service" || networksetup -createnetworkservice "$service" "$port" </dev/null || exit 1
  networksetup -setdhcp "$service" </dev/null || exit 1
done
"""

# Put the Thunderbolt Bridge back. Real Mac mini (macOS 26.7.1) findings:
# - `networksetup -removenetworkservice` refuses to drop our service ("there
#   aren't any other network services on Thunderbolt N"), so step 1 deletes
#   them from the preferences file.
# - configd only rebuilds the bridge when `VirtualNetworkInterfaces` changes
#   between two applied commits, and the kernel refuses bridge members while a
#   port still has a service of its own. So: services gone, commit without the
#   bridge entry, then put the entry back and commit again.
# Run as root; safe to run again. Every `networksetup` write re-reads the
# preferences file and applies it, which is the signal configd needs (no
# `killall configd`, no reboot; Wi-Fi and Tailscale are never touched).
RESTORE_SCRIPT = """P="@PREFS@"
PB=/usr/libexec/PlistBuddy
mkdir -p "@BACKUP_DIR@"
cp "$P" "@BACKUP_DIR@/preferences.plist.pre-restore"
removed=""
for id in $(plutil -extract NetworkServices raw -o - "$P" 2>/dev/null); do
  name=$(plutil -extract "NetworkServices.$id.UserDefinedName" raw -o - "$P" 2>/dev/null)
  case "$name" in
    "@PREFIX@ "*) plutil -remove "NetworkServices.$id" "$P"; removed="$removed $id" ;;
  esac
done
for set in $(plutil -extract Sets raw -o - "$P" 2>/dev/null); do
  for id in $removed; do plutil -remove "Sets.$set.Network.Service.$id" "$P" 2>/dev/null; done
  order="Sets.$set.Network.Global.IPv4.ServiceOrder"
  i=$(plutil -extract "$order" raw -o - "$P" 2>/dev/null) || continue
  while [ "$i" -gt 0 ]; do
    i=$((i - 1))
    entry=$(plutil -extract "$order.$i" raw -o - "$P")
    for id in $removed; do [ "$entry" = "$id" ] && plutil -remove "$order.$i" "$P"; done
  done
done
$PB -c "Delete :VirtualNetworkInterfaces:Bridge:bridge0" "$P" 2>/dev/null
if ifconfig bridge0 >/dev/null 2>&1; then
  for member in $(ifconfig bridge0 | awk '/member:/ {print $2}'); do ifconfig bridge0 deletem "$member" 2>/dev/null; done
  ifconfig bridge0 destroy 2>/dev/null
fi
networksetup -setnetworkserviceenabled "Thunderbolt Bridge" off
$PB -c "Print :VirtualNetworkInterfaces" "$P" >/dev/null 2>&1 || $PB -c "Add :VirtualNetworkInterfaces dict" "$P"
$PB -c "Print :VirtualNetworkInterfaces:Bridge" "$P" >/dev/null 2>&1 || $PB -c "Add :VirtualNetworkInterfaces:Bridge dict" "$P"
$PB -c "Add :VirtualNetworkInterfaces:Bridge:bridge0 dict" "$P"
$PB -c "Add :VirtualNetworkInterfaces:Bridge:bridge0:Interfaces array" "$P"
for device in $(networksetup -listallhardwareports | awk '/^Hardware Port: Thunderbolt [0-9]+$/ {getline; print $2}' | sort); do
  $PB -c "Add :VirtualNetworkInterfaces:Bridge:bridge0:Interfaces: string $device" "$P"
done
$PB -c "Add :VirtualNetworkInterfaces:Bridge:bridge0:Options dict" "$P"
$PB -c "Add :VirtualNetworkInterfaces:Bridge:bridge0:Options:__AUTO__ string thunderbolt-bridge" "$P"
networksetup -setnetworkserviceenabled "Thunderbolt Bridge" on
for attempt in $(seq 1 40); do
  ifconfig bridge0 2>/dev/null | grep -q 'member:' && ifconfig bridge0 | grep -q 'inet ' && break
  sleep 0.5
done
exit 0
""".replace("@PREFS@", PREFS).replace("@BACKUP_DIR@", BACKUP_DIR).replace("@PREFIX@", SERVICE_PREFIX)

ADDRESS_WAIT_SECONDS = 90
_POLL_SECONDS = 3


def apply(*, run=subprocess.run) -> None:
    """Replace the Thunderbolt Bridge with per-port DHCP services (one sudo prompt)."""
    result = run(["sudo", "/bin/sh", "-c", APPLY_SCRIPT], check=False)
    if result.returncode != 0:
        raise ClusterError("could not set up the Thunderbolt network (sudo or networksetup failed)")


def restore(*, run=subprocess.run) -> None:
    result = run(["sudo", "/bin/sh", "-c", RESTORE_SCRIPT], check=False)
    if result.returncode != 0:
        raise ClusterError("could not put the Thunderbolt Bridge back (sudo failed); "
                           "turn it on in System Settings > Network if you need it")


def _active_without_address(device: str, run) -> bool:
    text = run(["/sbin/ifconfig", device], capture_output=True, text=True, check=False).stdout or ""
    return "status: active" in text and "inet " not in text


def wait_for_addresses(out: Callable[[str], None], *, run=subprocess.run, sleep=time.sleep,
                       timeout_seconds: int = ADDRESS_WAIT_SECONDS) -> None:
    """Wait until every cabled Thunderbolt port has its link-local address.

    The address only appears ~1 min after DHCP gives up, and exo only looks for
    peers on addresses that exist when it starts, so init/join start exo after
    this. A port that is still pending at the deadline is left alone: its
    cable may simply be plugged in later (then restart exo or run init/join
    again).
    """
    devices = thunderbolt_interfaces(run=run)
    pending = [d for d in devices if _active_without_address(d, run)]
    if not pending:
        return
    out(f"Waiting for the Thunderbolt links to get addresses (up to {timeout_seconds} s)...")
    for _ in range(timeout_seconds // _POLL_SECONDS):
        sleep(_POLL_SECONDS)
        pending = [d for d in pending if _active_without_address(d, run)]
        if not pending:
            return
    out(f"No address yet on {', '.join(pending)}; if its cable is not plugged in, ignore this.")
