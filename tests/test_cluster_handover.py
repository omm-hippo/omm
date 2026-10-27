"""Moving the head role to another Mac (cluster/handover.py, `omm cluster handover`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
import threading
import types

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.cluster import (commands, document_search, handover, member_auth, member_link, member_service, members,
                         rag_backup, service, settings, tls, tokens, version_follow)
from omm.cluster.errors import ClusterError
from omm.documents import DocumentStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="omm cluster is macOS-only")

runner = CliRunner()
NS = "omm-" + "a" * 32
HEAD_V = {"commit": "b" * 40, "branch": "main", "git": True}
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
STATE = {"nodeIdentities": {"me": {"friendlyName": "node4"}, "t": {"friendlyName": "node1"}},
         "nodeMemory": {"me": {}, "t": {}},
         "nodeNetwork": {"me": {"interfaces": [{"ipAddress": "169.254.0.4"}, {"ipAddress": "192.0.2.4"}]},
                         "t": {"interfaces": [{"ipAddress": "169.254.0.1"}, {"ipAddress": "192.0.2.1"}]}}}


@pytest.fixture
def head(isolated_omm_home, monkeypatch):
    settings.save(settings.ClusterSettings("head", NS, 52440, known_nodes=("node4", "node1"), head_epoch=2,
                                           models=(settings.KeptModel("mlx-community/x", "Tensor", 2),)))
    monkeypatch.setattr(version_follow, "local_version", lambda: dict(HEAD_V))
    monkeypatch.setattr(member_link, "_raw_state", lambda: STATE)
    monkeypatch.setattr(member_link, "local_ipv4s", lambda run=None: ["169.254.0.4"])
    monkeypatch.setattr(document_search, "_ollama", lambda m, u: ("running", True))
    calls = types.SimpleNamespace(log=[])
    monkeypatch.setattr(service, "install", lambda **k: calls.log.append("gateway install"))
    monkeypatch.setattr(service, "uninstall", lambda **k: calls.log.append("gateway uninstall"))
    monkeypatch.setattr(member_service, "install", lambda **k: calls.log.append("member install"))
    monkeypatch.setattr(member_service, "uninstall", lambda **k: calls.log.append("member uninstall"))
    return calls


def _record(node="t", seen=NOW, **fields):
    report = {"version": dict(HEAD_V), "version_state": "same", "version_error": None, "rag_marker": None,
              "prepared": True, "documents_ready": True, **fields}
    members.record(node, report, now=seen)


def test_preflight_accepts_a_ready_member(head):
    _record()
    assert handover.preflight("node1", now=NOW) == ("t", [])


def test_preflight_refuses_unknown_mac_and_itself(head):
    assert "no Mac named nodeX" in handover.preflight("nodeX", now=NOW)[1][0]
    assert "already the head" in handover.preflight("node4", now=NOW)[1][0]


def test_preflight_refuses_unprepared_target(head):
    _record(prepared=False)
    assert any(f"run omm cluster join {NS} again on node1 first" in p for p in handover.preflight("node1", now=NOW)[1])


def test_preflight_refuses_silent_target(head):
    _record(seen=NOW - timedelta(minutes=3))
    assert any("has not checked in" in p for p in handover.preflight("node1", now=NOW)[1])


def test_preflight_refuses_version_mismatch(head):
    _record(version={"commit": "a" * 40, "branch": "main", "git": True})
    assert any("runs omm aaaaaaa" in p for p in handover.preflight("node1", now=NOW)[1])


def test_preflight_refuses_documents_not_ready_when_head_has_documents(head):
    DocumentStore().create_collection("company")
    _record(documents_ready=False)
    assert any("cannot answer document questions yet" in p for p in handover.preflight("node1", now=NOW)[1])


def test_start_bumps_epoch_and_carries_models(head):
    assert handover.start("t", now=datetime.now(timezone.utc)) == 3
    pending = members.pending_handover()
    assert pending["to"] == "t" and pending["epoch"] == 3
    assert pending["settings"]["models"] == [{"model": "mlx-community/x", "sharding": "Tensor", "macs": 2}]
    assert pending["settings"]["known_nodes"] == ["node4", "node1"] and pending["settings"]["gateway_port"] == 52440


def test_wait_for_new_head_true_when_target_answers_with_epoch(head, monkeypatch):
    answers = iter([ClusterError("not yet"), {"head": True, "epoch": 2}, {"head": True, "epoch": 3}])
    tried = []

    def fake(address, method, path, key, own, *, body=None, timeout=5):
        tried.append(address)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return 200, json.dumps(answer).encode(), {}

    monkeypatch.setattr(member_link, "request", fake)
    assert handover.wait_for_new_head("t", 3, deadline=float("inf"), sleep=lambda s: None)
    # Thunderbolt first, the office network only when that does not answer.
    assert tried[:2] == ["169.254.0.1:52440", "192.0.2.1:52440"]


def test_wait_for_new_head_false_at_deadline(head, monkeypatch):
    monkeypatch.setattr(member_link, "request", lambda *a, **k: (_ for _ in ()).throw(ClusterError("no")))
    assert not handover.wait_for_new_head("t", 3, deadline=0, sleep=lambda s: None)


def test_step_down_keeps_tls_and_removes_tokens(head):
    tls.ensure(names=["127.0.0.1"])
    ca = tls.ca_pem()
    tokens.add_token("kim")
    tokens.add_token("lee")
    _record()
    assert sorted(handover.step_down(3)) == ["kim", "lee"]
    current = settings.load()
    assert current.role == "member" and current.head_epoch == 3 and current.models == ()
    assert not tokens.tokens_path().exists() and members.load() == {}
    assert tls.ca_pem() == ca
    assert handover.stepped_down_path().is_file()
    # The settings and the member link come first: in the stale-head path the
    # gateway uninstalls itself, which ends that process.
    assert head.log == ["member install", "gateway uninstall"]


def test_finish_step_down_turns_the_live_store_into_the_backup(head):
    DocumentStore().create_collection("company")
    handover.step_down(3)
    handover.finish_step_down()
    assert not rag_backup.live_path().exists() and rag_backup.backup_path().is_file()
    assert not handover.stepped_down_path().exists()
    handover.finish_step_down()  # nothing left to do


def _as_target(monkeypatch):
    settings.save(settings.ClusterSettings("member", NS, 52440))
    monkeypatch.setattr(member_link, "local_ipv4s", lambda run=None: ["169.254.0.1"])


def _reply(rag_marker=None, epoch=3):
    return member_link.HeadReply("me", "169.254.0.4:52440", 2, {"head": True, "epoch": 2, "handover": {
        "to": "t", "epoch": epoch, "rag_marker": rag_marker,
        "settings": {"models": [{"model": "mlx-community/x", "sharding": "Tensor", "macs": 2}],
                     "known_nodes": ["node4", "node1"], "gateway_port": 52440}}})


def test_promote_adopts_backup_and_becomes_head(head, monkeypatch, tmp_path):
    _as_target(monkeypatch)
    fetched = []

    def fetch(address, key, own):
        fetched.append((address, own))
        rag_backup._write_meta("m1", "0" * 64)
        rag_backup.backup_path().write_bytes(b"db")

    monkeypatch.setattr(member_link, "fetch_snapshot", fetch)
    handover.promote(_reply("m1"), "t")
    assert fetched == [("169.254.0.4:52440", "t")]
    current = settings.load()
    assert current.role == "head" and current.head_epoch == 3 and current.known_nodes == ("node4", "node1")
    assert current.models == (settings.KeptModel("mlx-community/x", "Tensor", 2),)
    assert rag_backup.live_path().read_bytes() == b"db"
    assert head.log == ["gateway install", "member uninstall"]


def test_promote_refuses_stale_snapshot(head, monkeypatch):
    _as_target(monkeypatch)

    def fetch(address, key, own):
        rag_backup._write_meta("m0", "0" * 64)
        rag_backup.backup_path().write_bytes(b"db")

    monkeypatch.setattr(member_link, "fetch_snapshot", fetch)
    with pytest.raises(ClusterError, match="documents changed"):
        handover.promote(_reply("m1"), "t")
    assert settings.load().role == "member" and head.log == []


def test_promote_without_documents(head, monkeypatch):
    _as_target(monkeypatch)
    handover.promote(_reply(None), "t")
    assert settings.load().role == "head" and not rag_backup.live_path().exists()


def test_stale_head_steps_down_for_higher_epoch(head, monkeypatch):
    monkeypatch.setattr(member_link, "find_head",
                        lambda raw, key, own, *, report: member_link.HeadReply("t", "169.254.0.1:52440", 5, {}))
    assert handover.check_for_newer_head() is True
    assert settings.load().role == "member" and settings.load().head_epoch == 5
    assert head.log == ["member install", "gateway uninstall"]


@pytest.mark.parametrize("epoch", [1, 2])
def test_stale_head_ignores_equal_or_lower_epoch(head, monkeypatch, epoch):
    monkeypatch.setattr(member_link, "find_head",
                        lambda raw, key, own, *, report: member_link.HeadReply("t", "x:1", epoch, {}))
    assert handover.check_for_newer_head() is False
    assert settings.load().role == "head" and head.log == []


def test_stale_head_loop_stops(head, monkeypatch):
    monkeypatch.setattr(handover, "check_for_newer_head", lambda: False)
    stop = threading.Event()
    stop.set()
    handover.stale_head_loop(stop)  # returns at once once stopped


# -- the command -------------------------------------------------------------

@pytest.fixture
def command(head, monkeypatch):
    monkeypatch.setattr(commands, "require_macos", lambda: None)
    _record()
    monkeypatch.setattr(handover, "WAIT_SECONDS", 0)
    return head


def test_cli_handover_requires_yes_without_tty(command):
    result = runner.invoke(cli.app, ["cluster", "handover", "node1"])
    assert result.exit_code == 1 and "Pass --yes" in result.output
    assert members.pending_handover() is None


def test_cli_handover_prints_preflight_problems(command):
    _record(prepared=False)
    result = runner.invoke(cli.app, ["cluster", "handover", "node1", "--yes"])
    assert result.exit_code == 1 and "omm cluster join" in result.output


def test_handover_times_out_and_stays_head(command, monkeypatch):
    monkeypatch.setattr(handover, "wait_for_new_head", lambda node, epoch, *, deadline, sleep=None: False)
    result = runner.invoke(cli.app, ["cluster", "handover", "node1", "--yes"])
    assert result.exit_code == 1
    assert "node1 did not take over within 3 minutes; this Mac is still the head" in result.output
    assert settings.load().role == "head" and members.pending_handover() is None


def test_handover_rerun_after_expiry(command, monkeypatch):
    members.start_handover("t", 3, {}, None, now=datetime.now(timezone.utc) - timedelta(minutes=10))
    monkeypatch.setattr(handover, "wait_for_new_head", lambda node, epoch, *, deadline, sleep=None: True)
    tokens.add_token("kim")
    result = runner.invoke(cli.app, ["cluster", "handover", "node1", "--yes"])
    assert result.exit_code == 0, result.output
    assert "node1 is now the cluster head. This Mac is a member." in result.output
    assert "omm cluster token add <name>      for each employee: kim" in result.output
    assert "omm rag remote set https://192.0.2.1:52440" in result.output
    assert settings.load().role == "member" and settings.load().head_epoch == 3


def test_cli_handover_help_lists_yes():
    result = runner.invoke(cli.app, ["cluster", "handover", "--help"])
    assert result.exit_code == 0 and "--yes" in result.output
