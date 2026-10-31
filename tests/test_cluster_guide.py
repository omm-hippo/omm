"""Bare `omm cluster`: steps from status problems, and the ask -> run -> look again loop (guide.py)."""

from __future__ import annotations

import sys

import pytest

from omm.cluster import commands, guide, settings
from omm.cluster.problems import Problem

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="omm cluster is macOS-only")

NS = "omm-" + "a" * 32


class FakeUi:
    def __init__(self, *, confirms=(), texts=(), choices=(), pauses=()):
        self.confirms, self.texts, self.choices, self.pauses = list(confirms), list(texts), list(choices), list(pauses)
        self.asked = []

    def confirm(self, message, default):
        self.asked.append(message)
        return self.confirms.pop(0)

    def text(self, message):
        self.asked.append(message)
        return self.texts.pop(0)

    def choose(self, message, choices):
        self.asked.append(message)
        return self.choices.pop(0)

    def pause(self, message):
        self.asked.append(message)
        return self.pauses.pop(0)


@pytest.fixture
def mac(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(guide.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(commands, "exo_answers", lambda out: True)
    monkeypatch.setattr(guide, "omm_behind", lambda timeout=5: False)
    return monkeypatch


def _reports(*problem_lists, role="head"):
    queue = [{"role": role, "namespace": NS, "exo": {"nodes": []}, "problems": list(p), "catching_up": False}
             for p in problem_lists]
    return lambda: queue.pop(0) if len(queue) > 1 else queue[0]


def test_steps_put_this_macs_fixes_first_and_other_macs_last():
    found = guide.steps([Problem("on node1", "tell", where="node1"), Problem("wait", "wait"),
                         Problem("cable", "manual"), Problem("fw", "run", fix="firewall"), "plain text"])
    assert [s.text for s in found] == ["fw", "cable", "plain text", "wait", "on node1"]


def test_not_a_terminal_only_prints(mac):
    settings.save(settings.ClusterSettings("head", NS, 52440))
    lines = []
    ui = FakeUi()
    code = guide.run(ui, lines.append, interactive=False,
                     status=_reports([Problem("fw; run omm cluster on this Mac to fix it", "run", fix="firewall")]))
    assert code == 0 and ui.asked == []
    assert "  1. [omm can do this] fw; run omm cluster on this Mac to fix it" in lines
    assert f"  omm cluster join {NS}" in lines


def test_runs_the_fix_after_a_yes_and_looks_again(mac):
    settings.save(settings.ClusterSettings("head", NS, 52440))
    ran = []
    mac.setitem(guide.FIXES, "firewall", lambda out: ran.append("firewall"))
    lines = []
    code = guide.run(FakeUi(confirms=[True]), lines.append, interactive=True,
                     status=_reports([Problem("fw", "run", fix="firewall")], []))
    assert code == 0 and ran == ["firewall"]
    assert lines[-1] == "Nothing to do: every check passed."


def test_a_fix_that_did_not_help_is_not_offered_again(mac):
    settings.save(settings.ClusterSettings("head", NS, 52440))
    ran = []
    mac.setitem(guide.FIXES, "firewall", lambda out: ran.append("firewall"))
    ui = FakeUi(confirms=[True])
    guide.run(ui, [].append, interactive=True, status=_reports([Problem("fw", "run", fix="firewall")]))
    assert ran == ["firewall"] and len(ui.asked) == 1


def test_waiting_looks_again_without_advising_anything(mac):
    settings.save(settings.ClusterSettings("head", NS, 52440))
    slept = []
    ui = FakeUi(confirms=[True, False])
    guide.run(ui, [].append, interactive=True, sleep=slept.append,
              status=_reports([Problem("Node2 is catching up", "wait", where="Node2")]))
    assert slept == [guide.WAIT_SECONDS] and ui.asked == ["Check again in 30 s?"] * 2


def test_no_restart_offered_on_a_mac_that_is_catching_up(mac):
    settings.save(settings.ClusterSettings("member", NS, 52440))
    restarted = []
    mac.setattr(commands, "restart", lambda out: restarted.append(1))
    report = {"role": "member", "namespace": NS, "exo": {"nodes": []}, "catching_up": True,
              "problems": [Problem("exo on this Mac sees no other Mac", "manual", after="restart")]}
    ui = FakeUi(pauses=[True, False])
    guide.run(ui, [].append, interactive=True, status=lambda: report)
    assert restarted == [] and not any("Restart exo" in q for q in ui.asked)


def test_cable_step_offers_restart_even_while_catching_up(mac):
    settings.save(settings.ClusterSettings("member", NS, 52440))
    restarted = []
    mac.setattr(commands, "restart", lambda out: restarted.append(1))
    report = {"role": "member", "namespace": NS, "exo": {"nodes": []}, "catching_up": True,
              "problems": [Problem("no Thunderbolt link between a and b", "manual", after="restart")]}
    guide.run(FakeUi(pauses=[True, False], confirms=[True]), [].append, interactive=True, status=lambda: report)
    assert restarted == [1]


def test_address_change_is_acknowledged_only_after_a_yes(mac):
    settings.save(settings.ClusterSettings("head", NS, 52440))
    acknowledged = []
    mac.setattr(commands, "acknowledge_addresses", lambda: acknowledged.append(1))
    problem = Problem("the head's address changed", "tell", where="employee PCs", fix="addresses_seen")
    guide.run(FakeUi(confirms=[True]), [].append, interactive=True, status=_reports([problem], []))
    assert acknowledged == [1]


def test_new_mac_joins_with_a_pasted_name_then_asks_for_cables(mac):
    joined, restarted = [], []
    mac.setattr(guide, "new_mac_steps", lambda: [])
    mac.setattr(commands, "join", lambda name, out: joined.append(name) or
                settings.save(settings.ClusterSettings("member", name, 52440)))
    mac.setattr(commands, "restart", lambda out: restarted.append(1))
    lines = []
    ui = FakeUi(choices=["join"], texts=["not-a-name", f"  {NS} "], confirms=[True], pauses=[True])
    code = guide.run(ui, lines.append, interactive=True, status=_reports([], role="member"))
    assert code == 0 and joined == [NS] and restarted == [1]
    assert "That is not a cluster name: it is omm- followed by 32 letters and digits." in lines


def test_new_mac_on_old_macos_stops_before_asking(mac):
    from omm.cluster.errors import ClusterError

    def old():
        raise ClusterError("exo needs macOS 26.2 or later (this Mac runs 15.6)")

    mac.setattr(guide.preflight, "require_supported_macos", old)
    ui = FakeUi()
    lines = []
    assert guide.run(ui, lines.append, interactive=True) == 0
    assert ui.asked == [] and any("exo needs macOS 26.2" in line for line in lines)


def test_new_mac_with_rdma_off_gets_the_recovery_steps(mac):
    mac.setattr(guide.preflight, "require_supported_macos", lambda: None)
    mac.setattr(guide.preflight, "rdma_status", lambda **k: "disabled")
    [step] = guide.new_mac_steps()
    assert step.kind == "manual" and "rdma_ctl enable" in step.text


def test_restart_waits_for_exo_before_looking_again(mac):
    settings.save(settings.ClusterSettings("member", NS, 52440))
    calls = []
    mac.setattr(commands, "restart", lambda out: calls.append("restart"))
    mac.setattr(commands, "exo_answers", lambda out: calls.append("wait") or False)
    lines = []
    guide.run(FakeUi(confirms=[True]), lines.append, interactive=True,
              status=_reports([Problem("exo is not running on this Mac", "run", fix="restart")], []))
    assert calls == ["restart", "wait"]
    assert any(line.startswith("exo has not answered for 60 seconds.") for line in lines)


def test_omm_behind_is_false_when_the_channel_tip_holds_the_same_files(monkeypatch):
    # 2026-10-30: the head's guide ran `omm update` right after install.sh
    # (installed: PR tip; main: its merge commit), and every member followed.
    from omm import cli, package_metadata

    monkeypatch.setattr(package_metadata, "install_source", lambda: package_metadata.InstallSource.GIT)
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "pr_tip")
    monkeypatch.setattr(cli, "_channel_branch", lambda: "main")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda branch, timeout=5: "merge")
    monkeypatch.setattr(cli, "_same_tree", lambda first, second: True)
    assert guide.omm_behind() is False
    monkeypatch.setattr(cli, "_same_tree", lambda first, second: False)
    monkeypatch.setattr("omm.cluster.version_follow._is_ancestor", lambda ancestor, descendant: False)
    assert guide.omm_behind() is True
