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
        self.exo_restarts = 0
        self.exo_installs = 0
        self.keep_awake_calls = 0
        self.netconfig_calls = []
        self.power_restores = 0
        self.idle_sleep = 0
        self.appfw_allowed = []
        self.appfw_removes = 0
        self.appfw_missing = []
        self.firewall_state = "active"
        self.gateway_running = True
        self.exo_up = True
        self.rdma = "enabled"
        self.state = json.loads((FIX / "state.json").read_text(encoding="utf-8"))
        self.previews = json.loads((FIX / "previews.json").read_text(encoding="utf-8"))
        self.models = json.loads((FIX / "models.json").read_text(encoding="utf-8"))
        self.created = []
        self.deleted = []
        self.load_progress = []
        self.model_ready = True

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

    def await_instance(self, model_id, stall_seconds, on_progress=None, **_k):
        for progress in self.fakes.load_progress:
            on_progress(progress)
        return self.fakes.model_ready

    def delete_instance(self, instance_id):
        self.fakes.deleted.append(instance_id)


@pytest.fixture
def cluster_fakes(monkeypatch, isolated_omm_home):
    from omm.cluster import appfw, commands, exo_service, firewall, netconfig, power, preflight, service

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
    monkeypatch.setattr(exo_service, "restart", lambda **k: setattr(fakes, "exo_restarts", fakes.exo_restarts + 1))
    monkeypatch.setattr(firewall, "apply", lambda *, gateway_port, **k: fakes.firewall_apply_calls.append(gateway_port))
    monkeypatch.setattr(firewall, "remove", lambda **k: setattr(fakes, "firewall_removed", fakes.firewall_removed + 1))
    monkeypatch.setattr(firewall, "status", lambda *, gateway_port, **k: fakes.firewall_state)
    monkeypatch.setattr(appfw, "allow", lambda binaries, **k: fakes.appfw_allowed.append([p.name for p in binaries]))
    monkeypatch.setattr(appfw, "remove", lambda **k: setattr(fakes, "appfw_removes", fakes.appfw_removes + 1))
    monkeypatch.setattr(appfw, "not_allowed", lambda binaries, **k: list(fakes.appfw_missing))
    monkeypatch.setattr(appfw, "gateway_binary", lambda: Path("/fw/Python"))
    monkeypatch.setattr(netconfig, "apply", lambda **k: fakes.netconfig_calls.append("apply"))
    monkeypatch.setattr(netconfig, "wait_for_addresses", lambda out, **k: fakes.netconfig_calls.append("wait"))
    monkeypatch.setattr(netconfig, "restore", lambda **k: fakes.netconfig_calls.append("restore"))
    monkeypatch.setattr(power, "keep_awake", lambda **k: setattr(fakes, "keep_awake_calls", fakes.keep_awake_calls + 1))
    monkeypatch.setattr(power, "restore", lambda **k: setattr(fakes, "power_restores", fakes.power_restores + 1))
    monkeypatch.setattr(power, "current_idle_sleep_minutes", lambda **k: fakes.idle_sleep)
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


def test_init_and_join_tell_the_user_about_the_on_screen_allow_prompt(cluster_fakes):
    head = runner.invoke(cli.app, ["cluster", "init"])
    assert head.exit_code == 0, head.output
    assert "click Allow" in head.output
    left = runner.invoke(cli.app, ["cluster", "leave", "--yes"])
    assert left.exit_code == 0, left.output
    member = runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    assert member.exit_code == 0, member.output
    assert "click Allow" in member.output


def test_init_and_join_set_up_the_thunderbolt_network_before_exo_and_the_firewall(cluster_fakes, monkeypatch):
    from omm.cluster import exo_service, firewall
    from omm.cluster import netconfig as netconfig_module

    order = []
    monkeypatch.setattr(netconfig_module, "apply", lambda **k: order.append("netconfig.apply"))
    monkeypatch.setattr(exo_service, "install", lambda namespace, **k: order.append("exo"))
    monkeypatch.setattr(firewall, "apply", lambda *, gateway_port, **k: order.append("firewall"))
    result = runner.invoke(cli.app, ["cluster", "init"])
    assert result.exit_code == 0, result.output
    # exo needs the link-local addresses, and the pf rules list the Thunderbolt ports that exist afterwards.
    assert order == ["netconfig.apply", "exo", "firewall"]
    assert cluster_fakes.netconfig_calls == ["wait"]
    assert "set up the Thunderbolt network" in result.output


def test_join_sets_up_the_thunderbolt_network_too(cluster_fakes):
    result = runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    assert result.exit_code == 0, result.output
    assert cluster_fakes.netconfig_calls == ["apply", "wait"]


