"""The member side of the member link: finding the head, proofs, one poll cycle (member_link.py)."""

from __future__ import annotations

from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import threading
import types

import pytest

from omm import cli
from omm.cluster import (document_search, exo_service, gateway, launchd, member_auth, member_link, member_service,
                         rag_backup, settings, version_follow)
from omm.cluster.errors import ClusterError
from omm.documents import DocumentStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="omm cluster is macOS-only")

NS = "omm-" + "a" * 32
KEY = member_auth.member_key(NS)
HEAD_VERSION = {"commit": "b" * 40, "branch": "main", "git": True}


def _state(networks: dict) -> dict:
    return {"nodeNetwork": {node: {"interfaces": [{"ipAddress": a} for a in addresses]}
                            for node, addresses in networks.items()}}


def test_ordered_addresses_puts_thunderbolt_first():
    raw = _state({"A": ["192.0.2.1", "169.254.1.1"], "B": ["192.0.2.3", "169.254.9.9"]})
    assert member_link.ordered_addresses(raw, "A") == [("B", "169.254.9.9"), ("B", "192.0.2.3")]


def test_own_node_id_matches_local_address():
    raw = _state({"A": ["192.0.2.1", "169.254.1.1"], "B": ["192.0.2.3"]})
    assert member_link.own_node_id(raw, ["169.254.1.1"]) == "A"


def test_own_node_id_none_when_ambiguous_or_unknown():
    raw = _state({"A": ["192.0.2.1"], "B": ["192.0.2.1"]})
    assert member_link.own_node_id(raw, ["192.0.2.1"]) is None
    assert member_link.own_node_id(raw, ["10.0.0.9"]) is None


def test_local_ipv4s_keeps_link_local():
    text = "lo0: flags\n\tinet 127.0.0.1 netmask\nen0:\n\tinet 192.0.2.5 netmask\nen9:\n\tinet 169.254.3.4 netmask\n"
    run = lambda *a, **k: subprocess.CompletedProcess(a, 0, text, "")
    assert member_link.local_ipv4s(run=run) == ["192.0.2.5", "169.254.3.4"]


