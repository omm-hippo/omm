"""pf anchor that keeps exo off the office network (#503)."""

from __future__ import annotations

from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

from omm.cluster import firewall

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="omm cluster is macOS-only (launchd, pf, POSIX paths and modes)"
)

FIX = Path(__file__).parent / "fixtures" / "exo"


def _ok(argv, **_k):
    return subprocess.CompletedProcess(argv, 0, "", "")


def _needs_password(argv, **_k):
    return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required")


def test_constants_match_spike():
    assert firewall.EXO_PORTS == (52415, 52416)
    assert firewall.PF_ANCHOR == "com.apple/omm-cluster"
    assert firewall.RULES_PATH == Path("/Library/Application Support/omm/cluster-pf.conf")
    assert firewall.DAEMON_PLIST == Path("/Library/LaunchDaemons/com.omm.cluster-pf.plist")


def test_thunderbolt_interfaces_from_real_fixture_excludes_bridge():
    ifaces = firewall.thunderbolt_interfaces((FIX / "listallhardwareports.txt").read_text(encoding="utf-8"))
    assert ifaces == ["en2", "en3", "en4"]


def test_render_rules_exact_text_for_head_mac():
    assert firewall.render_rules((52415, 52416), ("en5", "en6"), gateway_port=52440) == (
        "# managed by omm cluster - do not edit\n"
        'exo_ports = "{ 52415, 52416 }"\n'
        "pass out quick all keep state\n"
        "pass in quick on lo0 all\n"
        "pass in quick on { en5 en6 } all\n"
        "pass in quick proto tcp to port 52440\n"
        "pass in quick proto tcp to port 52416\n"
        "block return in quick proto tcp to port $exo_ports flags S/SA\n"
        "block return in quick proto udp to port $exo_ports\n"
        "block return in quick proto tcp to port 49153:65535 flags S/SA\n"
    )


def test_render_rules_member_without_thunderbolt_has_no_gateway_or_tb_line():
    text = firewall.render_rules((52415,), (), gateway_port=None)
    assert "{ }" not in text and "52440" not in text
    assert "pass in quick on lo0 all\n" in text
    assert text.endswith("block return in quick proto tcp to port 49153:65535 flags S/SA\n")


def test_apply_uses_one_sudo_call_with_paths_as_arguments(monkeypatch):
    monkeypatch.setattr(firewall, "thunderbolt_interfaces", lambda *a, **k: ["en2"])
    calls = []
    firewall.apply(gateway_port=52440, run=lambda argv, **k: calls.append(argv) or _ok(argv))
    sudo = [c for c in calls if c[0] == "sudo"]
    assert len(sudo) == 1
    assert sudo[0][:4] == ["sudo", "/bin/sh", "-c", firewall.SCRIPT]
    rules_tmp, plist_tmp = sudo[0][5], sudo[0][6]
    assert not Path(rules_tmp).exists() and not Path(plist_tmp).exists()


def test_apply_writes_daemon_that_loads_the_anchor(monkeypatch, tmp_path):
    monkeypatch.setattr(firewall, "thunderbolt_interfaces", lambda *a, **k: ["en2"])
    seen = {}

    def run(argv, **k):
        if argv[0] == "sudo":
            seen["rules"] = Path(argv[5]).read_text(encoding="utf-8")
            seen["plist"] = plistlib.loads(Path(argv[6]).read_bytes())
        return _ok(argv)

    firewall.apply(gateway_port=None, run=run)
    assert seen["plist"]["Label"] == "com.omm.cluster-pf"
    assert seen["plist"]["ProgramArguments"] == ["/bin/sh", "-c", firewall.LOAD_SCRIPT]
    assert '/sbin/pfctl -E -a com.apple/omm-cluster -f "/Library/Application Support/omm/cluster-pf.conf"' in firewall.LOAD_SCRIPT
    assert seen["plist"]["RunAtLoad"] is True
    assert seen["rules"] == firewall.render_rules(firewall.EXO_PORTS, ["en2"], gateway_port=None)


def test_apply_failure_raises_cluster_error(monkeypatch):
    import pytest

    from omm.cluster.errors import ClusterError

    monkeypatch.setattr(firewall, "thunderbolt_interfaces", lambda *a, **k: [])
    with pytest.raises(ClusterError, match="firewall"):
        firewall.apply(gateway_port=None, run=lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "denied"))


def test_status_missing_active_and_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(firewall, "RULES_PATH", tmp_path / "pf.conf")
    monkeypatch.setattr(firewall, "thunderbolt_interfaces", lambda *a, **k: ["en5"])
    assert firewall.status(gateway_port=None, run=_needs_password) == "missing"
    firewall.RULES_PATH.write_text(firewall.render_rules(firewall.EXO_PORTS, ("en5",), gateway_port=None), encoding="utf-8")
    loaded = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "block return in quick proto tcp\n", "")
    assert firewall.status(gateway_port=None, run=loaded) == "active"
    # Without a cached sudo ticket pf cannot be read: the file alone proves nothing is loaded.
    assert firewall.status(gateway_port=None, run=_needs_password) == "unchecked"
    monkeypatch.setattr(firewall, "thunderbolt_interfaces", lambda *a, **k: ["en5", "en6"])
    assert firewall.status(gateway_port=None, run=_needs_password) == "drift"
    assert firewall.status(gateway_port=None, run=loaded) == "drift"


