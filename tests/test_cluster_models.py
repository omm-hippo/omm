"""Cluster model inventory, install, uninstall, info, search and the memory
guard (`omm cluster install/list/...`). The exo client is faked; download
entries reuse the shapes captured from a live exo 1.0.71 (downloads-mixed.json)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import platform

import pytest

from omm.cluster import exo_client, models, settings
from omm.cluster.commands import UsageProblem
from omm.cluster.errors import ClusterError
from omm.memory_guard import GuardPolicy

FIX = Path(__file__).parent / "fixtures" / "exo"
GIB = 1024**3
SMALL = "mlx-community/Qwen3-0.6B-4bit"
OTHER = "mlx-community/Llama-3.2-1B-Instruct-4bit"


def _shard(model_id):
    rows = json.loads((FIX / "downloads-mixed.json").read_text(encoding="utf-8"))["downloads"]
    for wrapper in next(iter(rows.values())):
        body = next(iter(wrapper.values()))
        metadata = copy.deepcopy(body["shardMetadata"])
        card = next(iter(metadata.values()))["modelCard"]
        if card["modelId"] == model_id:
            return metadata
    metadata = copy.deepcopy(next(iter(next(iter(rows.values()))[0].values()))["shardMetadata"])
    next(iter(metadata.values()))["modelCard"]["modelId"] = model_id
    return metadata


class FakeExo:
    """Three Macs; `downloads[node][model] = (kind, done, total)`."""

    def __init__(self):
        self.nodes = {"n1": "mac-a", "n2": "mac-b", "n3": "mac-c"}
        self.free = {"n1": 40 * GIB, "n2": 40 * GIB, "n3": 40 * GIB}
        self.downloads = {node: {} for node in self.nodes}
        self.instances = {}  # instance id -> (model, ready)
        self.catalog = {SMALL: 327, OTHER: 696, "big/Huge-8bit": 200_000}
        self.fits = {SMALL: True, OTHER: True}
        self.started, self.deleted_downloads, self.deleted_instances, self.added, self.created = [], [], [], [], []
        self.complete_on_poll = True
        self.fail_on = None
        self.hits = [exo_client.SearchHit(SMALL, 40482, ("text-generation",)),
                     exo_client.SearchHit("someone/New-4bit", 12, ())]

    def raw_state(self):
        downloads = {}
        for node, items in self.downloads.items():
            rows = []
            for model_id in self.catalog:
                kind, done, total = items.get(model_id, ("Pending", 0, 0))
                body = {"nodeId": node, "shardMetadata": _shard(model_id)}
                if kind == "Ongoing":
                    body["downloadProgress"] = {"downloaded": {"inBytes": done}, "total": {"inBytes": total}}
                elif kind == "Failed":
                    body["errorMessage"] = "disk full"
                else:
                    body["downloaded"], body["total"] = {"inBytes": done}, {"inBytes": total}
                rows.append({f"Download{kind}": body})
            downloads[node] = rows
        instances, runners = {}, {}
        for instance_id, (model_id, ready) in self.instances.items():
            runner = f"r-{instance_id}"
            instances[instance_id] = {"MlxRingInstance": {"instanceId": instance_id, "shardAssignments": {
                "modelId": model_id, "nodeToRunner": {n: runner for n in self.nodes},
                "runnerToShard": {runner: {"PipelineShardMetadata": {}}}}}}
            runners[runner] = {"RunnerReady" if ready else "RunnerLoading": {}}
        return {
            "topology": {"nodes": list(self.nodes)},
            "nodeIdentities": {n: {"friendlyName": name} for n, name in self.nodes.items()},
            "nodeMemory": {n: {"ramTotal": {"inBytes": 64 * GIB}, "ramAvailable": {"inBytes": self.free[n]}}
                           for n in self.nodes},
            "downloads": downloads, "instances": instances, "runners": runners,
        }

    def state(self):
        return exo_client.parse_state(self.raw_state())

    def models(self):
        return [exo_client.ExoModel(m, mb * 1024 * 1024, {"id": m, "quantization": "4bit", "family": "qwen"})
                for m, mb in self.catalog.items()]

    def previews(self, model_id):
        if self.fits.get(model_id):
            return [exo_client.Placement(model_id, "Pipeline", {"MlxRingInstance": {}},
                                         {n: GIB for n in self.nodes})]
        return [exo_client.Placement(model_id, "Pipeline", {}, {}, exo_client.NO_MEMORY)]

    def start_download(self, node_id, shard_metadata):
        model_id = next(iter(shard_metadata.values()))["modelCard"]["modelId"]
        self.started.append((node_id, model_id))
        if self.fail_on == node_id:
            self.downloads[node_id][model_id] = ("Failed", 0, 0)
        elif self.complete_on_poll:
            self.downloads[node_id][model_id] = ("Completed", 343 * 1024 * 1024, 343 * 1024 * 1024)
        else:
            self.downloads[node_id][model_id] = ("Ongoing", 0, 343 * 1024 * 1024)

    def delete_download(self, node_id, model_id):
        self.deleted_downloads.append((node_id, model_id))
        self.downloads[node_id].pop(model_id, None)

    def add_model(self, model_id):
        self.added.append(model_id)
        self.catalog[model_id] = 100

    def search(self, query, limit=20):
        return self.hits

    def delete_instance(self, instance_id):
        self.deleted_instances.append(instance_id)
        self.instances.pop(instance_id, None)

    def create_instance(self, placement):
        self.created.append(placement.model_id)
        self.instances[f"i-{placement.model_id}"] = (placement.model_id, True)
        return "cmd"

    def await_instance(self, model_id, stall_seconds, on_progress=None, **_k):
        return True

    # helpers
    def have(self, model_id, *nodes):
        for node in nodes:
            self.downloads[node][model_id] = ("Completed", 343 * 1024 * 1024, 343 * 1024 * 1024)

    def load(self, model_id, ready=True):
        self.instances[f"i-{model_id}"] = (model_id, ready)


@pytest.fixture
def exo(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    settings.save(settings.ClusterSettings(role="head", namespace="omm-" + "0" * 32, gateway_port=52440))
    return FakeExo()


def _lines():
    seen = []
    return seen, seen.append


def test_inventory_states(exo):
    exo.have(SMALL, "n1", "n2", "n3")
    exo.have(OTHER, "n1")
    exo.load(SMALL)
    rows = {row.model: row for row in models.inventory(exo)}
    assert rows[SMALL].state == "loaded" and rows[SMALL].nodes_installed == 3 and rows[SMALL].nodes_total == 3
    assert rows[OTHER].state == "partial" and rows[OTHER].nodes_installed == 1
    assert rows[SMALL].size_bytes == 343 * 1024 * 1024
    assert "big/Huge-8bit" not in rows
    assert set(rows[SMALL].as_dict()) == {"model", "size_bytes", "nodes_installed", "nodes_total", "state"}


def test_inventory_installed_and_loading(exo):
    exo.have(SMALL, "n1", "n2", "n3")
    exo.have(OTHER, "n1", "n2", "n3")
    exo.load(OTHER, ready=False)
    rows = {row.model: row.state for row in models.inventory(exo)}
    assert rows == {SMALL: "installed", OTHER: "loading"}


def test_install_starts_only_missing_nodes(exo):
    exo.have(SMALL, "n1")
    seen, out = _lines()
    started = models.install(SMALL, out, exo, sleep=lambda s: None)
    assert started == 2
    assert sorted(exo.started) == [("n2", SMALL), ("n3", SMALL)]
    assert exo.added == []


def test_install_when_every_mac_has_it_starts_nothing(exo):
    exo.have(SMALL, "n1", "n2", "n3")
    seen, out = _lines()
    assert models.install(SMALL, out, exo, sleep=lambda s: None) == 0
    assert exo.started == []
    assert any("already on every Mac" in line for line in seen)


def test_install_adds_unknown_model_first(exo):
    seen, out = _lines()
    models.install("someone/New-4bit", out, exo, sleep=lambda s: None)
    assert exo.added == ["someone/New-4bit"]
    assert {model for _, model in exo.started} == {"someone/New-4bit"}


def test_install_reports_failed_mac(exo):
    exo.fail_on = "n2"
    seen, out = _lines()
    with pytest.raises(ClusterError, match="mac-b could not download .*disk full"):
        models.install(SMALL, out, exo, sleep=lambda s: None)


def test_install_gives_up_when_nothing_moves(exo):
    exo.complete_on_poll = False
    ticks = iter(range(0, 10_000, 100))
    seen, out = _lines()
    with pytest.raises(ClusterError, match="no progress for 15 minutes"):
        models.install(SMALL, out, exo, sleep=lambda s: None, clock=lambda: next(ticks))


def test_install_refused_on_member(exo):
    settings.save(settings.ClusterSettings(role="member", namespace="omm-" + "0" * 32, gateway_port=52440))
    with pytest.raises(UsageProblem, match="runs on the head Mac"):
        models.install(SMALL, lambda line: None, exo)


def test_uninstall_unloads_then_deletes_and_clears_kept_model(exo):
    exo.have(SMALL, "n1", "n2", "n3")
    exo.load(SMALL)
    current = settings.load()
    settings.save(settings.ClusterSettings(current.role, current.namespace, current.gateway_port, model=SMALL))
    seen, out = _lines()
    assert models.uninstall(SMALL, out, exo) == 3
    assert exo.deleted_instances == [f"i-{SMALL}"]
    assert sorted(exo.deleted_downloads) == [("n1", SMALL), ("n2", SMALL), ("n3", SMALL)]
    assert settings.load().model is None


def test_uninstall_not_installed_errors(exo):
    with pytest.raises(ClusterError, match="is not installed on this cluster"):
        models.uninstall(SMALL, lambda line: None, exo)


def test_info_reports_card_nodes_and_fit(exo):
    exo.have(SMALL, "n1", "n2")
    data = models.info(SMALL, exo)
    assert data["model"] == SMALL and data["card"]["quantization"] == "4bit"
    assert data["nodes"] == [{"name": "mac-a", "state": "installed"}, {"name": "mac-b", "state": "installed"},
                             {"name": "mac-c", "state": "missing"}]
    assert data["loaded"] is False and data["fits"] is True and data["peak_node_bytes"] == GIB


def test_info_of_unknown_model_errors(exo):
    with pytest.raises(ClusterError, match="omm cluster search"):
        models.info("nobody/Nothing", exo)


def test_search_marks_catalog_and_fit(exo):
    rows = models.search("qwen3", exo)
    assert rows[0] == {"model": SMALL, "downloads": 40482, "in_catalog": True,
                       "size_bytes": 327 * 1024 * 1024, "fits": True}
    assert rows[1]["in_catalog"] is False and rows[1]["size_bytes"] is None and rows[1]["fits"] is None
