"""Cluster model inventory, install, uninstall, info, search and the memory
guard (`omm cluster install/list/...`). The exo client is faked; download
entries reuse the shapes captured from a live exo 1.0.71 (downloads-mixed.json)."""

from __future__ import annotations

import copy
import dataclasses
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
        self.fit_alone = set()  # fits only once none of `blockers` is loaded
        self.blockers = {SMALL, OTHER}
        self.started, self.deleted_downloads, self.deleted_instances, self.added, self.created = [], [], [], [], []
        self.complete_on_poll = True
        self.fail_on = None
        self.hits = [exo_client.SearchHit(SMALL, 40482, ("text-generation",)),
                     exo_client.SearchHit("someone/New-4bit", 12, ())]

    def raw_state(self, timeout=10):
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
        loaded = {model for model, _ in self.instances.values()}
        if self.fits.get(model_id) or (model_id in self.fit_alone and not loaded & self.blockers):
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
    from omm.cluster import commands

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(commands, "lan_ipv4", lambda: "192.0.2.10")
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


BIG = "big/Huge-8bit"


def _guarded(exo, policy, answer=None):
    asked = []

    def confirm(question):
        asked.append(question)
        return answer

    seen, out = _lines()
    result = models.ensure_loaded(BIG, out, policy=policy, confirm=None if answer is None else confirm, client=exo)
    return result, asked, seen


def _two_loaded(exo):
    exo.fits[BIG] = False
    exo.fit_alone = {BIG}
    exo.have(SMALL, "n1", "n2", "n3")
    exo.have(OTHER, "n1", "n2", "n3")
    exo.load(SMALL)
    exo.load(OTHER)
    # Sizes in MiB: SMALL 39 GiB, OTHER 48.8 GiB, BIG 156.25 GiB.
    exo.catalog[SMALL] = 40_000
    exo.catalog[OTHER] = 50_000
    exo.catalog[BIG] = 160_000


def test_ensure_loaded_loads_when_it_fits(exo):
    exo.fits[BIG] = True
    seen, out = _lines()
    assert models.ensure_loaded(BIG, out, policy=GuardPolicy.BLOCK, confirm=None, client=exo).startswith("Serving")
    assert exo.created == [BIG]


def test_guard_block_refuses(exo):
    _two_loaded(exo)
    with pytest.raises(ClusterError, match="Memory Guard blocked the load") as caught:
        _guarded(exo, GuardPolicy.BLOCK)
    assert SMALL in str(caught.value) and "omm cluster unload" in str(caught.value)
    assert exo.deleted_instances == [] and exo.created == []


def test_guard_ask_without_tty_refuses(exo):
    _two_loaded(exo)
    with pytest.raises(ClusterError, match="Memory Guard blocked the load"):
        _guarded(exo, GuardPolicy.ASK)
    assert exo.deleted_instances == []


def test_guard_ask_unloads_largest_first_then_loads(exo):
    _two_loaded(exo)
    # 75 GiB free + OTHER 48.8 is short of 156.25; + SMALL 39 is enough.
    exo.free = {n: 25 * GIB for n in exo.nodes}
    result, asked, seen = _guarded(exo, GuardPolicy.ASK, answer=True)
    assert len(asked) == 1 and OTHER in asked[0] and SMALL in asked[0] and "in use by other people" in asked[0]
    assert exo.deleted_instances == [f"i-{OTHER}", f"i-{SMALL}"]
    assert exo.created == [BIG] and result.startswith("Serving")


def test_guard_ask_offers_only_what_is_needed(exo):
    _two_loaded(exo)
    exo.free = {n: 40 * GIB for n in exo.nodes}  # 120 GiB free + OTHER 48.8 >= 156.25
    exo.blockers = {OTHER}
    result, asked, seen = _guarded(exo, GuardPolicy.ASK, answer=True)
    assert OTHER in asked[0] and SMALL not in asked[0]
    assert exo.deleted_instances == [f"i-{OTHER}"]


def test_guard_ask_declined_keeps_models(exo):
    _two_loaded(exo)
    with pytest.raises(ClusterError, match="was not loaded"):
        _guarded(exo, GuardPolicy.ASK, answer=False)
    assert exo.deleted_instances == [] and exo.created == []