def test_leave_puts_the_thunderbolt_bridge_back(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.netconfig_calls.clear()
    result = runner.invoke(cli.app, ["cluster", "leave", "--yes"])
    assert result.exit_code == 0, result.output
    assert cluster_fakes.netconfig_calls == ["restore"]


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


def test_status_tells_what_to_do_when_known_nodes_are_missing(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    assert not any("omm cluster restart" in p for p in _status()["problems"])
    cluster_fakes.drop_node("mac-d")
    problems = _status()["problems"]
    assert sum("omm cluster restart" in p for p in problems) == 1


def test_restart_restarts_exo_on_this_mac(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"])
    result = runner.invoke(cli.app, ["cluster", "restart"])
    assert result.exit_code == 0, result.output
    assert cluster_fakes.exo_restarts == 1
    assert "omm cluster status" in result.output


def test_restart_outside_a_cluster_exits_2(cluster_fakes):
    result = runner.invoke(cli.app, ["cluster", "restart"])
    assert result.exit_code == 2
    assert cluster_fakes.exo_restarts == 0
    assert "omm cluster init or join" in " ".join(result.output.split())


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
    assert data["exo"] == {"reachable": False, "nodes": [], "instances": [], "links": []}
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


def test_model_reports_download_progress_in_five_percent_steps(cluster_fakes):
    gib = 1024**3
    cluster_fakes.load_progress = [
        exo_client.LoadProgress("downloading", 1 * gib, 100 * gib, 3 * gib),
        exo_client.LoadProgress("downloading", 2 * gib, 100 * gib, 6 * gib),
        exo_client.LoadProgress("downloading", 30 * gib, 100 * gib, 90 * gib),
        exo_client.LoadProgress("loading"),
        exo_client.LoadProgress("loading"),
    ]
    runner.invoke(cli.app, ["cluster", "init"])
    result = runner.invoke(cli.app, ["cluster", "model", "mlx-community/Llama-3.2-1B-Instruct-4bit"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "1% (1.0 of 100.0 GiB" in text
    assert "2% (2.0 of" not in text
    assert "30% (30.0 of 100.0 GiB" in text
    assert text.count("into memory") == 1


def test_model_says_when_a_mac_failed_to_download(cluster_fakes):
    cluster_fakes.load_progress = [exo_client.LoadProgress("downloading", 0, 0, 0, failed="disk full")]
    runner.invoke(cli.app, ["cluster", "init"])
    result = runner.invoke(cli.app, ["cluster", "model", "mlx-community/Llama-3.2-1B-Instruct-4bit"])
    assert result.exit_code == 0, result.output
    assert "disk full" in " ".join(result.output.split())


def test_model_exits_1_when_loading_stalls(cluster_fakes):
    gib = 1024**3
    cluster_fakes.model_ready = False
    cluster_fakes.load_progress = [exo_client.LoadProgress("downloading", 5 * gib, 100 * gib, 15 * gib)]
    runner.invoke(cli.app, ["cluster", "init"])
    result = runner.invoke(cli.app, ["cluster", "model", "mlx-community/Llama-3.2-1B-Instruct-4bit"])
    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "no progress for 15 minutes" in text
    assert "Downloading mlx-community/Llama-3.2-1B-Instruct-4bit: 5%" in text


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


def test_interrupted_first_init_keeps_the_namespace(cluster_fakes, monkeypatch):
    from omm.cluster import firewall

    def interrupted(**k):
        raise KeyboardInterrupt

    monkeypatch.setattr(firewall, "apply", interrupted)
    runner.invoke(cli.app, ["cluster", "init"])
    first = settings.load().namespace
    monkeypatch.setattr(firewall, "apply", lambda *, gateway_port, **k: None)
    result = runner.invoke(cli.app, ["cluster", "init"])
    assert result.exit_code == 0
    assert settings.load().namespace == first
    assert cluster_fakes.exo_service_installs == [first, first]


def test_leave_stops_when_firewall_removal_fails(cluster_fakes, monkeypatch):
    from omm.cluster import firewall
    from omm.cluster.errors import ClusterError

    runner.invoke(cli.app, ["cluster", "init"])
    monkeypatch.setattr(firewall, "remove", lambda **k: (_ for _ in ()).throw(ClusterError("could not remove the cluster firewall rules")))
    result = runner.invoke(cli.app, ["cluster", "leave", "--yes"])
    assert result.exit_code == 1
    assert settings.load() is not None and "left the cluster" not in result.output


def test_status_reports_other_exo_errors_as_problems(cluster_fakes, monkeypatch):
    from omm.cluster.errors import ClusterError

    runner.invoke(cli.app, ["cluster", "init"])
    monkeypatch.setattr(_FakeExoClient, "state", lambda self: (_ for _ in ()).throw(ClusterError("exo request GET /state failed: HTTP 500")))
    data = _status()
    assert data["exo"]["reachable"] is False
    assert "exo request GET /state failed: HTTP 500" in data["problems"]
    assert data["firewall"] == "active"


def test_status_lists_links_and_flags_missing_thunderbolt_link(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    data = _status()
    assert len(data["exo"]["links"]) == 12
    assert set(data["exo"]["links"][0]) == {"from", "to", "kind"}
    ids = {v["friendlyName"]: k for k, v in cluster_fakes.state["nodeIdentities"].items()}
    conns = cluster_fakes.state["topology"]["connections"]
    conns[ids["mac-a"]].pop(ids["mac-b"])
    conns[ids["mac-b"]].pop(ids["mac-a"])
    assert "no Thunderbolt link between mac-a and mac-b" in _status()["problems"]


def test_status_flags_thunderbolt_bridge_enabled(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    node = next(iter(cluster_fakes.state["nodeThunderboltBridge"]))
    cluster_fakes.state["nodeThunderboltBridge"][node] = {"enabled": True, "exists": True, "serviceName": "Thunderbolt Bridge"}
    assert "Thunderbolt Bridge is on for mac-a; run omm cluster init (head) or join on it again" in _status()["problems"]


def test_init_and_join_turn_idle_sleep_off(cluster_fakes):
    assert runner.invoke(cli.app, ["cluster", "init"]).exit_code == 0
    assert cluster_fakes.keep_awake_calls == 1
    runner.invoke(cli.app, ["cluster", "leave", "--yes"])
    assert runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"]).exit_code == 0
    assert cluster_fakes.keep_awake_calls == 2


def test_leave_restores_idle_sleep(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    assert runner.invoke(cli.app, ["cluster", "leave", "--yes"]).exit_code == 0
    assert cluster_fakes.power_restores == 1


def test_status_flags_idle_sleep(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.idle_sleep = 1
    data = _status()
    assert "this Mac sleeps after 1 min idle; run omm cluster init again" in data["problems"]


def test_init_lets_exo_and_gateway_through_the_macos_firewall_join_only_exo(cluster_fakes):
    assert runner.invoke(cli.app, ["cluster", "init"]).exit_code == 0
    runner.invoke(cli.app, ["cluster", "leave", "--yes"])
    assert runner.invoke(cli.app, ["cluster", "join", "omm-0a1b2c3d"]).exit_code == 0
    assert cluster_fakes.appfw_allowed == [["exo", "Python"], ["exo"]]
    assert cluster_fakes.appfw_removes == 1


def test_status_flags_binaries_the_macos_firewall_holds(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.appfw_missing = ["/fw/Python"]
    data = _status()
    assert "the macOS firewall holds incoming connections to /fw/Python; run omm cluster init again" in data["problems"]


def _ids(fakes, *names):
    return [fakes._node_id(n) for n in names]


def test_status_reports_a_thunderbolt_bridge_loop_of_three_or_more_macs(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.state["thunderboltBridgeCycles"] = [_ids(cluster_fakes, "mac-a", "mac-b", "mac-c")]
    problems = _status()["problems"]
    assert ("Thunderbolt Bridge loop between mac-a, mac-b, mac-c (packet storm); "
            "unplug one of those cables or run omm cluster init/join again on each of them" in problems)


def test_status_ignores_two_node_bridge_cycles(cluster_fakes):
    # exo lists every bidirectional link as a 2-node cycle; only 3+ nodes loop.
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.state["thunderboltBridgeCycles"] = [_ids(cluster_fakes, "mac-a", "mac-b")]
    assert not any("loop" in p for p in _status()["problems"])


def test_status_shows_unknown_memory_for_a_node_without_a_memory_report(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.state["nodeMemory"].pop(cluster_fakes._node_id("mac-a"))
    node = next(n for n in _status()["exo"]["nodes"] if n["name"] == "mac-a")
    assert node["memory_total_bytes"] is None and node["memory_available_bytes"] is None
    plain = runner.invoke(cli.app, ["cluster", "status"]).output
    assert "mac-a: memory not reported yet" in plain
    assert "0/0 GiB" not in plain


def test_model_recommendation_skips_nodes_without_a_memory_report(cluster_fakes):
    runner.invoke(cli.app, ["cluster", "init"])
    cluster_fakes.state["nodeMemory"].pop(cluster_fakes._node_id("mac-a"))
    assert runner.invoke(cli.app, ["cluster", "model"]).exit_code == 0


def test_cluster_errors_are_written_to_the_omm_log(cluster_fakes, caplog):
    cluster_fakes.exo_down()
    with caplog.at_level("ERROR", logger="omm.cluster"):
        result = runner.invoke(cli.app, ["cluster", "model", "some/model"])
    assert result.exit_code == 1
    assert "exo is not running on this Mac" in caplog.text
    with caplog.at_level("ERROR", logger="omm.cluster"):
        result = runner.invoke(cli.app, ["cluster", "join", "not-a-namespace"])
    assert result.exit_code == 2
    assert "the cluster name looks like" in caplog.text
