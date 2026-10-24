"""Thunderbolt network setup for a Mac mesh (#503, #508).

Real Mac minis, 2026-10-23: the macOS Thunderbolt Bridge floods a triangle of
three Macs (rx 200 -> 38 000+ packets per 5 s). Per-port DHCP services with
the bridge gone gave a quiet full mesh.
"""

from __future__ import annotations

import subprocess

import pytest

from omm.cluster import netconfig
from omm.cluster.errors import ClusterError

HARDWARE_PORTS = """Hardware Port: Ethernet
Device: en0

Hardware Port: Thunderbolt 1
Device: en2

Hardware Port: Thunderbolt 2
Device: en3

Hardware Port: Thunderbolt 3
Device: en4
"""


def _runner(statuses, calls=None, hardware=HARDWARE_PORTS):
    """`statuses` maps a device to the ifconfig text for each successive call."""
    seen = {device: 0 for device in statuses}

    def run(argv, **_k):
        if calls is not None:
            calls.append(argv)
        if argv[:2] == ["/usr/sbin/networksetup", "-listallhardwareports"]:
            return subprocess.CompletedProcess(argv, 0, hardware, "")
        if argv[:1] == ["/sbin/ifconfig"]:
            texts = statuses[argv[1]]
            text = texts[min(seen[argv[1]], len(texts) - 1)]
            seen[argv[1]] += 1
            return subprocess.CompletedProcess(argv, 0, text, "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


ACTIVE_NO_ADDRESS = "en2: flags=8863<UP>\n\tstatus: active\n"
ACTIVE_WITH_ADDRESS = "en2: flags=8863<UP>\n\tinet 169.254.126.134 netmask 0xffff0000\n\tstatus: active\n"
INACTIVE = "en2: flags=8863<UP>\n\tstatus: inactive\n"


def test_apply_runs_one_root_script_and_names_no_user_values():
    calls = []
    netconfig.apply(run=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert calls == [["sudo", "/bin/sh", "-c", netconfig.APPLY_SCRIPT]]


def test_apply_script_removes_the_bridge_before_it_creates_the_port_services():
    script = netconfig.APPLY_SCRIPT
    destroy = script.index("ifconfig bridge0 destroy")
    plist = script.index("Delete :VirtualNetworkInterfaces:Bridge:bridge0")
    disable = script.index('"Thunderbolt Bridge" off') if '"Thunderbolt Bridge" off' in script else script.index("setnetworkserviceenabled")
    create = script.index("createnetworkservice")
    # Services created before the bridge entry was removed vanished on real Macs.
    assert destroy < plist < disable < create
    assert "-setdhcp" in script and "</dev/null" in script


def test_apply_script_keeps_one_backup_of_the_preferences_it_edits():
    assert "preferences.plist.pre-netconfig" in netconfig.APPLY_SCRIPT
    assert "[ -e" in netconfig.APPLY_SCRIPT


def test_apply_failure_is_a_cluster_error():
    with pytest.raises(ClusterError, match="Thunderbolt network"):
        netconfig.apply(run=lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", ""))


def test_restore_deletes_only_our_services_from_the_preferences_file():
    script = netconfig.RESTORE_SCRIPT
    # networksetup -removenetworkservice refuses on a real Mac, so the plist is edited.
    assert "-removenetworkservice" not in script
    assert '"omm Thunderbolt "*)' in script and 'plutil -remove "NetworkServices.$id"' in script
    assert "Network.Global.IPv4.ServiceOrder" in script


def test_restore_commits_without_the_bridge_before_it_puts_the_bridge_back():
    script = netconfig.RESTORE_SCRIPT
    drop = script.index("Delete :VirtualNetworkInterfaces:Bridge:bridge0")
    commit = script.index('setnetworkserviceenabled "Thunderbolt Bridge" off')
    add = script.index("Add :VirtualNetworkInterfaces:Bridge:bridge0 dict")
    enable = script.index('setnetworkserviceenabled "Thunderbolt Bridge" on')
    # configd only rebuilds the bridge when the entry changes between two commits.
    assert drop < commit < add < enable
    assert "__AUTO__ string thunderbolt-bridge" in script
    assert "Interfaces: string $device" in script


def test_restore_keeps_a_single_backup_and_never_touches_wifi_or_tailscale():
    script = netconfig.RESTORE_SCRIPT
    assert "preferences.plist.pre-restore" in script and "$(date" not in script
    assert "Wi-Fi" not in script and "Tailscale" not in script
    assert "@" not in script  # every placeholder was filled


def test_restore_failure_tells_the_user_how_to_get_the_bridge_back():
    with pytest.raises(ClusterError, match="System Settings"):
        netconfig.restore(run=lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", ""))


def test_wait_returns_at_once_when_no_cabled_port_lacks_an_address():
    messages = []
    run = _runner({"en2": [ACTIVE_WITH_ADDRESS], "en3": [INACTIVE], "en4": [INACTIVE]})
    netconfig.wait_for_addresses(messages.append, run=run, sleep=lambda s: pytest.fail("must not wait"))
    assert messages == []


def test_wait_polls_until_the_address_appears():
    messages, sleeps = [], []
    run = _runner({"en2": [ACTIVE_NO_ADDRESS, ACTIVE_NO_ADDRESS, ACTIVE_NO_ADDRESS, ACTIVE_WITH_ADDRESS],
                   "en3": [INACTIVE], "en4": [INACTIVE]})
    netconfig.wait_for_addresses(messages.append, run=run, sleep=sleeps.append)
    assert len(messages) == 1 and "Waiting for the Thunderbolt links" in messages[0]
    assert len(sleeps) == 3


def test_wait_gives_up_after_the_deadline_and_names_the_port():
    messages, sleeps = [], []
    run = _runner({"en2": [ACTIVE_NO_ADDRESS], "en3": [INACTIVE], "en4": [INACTIVE]})
    netconfig.wait_for_addresses(messages.append, run=run, sleep=sleeps.append, timeout_seconds=9)
    assert len(sleeps) == 3
    assert "No address yet on en2" in messages[-1]