def test_guard_observe_shows_exo_error(exo):
    _two_loaded(exo)
    with pytest.raises(ClusterError, match="exo refused to load .*No cycles found with sufficient memory"):
        _guarded(exo, GuardPolicy.OBSERVE)
    assert exo.deleted_instances == []


def test_not_memory_related_misfit_keeps_old_error(exo):
    exo.fits[BIG] = False
    exo.previews = lambda model_id: [exo_client.Placement(model_id, "Tensor", {}, {}, "No tensor sharding found")]
    with pytest.raises(ClusterError, match="does not fit in this cluster's free memory"):
        _guarded(exo, GuardPolicy.ASK, answer=True)


def test_guard_ask_refuses_when_unloading_everything_is_not_enough(exo):
    _two_loaded(exo)
    exo.free = {n: 1 * GIB for n in exo.nodes}
    with pytest.raises(ClusterError, match="even with every loaded model unloaded"):
        _guarded(exo, GuardPolicy.ASK, answer=True)
    assert exo.deleted_instances == []


def test_guard_block_does_not_suggest_unloading_when_it_cannot_help(exo):
    _two_loaded(exo)
    exo.free = {n: 1 * GIB for n in exo.nodes}
    with pytest.raises(ClusterError, match="even with every loaded model unloaded") as caught:
        _guarded(exo, GuardPolicy.BLOCK)
    assert "omm cluster unload" not in str(caught.value)


def test_guard_block_names_only_what_would_make_room(exo):
    _two_loaded(exo)
    exo.free = {n: 40 * GIB for n in exo.nodes}
    with pytest.raises(ClusterError, match=f"omm cluster unload {OTHER}") as caught:
        _guarded(exo, GuardPolicy.BLOCK)
    assert f"unload {SMALL}" not in str(caught.value)


def test_guard_ask_loads_the_unloaded_models_back_when_the_load_fails(exo):
    _two_loaded(exo)
    exo.free = {n: 25 * GIB for n in exo.nodes}
    exo.fit_alone = set()  # exo still refuses BIG after the unloads
    exo.fits.update({SMALL: True, OTHER: True})
    settings.save(dataclasses.replace(settings.load(), model=SMALL))
    with pytest.raises(ClusterError, match=f"still does not fit .*Loaded {OTHER}, {SMALL} back"):
        _guarded(exo, GuardPolicy.ASK, answer=True)
    assert sorted(model for model, _ in exo.instances.values()) == sorted([SMALL, OTHER])
    # The model the head reloads after a restart is still the admin's own choice.
    assert settings.load().model == SMALL


def test_guard_observe_does_not_claim_to_try(exo):
    _two_loaded(exo)
    seen, out = _lines()
    with pytest.raises(ClusterError):
        models.ensure_loaded(BIG, out, policy=GuardPolicy.OBSERVE, confirm=None, client=exo)
    assert seen and "trying anyway" not in " ".join(seen)


def test_load_of_an_installed_model_does_not_mention_downloading(exo):
    from omm.cluster import commands

    exo.have(SMALL, "n1", "n2", "n3")
    seen, out = _lines()
    commands.load_model(SMALL, out, exo)
    assert seen[0] == f"Loading {SMALL} (Pipeline)..."
    exo.instances.clear()
    exo.downloads["n3"].clear()
    seen, out = _lines()
    commands.load_model(SMALL, out, exo)
    assert "download it first" in seen[0]


def test_search_leaves_out_speech_and_image_models(exo):
    exo.hits.append(exo_client.SearchHit("mlx-community/Qwen3-ASR-0.6B-8bit", 900,
                                         ("mlx-audio", "speech-to-text", "asr")))
    exo.hits.append(exo_client.SearchHit("mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ", 900,
                                         ("text-generation", "sentence-similarity", "feature-extraction")))
    found = [row["model"] for row in models.search("qwen3", exo)]
    assert SMALL in found and not [model for model in found if "ASR" in model or "Embedding" in model]


def test_load_placed_only_on_macs_that_have_it_does_not_mention_downloading(exo):
    from omm.cluster import commands

    exo.have(SMALL, "n1", "n2")
    placement = exo_client.Placement(SMALL, "Tensor", {"MlxJacclInstance": {}}, {"n1": GIB, "n2": GIB})
    seen, out = _lines()
    commands.load_model(SMALL, out, exo, placement)
    assert seen[0] == f"Loading {SMALL} (Tensor)..."