def test_status_reports_missing_when_loaded_anchor_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(firewall, "RULES_PATH", tmp_path / "pf.conf")
    monkeypatch.setattr(firewall, "thunderbolt_interfaces", lambda *a, **k: [])
    firewall.RULES_PATH.write_text(firewall.render_rules(firewall.EXO_PORTS, (), gateway_port=None), encoding="utf-8")
    empty = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", "")
    assert firewall.status(gateway_port=None, run=empty) == "missing"


def test_remove_uses_one_sudo_call():
    calls = []
    firewall.remove(run=lambda argv, **k: calls.append(argv) or _ok(argv))
    assert [c[:4] for c in calls] == [["sudo", "/bin/sh", "-c", firewall.REMOVE_SCRIPT]]


def test_thunderbolt_bridge_is_trusted_with_the_ports():
    # With Thunderbolt Bridge on, en2-en4 are bridge0 members and pf sees
    # traffic arrive on bridge0 (review Critical 1).
    ifaces = firewall.trusted_interfaces((FIX / "listallhardwareports.txt").read_text(encoding="utf-8"))
    assert ifaces == ["bridge0", "en2", "en3", "en4"]


def test_apply_trusts_bridge_and_ports(monkeypatch):
    seen = {}
    monkeypatch.setattr(firewall, "_hardware_ports", lambda run: (FIX / "listallhardwareports.txt").read_text(encoding="utf-8"))

    def run(argv, **k):
        if argv[0] == "sudo":
            seen["rules"] = Path(argv[5]).read_text(encoding="utf-8")
        return _ok(argv)

    firewall.apply(gateway_port=None, run=run)
    assert "pass in quick on { bridge0 en2 en3 en4 } all\n" in seen["rules"]


def test_remove_failure_raises_cluster_error():
    import pytest

    from omm.cluster.errors import ClusterError

    with pytest.raises(ClusterError, match="firewall"):
        firewall.remove(run=lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "Sorry, try again."))


def _fake_pfctl(tmp_path):
    """A pfctl that keeps enable references like the real one (`-E` adds one
    and prints its token, `-X <token>` releases it or fails)."""
    refs = tmp_path / "refs"
    refs.write_text("", encoding="utf-8")
    fake = tmp_path / "pfctl"
    fake.write_text(f"""#!/bin/sh
case "$1" in
  -E) n=$(( $(wc -l < "{refs}") + 100 )); echo "$n" >> "{refs}"; echo "Token : $n" >&2 ;;
  -X) grep -qx "$2" "{refs}" || exit 1; grep -vx "$2" "{refs}" > "{refs}.new"; mv "{refs}.new" "{refs}" ;;
esac
exit 0
""", encoding="utf-8")
    fake.chmod(0o755)
    return fake, refs


def _local(script, fake, token):
    return script.replace("/sbin/pfctl", str(fake)).replace(str(firewall.TOKEN_PATH), str(token))


def test_loading_again_gives_back_the_previous_pf_enable_reference(tmp_path):
    # Real Mac mini 2026-10-24: three init/leave rounds left three pfctl
    # references, so pf stayed enabled for a day after leave.
    fake, refs = _fake_pfctl(tmp_path)
    token = tmp_path / "cluster-pf.token"
    for _ in range(3):  # boot, init, init again
        subprocess.run(["sh", "-c", _local(firewall.LOAD_SCRIPT, fake, token)], check=True)
    assert len(refs.read_text(encoding="utf-8").split()) == 1
    assert token.read_text(encoding="utf-8").strip() == refs.read_text(encoding="utf-8").strip()


def test_removing_gives_back_the_last_pf_enable_reference(tmp_path):
    fake, refs = _fake_pfctl(tmp_path)
    token = tmp_path / "cluster-pf.token"
    subprocess.run(["sh", "-c", _local(firewall.LOAD_SCRIPT, fake, token)], check=True)
    subprocess.run(["sh", "-c", _local(firewall.RELEASE_TOKEN, fake, token)], check=True)
    assert refs.read_text(encoding="utf-8").split() == [] and not token.exists()
    assert firewall.RELEASE_TOKEN in firewall.REMOVE_SCRIPT
    # After a reboot the saved token is stale; releasing it must not fail the script.
    token.write_text("999\n", encoding="utf-8")
    subprocess.run(["sh", "-c", _local(firewall.RELEASE_TOKEN, fake, token)], check=True)


def test_exo_peer_port_is_reachable_from_the_office_network():
    # exo dials a peer's office-network address before its Thunderbolt one and
    # skips the rest while that dial is pending, so with 52416 blocked three
    # Mac minis met only by luck (2 of 6 tries); with it open, 10 of 10. The
    # pnet key behind that port is derived from the cluster name.
    text = firewall.render_rules(firewall.EXO_PORTS, ("en2",), gateway_port=None)
    assert text.index("pass in quick proto tcp to port 52416\n") < text.index("block return in quick proto tcp")
    assert "pass in quick proto tcp to port 52415" not in text
