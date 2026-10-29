"""`omm update` on a cluster head restarts its gateway and warns that members follow."""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.cluster import launchd, settings

runner = CliRunner()
NS = "omm-" + "a" * 32


@pytest.fixture
def updated(monkeypatch, isolated_omm_home):
    """`omm update` with every step that would touch git, pipx or the real SRC_DIR stubbed."""
    commits = iter(["a" * 40, "b" * 40, "b" * 40])
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "a" * 40)
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda: True)
    monkeypatch.setattr(cli, "_installed_commit", lambda: next(commits, "b" * 40))
    monkeypatch.setattr(cli, "_remote_head_commit", lambda branch: "b" * 40)
    monkeypatch.setattr(cli.version_check, "record", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_perform_update", lambda branch, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    monkeypatch.setattr(cli, "_maybe_prompt_data_sharing_after_update", lambda: None)
    restarts = []
    monkeypatch.setattr(launchd, "restart_agent", lambda label, **k: restarts.append(label))
    return restarts


def test_update_on_head_restarts_gateway_and_warns(updated):
    settings.save(settings.ClusterSettings("head", NS, 52440, known_nodes=("node1", "node3", "node4")))
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 0, result.output
    assert updated == ["com.omm.cluster-gateway"]
    assert "2 other Mac(s) in the cluster will update to this version within a few minutes" in result.output
    assert "Cluster models may stop answering briefly" in result.output


def test_update_on_member_prints_no_cluster_warning(updated):
    settings.save(settings.ClusterSettings("member", NS, 52440, known_nodes=("node1", "node3")))
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 0, result.output
    assert updated == [] and "other Mac(s)" not in result.output


def test_update_on_lone_head_restarts_without_warning(updated):
    settings.save(settings.ClusterSettings("head", NS, 52440, known_nodes=("node1",)))
    result = runner.invoke(cli.app, ["update"])
    assert updated == ["com.omm.cluster-gateway"] and "other Mac(s)" not in result.output