@pytest.fixture
def head(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(version_follow, "local_version", lambda: dict(HEAD_VERSION))
    settings.save(settings.ClusterSettings("head", NS, gateway.GATEWAY_PORT, head_epoch=2))
    servers = []

    def start(key=KEY):
        srv = gateway.GatewayServer("127.0.0.1", 0, upstream="http://127.0.0.1:9", member_key=key)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_request_returns_a_proven_reply(head):
    srv = head()
    status, body, headers = member_link.request(f"127.0.0.1:{srv.server_address[1]}", "POST", "/omm/member/poll",
                                                KEY, "12D3me", body=b"{}")
    assert status == 200 and b'"epoch": 2' in body


def test_member_rejects_unproven_reply(head):
    srv = head(key=member_auth.member_key("omm-" + "b" * 32))
    with pytest.raises(ClusterError, match="not proven"):
        member_link.request(f"127.0.0.1:{srv.server_address[1]}", "POST", "/omm/member/poll", KEY, "12D3me",
                            body=b"{}")


def test_fetch_snapshot_installs_backup(head):
    srv = head()
    DocumentStore().create_collection("company")
    marker = rag_backup.store_marker()
    member_link.fetch_snapshot(f"127.0.0.1:{srv.server_address[1]}", KEY, "12D3me")
    assert rag_backup.backup_marker() == marker
    assert rag_backup.backup_path().is_file()


def test_find_head_prefers_higher_epoch(monkeypatch):
    raw = _state({"me": ["169.254.0.1"], "old": ["169.254.0.2"], "new": ["169.254.0.3"], "plain": ["169.254.0.4"]})
    replies = {"169.254.0.2": {"head": True, "epoch": 3}, "169.254.0.3": {"head": True, "epoch": 4}}

    def fake(address, method, path, key, own_node, *, body=None, timeout=5):
        if address.split(":")[0] not in replies:
            raise ClusterError("no gateway")
        import json
        return 200, json.dumps(replies[address.split(":")[0]]).encode(), {}

    monkeypatch.setattr(member_link, "request", fake)
    found = member_link.find_head(raw, KEY, "me", report={})
    assert (found.node_id, found.epoch) == ("new", 4)
    assert found.address == "169.254.0.3:52440"


def test_find_head_none_without_heads(monkeypatch):
    monkeypatch.setattr(member_link, "request", lambda *a, **k: (_ for _ in ()).throw(ClusterError("x")))
    assert member_link.find_head(_state({"me": ["1.1.1.1"], "b": ["1.1.1.2"]}), KEY, "me", report={}) is None


@pytest.fixture
def cycle(isolated_omm_home, monkeypatch):
    settings.save(settings.ClusterSettings("member", NS, gateway.GATEWAY_PORT))
    seen = types.SimpleNamespace(applied=[], fetched=[], promoted=[], finished=0, exo=[])
    reply = member_link.HeadReply("head", "169.254.0.2:52440", 2,
                                  {"head": True, "epoch": 2, "version": HEAD_VERSION, "rag_marker": None,
                                   "handover": None})
    seen.reply = reply
    monkeypatch.setattr(member_link, "_raw_state", lambda: _state({"me": ["169.254.0.1"], "head": ["169.254.0.2"]}))
    monkeypatch.setattr(member_link, "local_ipv4s", lambda run=None: ["169.254.0.1"])
    monkeypatch.setattr(member_link, "report", lambda: {"version_state": "same"})
    monkeypatch.setattr(member_link, "find_head", lambda raw, key, own, *, report: seen.reply)
    monkeypatch.setattr(version_follow, "apply", lambda v: seen.applied.append(v) or seen.result)
    seen.result = ("same", None)
    monkeypatch.setattr(member_link, "fetch_snapshot", lambda address, key, own: seen.fetched.append(address))
    monkeypatch.setattr(exo_service, "install", lambda ns, **kw: seen.exo.append((ns, kw)))
    monkeypatch.setattr(member_service, "refresh", lambda **k: False)
    fake = types.ModuleType("omm.cluster.handover")
    fake.promote = lambda r, own: seen.promoted.append((r, own))
    fake.finish_step_down = lambda: setattr(seen, "finished", seen.finished + 1)
    monkeypatch.setitem(sys.modules, "omm.cluster.handover", fake)
    import omm.cluster as package
    monkeypatch.setattr(package, "handover", fake, raising=False)
    return seen


def test_run_once_updates_and_exits(cycle):
    cycle.result = ("updated", None)
    assert member_link.run_once(first=True) == "updated"
    assert cycle.fetched == [] and cycle.exo == [(NS, {"keep_if_same": True})] and cycle.finished == 1


def test_run_once_pulls_snapshot_when_marker_differs(cycle):
    cycle.reply.body["rag_marker"] = "m2"
    assert member_link.run_once() == "same"
    assert cycle.fetched == ["169.254.0.2:52440"] and cycle.exo == []


def test_run_once_skips_snapshot_when_marker_same(cycle, monkeypatch):
    cycle.reply.body["rag_marker"] = "m2"
    monkeypatch.setattr(rag_backup, "backup_marker", lambda: "m2")
    member_link.run_once()
    assert cycle.fetched == []


def test_run_once_promotes_on_instruction(cycle):
    cycle.reply.body["handover"] = {"to": "me", "epoch": 3}
    assert member_link.run_once() == "promoted"
    assert cycle.promoted == [(cycle.reply, "me")] and cycle.applied == []


def test_run_once_waits_without_a_head(cycle):
    cycle.reply = None
    assert member_link.run_once() == "no_head"


def test_run_once_does_nothing_on_a_head(cycle):
    settings.save(settings.ClusterSettings("head", NS, gateway.GATEWAY_PORT))
    assert member_link.run_once() == "not_member"


def test_backup_embedding_settings_reads_the_backup(isolated_omm_home, tmp_path):
    assert rag_backup.backup_embedding_settings() is None
    store = DocumentStore()
    store.set_embedding_model("nomic-embed-text", "http://127.0.0.1:11434")
    file, sha, marker = rag_backup.snapshot()
    incoming = tmp_path / "in.sqlite3"
    shutil.copyfile(file, incoming)
    rag_backup.install_backup(incoming, sha256=sha, marker=marker)
    assert rag_backup.backup_embedding_settings() == {"model": "nomic-embed-text",
                                                      "base_url": "http://127.0.0.1:11434"}


def test_ready_for_needs_running_ollama_with_model(monkeypatch):
    monkeypatch.setattr(document_search, "_ollama", lambda m, u: ("running", True))
    assert document_search.ready_for("m", "http://127.0.0.1:11434")
    monkeypatch.setattr(document_search, "_ollama", lambda m, u: ("running", False))
    assert not document_search.ready_for("m", "http://127.0.0.1:11434")
    monkeypatch.setattr(document_search, "_ollama", lambda m, u: ("stopped", None))
    assert not document_search.ready_for("m", "http://127.0.0.1:11434")


def _ok(argv, **_kwargs):
    return subprocess.CompletedProcess(argv, 0, "state = running" if "print" in argv else "", "")


def test_member_service_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    member_service.install(run=_ok)
    data = plistlib.loads((tmp_path / "Library/LaunchAgents/com.omm.cluster-member.plist").read_bytes())
    assert data["Label"] == "com.omm.cluster-member"
    assert data["ProgramArguments"][-1] == "_cluster-member-run"
    assert data["KeepAlive"] is True and "OMM_HOME" in data["EnvironmentVariables"]
    assert member_service.is_installed()


def test_install_agent_keep_if_same_skips_bootout(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _ok(argv)

    launchd.install_agent("com.omm.test", {"RunAtLoad": True}, run=run)
    calls.clear()
    launchd.install_agent("com.omm.test", {"RunAtLoad": True}, run=run, keep_if_same=True)
    assert not any("bootout" in argv for argv in calls)
    launchd.install_agent("com.omm.test", {"RunAtLoad": False}, run=run, keep_if_same=True)
    assert any("bootout" in argv for argv in calls)


def test_cluster_member_run_is_internal():
    assert "_cluster-member-run" in cli._SKIP_UPDATE_CHECK_SUBCOMMANDS
    assert "_cluster-member-run" in cli._SKIP_QUEUED_UPLOAD_SUBCOMMANDS
    assert "_cluster-member-run" in cli._INTERNAL_SUBCOMMANDS


def test_uninstall_agent_removes_the_plist_before_bootout(tmp_path, monkeypatch):
    # A job that boots itself out is killed mid-call; its plist must already be gone.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launchd.install_agent("com.omm.test", {"RunAtLoad": True}, run=_ok)
    seen = []
    launchd.uninstall_agent("com.omm.test", run=lambda argv, **k: seen.append(launchd.plist_path("com.omm.test").exists())
                            or _ok(argv))
    assert seen == [False]


def test_find_head_breaks_epoch_ties_by_node_id(monkeypatch):
    import json

    raw = _state({"me": ["169.254.0.1"], "aaa": ["169.254.0.2"], "zzz": ["169.254.0.3"]})
    monkeypatch.setattr(member_link, "request",
                        lambda address, *a, **k: (200, json.dumps({"head": True, "epoch": 4}).encode(), {}))
    assert member_link.find_head(raw, KEY, "me", report={}).node_id == "zzz"


def test_member_plist_has_a_path_with_pipx(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    member_service.install(run=_ok)
    data = plistlib.loads((tmp_path / "Library/LaunchAgents/com.omm.cluster-member.plist").read_bytes())
    path = data["EnvironmentVariables"]["PATH"].split(":")
    assert str(Path(sys.executable).parent) in path and str(tmp_path / ".local/bin") in path and "/usr/bin" in path


def test_member_service_refresh_reloads_only_a_changed_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    spawned = []
    member_service.install(run=_ok)
    assert member_service.refresh(spawn=lambda argv: spawned.append(argv)) is False and spawned == []
    plist = tmp_path / "Library/LaunchAgents/com.omm.cluster-member.plist"
    plist.write_bytes(plist.read_bytes().replace(b"KeepAlive", b"KeepAlivX"))
    assert member_service.refresh(spawn=lambda argv: spawned.append(argv)) is True
    assert b"KeepAlivX" not in plist.read_bytes()
    assert len(spawned) == 1 and "bootstrap" in " ".join(spawned[0])


def test_run_once_refreshes_its_own_plist_on_the_first_cycle(cycle, monkeypatch):
    refreshed = []
    monkeypatch.setattr(member_service, "refresh", lambda **k: refreshed.append(True) or False)
    member_link.run_once(first=True)
    member_link.run_once()
    assert refreshed == [True]
