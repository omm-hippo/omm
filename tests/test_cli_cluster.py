"""`omm cluster` command group (#503). Every system touch point (brew, sudo,
pfctl, launchctl, exo) is faked; the fixtures are real exo 1.0.71 shapes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import platform

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.cluster import exo_client, settings, tokens

FIX = Path(__file__).parent / "fixtures" / "exo"
runner = CliRunner()


class _Fakes:
    def __init__(self):
        self.firewall_apply_calls = []
        self.firewall_removed = 0
        self.service_install_calls = 0
        self.service_uninstall_calls = 0
        self.exo_service_installs = []
        self.exo_service_uninstalls = 0
        self.exo_installs = 0
        self.firewall_state = "active"
        self.gateway_running = True
        self.exo_up = True
        self.rdma = "enabled"
        self.state = json.loads((FIX / "state.json").read_text(encoding="utf-8"))
        self.previews = json.loads((FIX / "previews.json").read_text(encoding="utf-8"))
        self.models = json.loads((FIX / "models.json").read_text(encoding="utf-8"))
        self.created = []
        self.deleted = []

    def _node_id(self, name):
        return next(k for k, v in self.state["nodeIdentities"].items() if v["friendlyName"] == name)

    def drop_node(self, name):
        node_id = self._node_id(name)
        for key in ("nodeIdentities", "nodeMemory", "nodeRdmaCtl"):
            self.state[key].pop(node_id, None)
        self.state["topology"]["nodes"].remove(node_id)

    def set_os(self, name, version):
        self.state["nodeIdentities"][self._node_id(name)]["osVersion"] = version

    def exo_down(self):
        self.exo_up = False

    def previews_all_error(self):
        for row in self.previews["previews"]:
            row["error"] = "not enough memory"


class _FakeExoClient:
    fakes: _Fakes

    def __init__(self, base_url="http://127.0.0.1:52415"):
        pass

    def _guard(self):
        if not self.fakes.exo_up:
            raise exo_client.ExoUnavailable()

    def state(self):
        self._guard()
        return exo_client.parse_state(copy.deepcopy(self.fakes.state))

    def models(self):
        self._guard()
        return exo_client.parse_models(self.fakes.models)

    def previews(self, model_id):
        self._guard()
        rows = exo_client.parse_previews(self.fakes.previews)
        return [exo_client.Placement(model_id, r.sharding, r.instance, r.memory_delta_by_node, r.error) for r in rows]

    def create_instance(self, placement):
        self._guard()
        self.fakes.created.append(placement)
        return "cmd-1"

    def await_instance(self, model_id, timeout_seconds, **_k):
        return True

    def delete_instance(self, instance_id):
        self.fakes.deleted.append(instance_id)


@pytest.fixture
def cluster_fakes(monkeypatch, isolated_omm_home):
    from omm.cluster import commands, exo_service, firewall, preflight, service

    fakes = _Fakes()
    _FakeExoClient.fakes = fakes
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(preflight, "require_supported_macos", lambda: None)
    monkeypatch.setattr(preflight, "exo_installed", lambda: fakes.exo_installs > 0 or True)
    monkeypatch.setattr(preflight, "install_exo", lambda on_output, **k: setattr(fakes, "exo_installs", fakes.exo_installs + 1))
    monkeypatch.setattr(preflight, "stage_runtime", lambda **k: preflight.runtime_dir())
    monkeypatch.setattr(preflight, "rdma_status", lambda **k: fakes.rdma)
    monkeypatch.setattr(exo_service, "install", lambda namespace, **k: fakes.exo_service_installs.append(namespace))
    monkeypatch.setattr(exo_service, "uninstall", lambda **k: setattr(fakes, "exo_service_uninstalls", fakes.exo_service_uninstalls + 1))
    monkeypatch.setattr(firewall, "apply", lambda *, gateway_port, **k: fakes.firewall_apply_calls.append(gateway_port))
    monkeypatch.setattr(firewall, "remove", lambda **k: setattr(fakes, "firewall_removed", fakes.firewall_removed + 1))
    monkeypatch.setattr(firewall, "status", lambda *, gateway_port, **k: fakes.firewall_state)
    monkeypatch.setattr(service, "install", lambda **k: setattr(fakes, "service_install_calls", fakes.service_install_calls + 1))
    monkeypatch.setattr(service, "uninstall", lambda **k: setattr(fakes, "service_uninstall_calls", fakes.service_uninstall_calls + 1))
    monkeypatch.setattr(service, "is_running", lambda **k: fakes.gateway_running)
    monkeypatch.setattr(commands, "ExoClient", _FakeExoClient)
    monkeypatch.setattr(commands, "lan_ipv4", lambda: "192.0.2.10")
    return fakes


def _status(*extra):
    result = runner.invoke(cli.app, ["cluster", "status", "--json", *extra])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_init_twice_reuses_namespace_and_reapplies(cluster_fakes):
    first = runner.invoke(cli.app, ["cluster", "init"])
    second = runner.invoke(cli.app, ["cluster", "init"])
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    loaded = settings.load()
    assert loaded.role == "head" and settings.NAMESPACE_RE.match(loaded.namespace)
    for result in (first, second):
        assert f"omm cluster join {loaded.namespace}" in result.output
        assert "omm cluster token add <name>" in result.output
    assert cluster_fakes.firewall_apply_calls == [52440, 52440]
    assert cluster_fakes.service_install_calls == 2
    assert cluster_fakes.exo_service_installs == [loaded.namespace, loaded.namespace]


def test_init_prints_rdma_guide_when_disabled(cluster_fakes):
    cluster_fakes.rdma = "disabled"
    result = runner.invoke(cli.app, ["cluster", "init"])
    assert result.exit_code == 0
    assert "rdma_ctl enable" in result.output


def test_join_rejects_bad_namespace(cluster_fakes):
    result = runner.invoke(cli.app, ["cluster", "join", "anything"])
    assert result.exit_code == 2
    assert settings.load() is None


def test_join_sets_member_without_gateway(cluster_fakes):
    result = runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    assert result.exit_code == 0, result.output
    assert settings.load().role == "member" and settings.load().namespace == "omm-0a1b2c3d"
    assert cluster_fakes.firewall_apply_calls == [None]
    assert cluster_fakes.service_install_calls == 0
    assert cluster_fakes.exo_service_installs == ["omm-0a1b2c3d"]


def test_init_on_member_is_refused(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    result = runner.invoke(cli.app, ["cluster", "init"])
    assert result.exit_code == 2 and "omm cluster leave" in result.output


def test_status_json_healthy_shape(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    data = _status()
    assert set(data) == {"role", "namespace", "exo", "firewall", "gateway", "problems"}
    assert data["role"] == "head" and data["firewall"] == "active" and data["gateway"] == "running"
    assert data["exo"]["reachable"] is True and len(data["exo"]["nodes"]) == 4
    assert set(data["exo"]["nodes"][0]) == {"name", "memory_total_bytes", "memory_available_bytes", "rdma", "os_version"}
    assert data["problems"] == []


def test_status_json_lists_missing_node_and_os_mismatch(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    _status()
    cluster_fakes.drop_node("mac-d")
    cluster_fakes.set_os("mac-b", "26.3")
    data = _status()
    assert "node mac-d missing (seen before)" in data["problems"]
    assert any(p.startswith("macOS versions differ:") for p in data["problems"])


def test_status_flags_rdma_firewall_and_gateway(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    node = next(iter(cluster_fakes.state["nodeRdmaCtl"]))
    cluster_fakes.state["nodeRdmaCtl"][node] = {"enabled": False}
    cluster_fakes.firewall_state = "drift"
    cluster_fakes.gateway_running = False
    data = _status()
    assert "RDMA disabled on mac-a" in data["problems"]
    assert "firewall rules are drift; run omm cluster init again" in data["problems"]
    assert "gateway is not running" in data["problems"]
    assert data["gateway"] == "stopped"


def test_status_when_exo_is_down_has_same_shape(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.exo_down()
    data = _status()
    assert data["exo"] == {"reachable": False, "nodes": [], "instances": []}
    assert "exo is not running on this Mac" in data["problems"]


def test_status_without_cluster(cluster_fakes):
    data = _status()
    assert data["role"] is None and data["gateway"] == "not_head"


def test_status_plain_output_mentions_nodes(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    result = runner.invoke(cli.app, ["cluster", "status"])
    assert result.exit_code == 0 and "mac-a" in result.output and "No problems found" in result.output


def test_model_without_argument_lists_candidates(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    result = runner.invoke(cli.app, ["cluster", "model"])
    assert result.exit_code == 0, result.output
    assert "mlx-community/" in result.output


def test_model_loads_and_prints_gateway_url(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    result = runner.invoke(cli.app, ["cluster", "model", "mlx-community/Llama-3.2-1B-Instruct-4bit"])
    assert result.exit_code == 0, result.output
    assert cluster_fakes.created and cluster_fakes.created[0].sharding == "Pipeline"
    assert "Serving mlx-community/Llama-3.2-1B-Instruct-4bit at http://192.0.2.10:52440" in result.output


def test_model_without_fit_exits_1(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.previews_all_error()
    result = runner.invoke(cli.app, ["cluster", "model", "mlx-community/huge"])
    assert result.exit_code == 1 and "does not fit in this cluster's free memory" in result.output


def test_model_unload_deletes_matching_instances(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    one = json.loads((FIX / "state-1node.json").read_text(encoding="utf-8"))
    cluster_fakes.state["instances"] = one["instances"]
    result = runner.invoke(cli.app, ["cluster", "model", "mlx-community/Llama-3.2-1B-Instruct-4bit", "--unload"])
    assert result.exit_code == 0, result.output
    assert cluster_fakes.deleted == list(one["instances"])


def test_model_when_exo_down_exits_1(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.exo_down()
    result = runner.invoke(cli.app, ["cluster", "model"])
    assert result.exit_code == 1 and "exo is not running on this Mac" in result.output


def test_token_add_list_revoke_on_head(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    added = runner.invoke(cli.app, ["cluster", "token", "add", "kim"])
    assert added.exit_code == 0 and "Store it now - omm cannot show it again." in added.output
    token = next(word for word in added.output.split() if word.startswith("omm_"))
    assert tokens.verify(token) == "kim"
    listed = runner.invoke(cli.app, ["cluster", "token", "list"])
    assert "kim" in listed.output and token not in listed.output
    assert runner.invoke(cli.app, ["cluster", "token", "revoke", "kim"]).exit_code == 0
    assert runner.invoke(cli.app, ["cluster", "token", "revoke", "kim"]).exit_code == 1


def test_token_commands_refused_on_member(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    result = runner.invoke(cli.app, ["cluster", "token", "add", "kim"])
    assert result.exit_code == 2 and "tokens are managed on the head Mac" in result.output


def test_token_list_with_corrupt_file_exits_1(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    tokens.tokens_path().parent.mkdir(parents=True, exist_ok=True)
    tokens.tokens_path().write_text("{broken", encoding="utf-8")
    result = runner.invoke(cli.app, ["cluster", "token", "list"])
    assert result.exit_code == 1 and "tokens.json" in result.output


def test_leave_on_head_removes_everything_but_tokens(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    tokens.add_token("kim")
    result = runner.invoke(cli.app, ["cluster", "leave", "--yes"])
    assert result.exit_code == 0, result.output
    assert settings.load() is None and tokens.tokens_path().exists()
    assert cluster_fakes.firewall_removed == 1 and cluster_fakes.service_uninstall_calls == 1
    assert cluster_fakes.exo_service_uninstalls == 1
    assert "exo itself stays installed" in result.output


def test_leave_on_member_keeps_gateway_untouched(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    assert runner.invoke(cli.app, ["cluster", "leave", "--yes"]).exit_code == 0
    assert cluster_fakes.service_uninstall_calls == 0


def test_non_macos_exits_2(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    result = runner.invoke(cli.app, ["cluster", "status"])
    assert result.exit_code == 2
    assert "omm cluster runs on the Macs that form the cluster" in result.output
