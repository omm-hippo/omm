"""exo loopback client and state parsers (#503), against fixtures captured
from a real exo 1.0.71 node (see the spike findings doc)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omm.cluster import exo_client
from omm.cluster.errors import ClusterError

FIX = Path(__file__).parent / "fixtures" / "exo"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_parse_state_fixture_has_four_nodes_with_memory_and_rdma():
    state = exo_client.parse_state(_load("state.json"))
    assert len(state.nodes) == 4
    assert all(n.memory_total_bytes > 0 for n in state.nodes)
    assert {n.rdma for n in state.nodes} <= {"enabled", "disabled", "unknown"}
    assert sorted(n.name for n in state.nodes) == ["mac-a", "mac-b", "mac-c", "mac-d"]


def test_parse_real_single_node_state():
    state = exo_client.parse_state(_load("state-1node.json"))
    (node,) = state.nodes
    assert node.name == "mac-a"
    assert node.memory_total_bytes == 68719476736
    assert 0 < node.memory_available_bytes <= node.memory_total_bytes
    assert node.rdma == "enabled"
    assert node.os_version == "26.5"
    (instance,) = state.instances
    assert instance.model_id == "mlx-community/Llama-3.2-1B-Instruct-4bit"
    assert instance.node_ids == (node.node_id,)
    assert instance.sharding == "Pipeline"
    assert state.links == ()


def test_parse_state_links_from_topology_edges():
    state = exo_client.parse_state(_load("state.json"))
    assert len(state.links) == 12
    assert {link.kind for link in state.links} == {"rdma"}


def test_parse_state_tolerates_missing_optional_fields():
    assert exo_client.parse_state({}) == exo_client.ClusterState(None, (), (), ())


def test_node_without_identity_falls_back_to_id_and_unknown_rdma():
    data = {"nodeMemory": {"n1": {"ramTotal": {"inBytes": 10}, "ramAvailable": {"inBytes": 5}}}}
    (node,) = exo_client.parse_state(data).nodes
    assert (node.name, node.rdma, node.os_version) == ("n1", "unknown", None)


def test_parse_models_uses_megabytes():
    models = exo_client.parse_models(_load("models.json"))
    first = next(m for m in models if m.model_id == "mlx-community/Qwen3-0.6B-4bit")
    assert first.size_bytes == 327 * 1024 * 1024


def test_parse_previews_keeps_error_rows_and_pipeline_sharding():
    rows = exo_client.parse_previews(_load("previews.json"))
    assert rows and any(r.error is None for r in rows)
    assert all(isinstance(r.memory_delta_by_node, dict) for r in rows)
    assert {r.sharding for r in rows} == {"Pipeline", "Tensor"}
    assert all(r.model_id == "mlx-community/Llama-3.2-1B-Instruct-4bit" for r in rows)


def test_ready_instance_detection_reads_runner_state():
    data = _load("state-1node.json")
    assert exo_client.instance_ready(data, "mlx-community/Llama-3.2-1B-Instruct-4bit") is True
    assert exo_client.instance_ready(data, "other/model") is False
    runner = next(iter(data["runners"]))
    data["runners"][runner] = {"RunnerLoading": {}}
    assert exo_client.instance_ready(data, "mlx-community/Llama-3.2-1B-Instruct-4bit") is False


def test_raw_state_is_the_unparsed_state_that_state_parses(monkeypatch):
    client = exo_client.ExoClient()
    data = _load("state-1node.json")
    paths = []
    monkeypatch.setattr(client, "_get", lambda path, **k: paths.append(path) or data)
    assert client.raw_state() is data
    assert client.state() == exo_client.parse_state(data)
    assert paths == ["/state", "/state"]


def test_client_raises_exo_unavailable_when_nothing_listens():
    client = exo_client.ExoClient("http://127.0.0.1:9")
    with pytest.raises(exo_client.ExoUnavailable, match="exo is not running on this Mac"):
        client.state()


def test_exo_unavailable_is_a_cluster_error():
    assert issubclass(exo_client.ExoUnavailable, ClusterError)


def test_await_instance_polls_state_until_ready(monkeypatch):
    client = exo_client.ExoClient()
    loading = _load("state-1node.json")
    runner = next(iter(loading["runners"]))
    loading["runners"][runner] = {"RunnerLoading": {}}
    states = iter([loading, loading, _load("state-1node.json")])
    monkeypatch.setattr(client, "_get", lambda path, **k: next(states))
    sleeps = []
    assert client.await_instance("mlx-community/Llama-3.2-1B-Instruct-4bit", 60, sleep=sleeps.append) is True
    assert len(sleeps) == 2


def test_await_instance_gives_up_when_nothing_changes_for_the_stall_window(monkeypatch):
    client = exo_client.ExoClient()
    monkeypatch.setattr(client, "_get", lambda path, **k: {})
    clock = iter(range(0, 1000, 5))
    assert client.await_instance("m", 12, sleep=lambda s: None, clock=lambda: next(clock)) is False


# downloads-live.json holds real Pending / Ongoing / Completed entries captured
# from a live exo 1.0.71 (2026-10-24); the synthetic entries below follow the same
# wire format (camelCase, Memory -> {"inBytes"}) for cases a live run never shows.
MODEL = "mlx-community/Qwen3.5-122B-A10B-8bit"
GIB = 1024**3


def _download(kind, node, model_id=MODEL, **body):
    shard = {"PipelineShardMetadata": {"modelCard": {"modelId": model_id}}}
    return {kind: {"nodeId": node, "shardMetadata": shard, **body}}


def _memory(n):
    return {"inBytes": n}


def _ongoing(node, done, total, model_id=MODEL):
    progress = {"downloaded": _memory(done), "total": _memory(total), "completedFiles": 1, "totalFiles": 9}
    return _download("DownloadOngoing", node, model_id, downloadProgress=progress)


LIVE_MODEL = "mlx-community/Llama-3.2-1B-Instruct-4bit"


def test_load_progress_reads_entries_captured_from_a_live_exo():
    live = _load("downloads-live.json")
    node = next(iter(live["downloads"]))
    pending = exo_client.load_progress(live, LIVE_MODEL)
    assert (pending.phase, pending.downloaded_bytes, pending.total_bytes) == ("downloading", 0, 712593855)
    ongoing = exo_client.load_progress({"downloads": {node: [live["ongoing"]]}}, LIVE_MODEL)
    assert (ongoing.phase, ongoing.downloaded_bytes, ongoing.total_bytes) == ("downloading", 472708913, 712593855)
    completed = exo_client.load_progress({"downloads": {node: [live["completed"]]}}, LIVE_MODEL)
    assert completed.phase == "loading" and completed.transferred_bytes == 712593855


def test_load_progress_reports_the_mac_furthest_behind():
    data = {"downloads": {"a": [_ongoing("a", 90 * GIB, 130 * GIB)], "b": [_ongoing("b", 40 * GIB, 130 * GIB)]}}
    progress = exo_client.load_progress(data, MODEL)
    assert (progress.phase, progress.downloaded_bytes, progress.total_bytes) == ("downloading", 40 * GIB, 130 * GIB)
    assert progress.transferred_bytes == 130 * GIB
    assert progress.failed is None


def test_load_progress_is_loading_once_every_download_completed_or_none_listed():
    done = {"downloads": {"a": [_download("DownloadCompleted", "a", total=_memory(130 * GIB))]}}
    assert exo_client.load_progress(done, MODEL).phase == "loading"
    assert exo_client.load_progress({}, MODEL).phase == "loading"


def test_load_progress_pending_download_has_unknown_total():
    data = {"downloads": {"a": [_download("DownloadPending", "a", downloaded=_memory(0), total=_memory(0))]}}
    progress = exo_client.load_progress(data, MODEL)
    assert (progress.phase, progress.downloaded_bytes, progress.total_bytes) == ("downloading", 0, 0)


def test_load_progress_ignores_other_models_and_keeps_failure_message():
    failed = _download("DownloadFailed", "a", errorMessage="disk full")
    data = {"downloads": {"a": [_ongoing("a", 1, 2, "other/model"), failed]}}
    progress = exo_client.load_progress(data, MODEL)
    assert progress.phase == "downloading" and progress.failed == "disk full"
    assert exo_client.load_progress(data, "other/model").failed is None


def test_load_progress_names_the_mac_whose_download_failed():
    failed = _download("DownloadFailed", "a", errorMessage="[Errno 13] Permission denied")
    data = {"downloads": {"a": [failed]}, "nodeIdentities": {"a": {"friendlyName": "Node4 Mac mini"}}}
    progress = exo_client.load_progress(data, MODEL)
    assert (progress.failed, progress.failed_on) == ("[Errno 13] Permission denied", "Node4 Mac mini")
    assert exo_client.load_progress({"downloads": {"a": [failed]}}, MODEL).failed_on == "a"


def test_await_instance_stops_at_a_failed_download(monkeypatch):
    # exo never retries a failed download until it restarts (exo 1.0.71
    # DownloadCoordinator._start_download skips models whose status is Failed).
    client = exo_client.ExoClient()
    failed = {"downloads": {"a": [_download("DownloadFailed", "a", errorMessage="disk full")]}}
    monkeypatch.setattr(client, "_get", lambda path, **k: failed)
    seen = []
    clock = iter(range(0, 10_000, 1))
    assert client.await_instance(MODEL, 900, on_progress=seen.append, sleep=lambda s: None,
                                 clock=lambda: next(clock)) is False
    assert len(seen) == 1 and seen[0].failed == "disk full"


def test_load_progress_survives_junk_download_entries():
    data = {"downloads": {"a": "junk", "b": [None, {}, {"x": 1, "y": 2}, {"DownloadOngoing": "junk"}]}}
    assert exo_client.load_progress(data, MODEL).phase == "loading"


def test_await_instance_keeps_waiting_while_the_download_advances(monkeypatch):
    client = exo_client.ExoClient()
    ready = _load("state-1node.json")
    llama = "mlx-community/Llama-3.2-1B-Instruct-4bit"
    placed = {"i1": {"MlxRingInstance": {"instanceId": "i1", "shardAssignments": {
        "modelId": llama, "nodeToRunner": {"a": "r1"}, "runnerToShard": {"r1": {"PipelineShardMetadata": {}}}}}}}
    steps = [{"downloads": {"a": [_ongoing("a", n * GIB, 100 * GIB, llama)]}, "instances": placed}
             for n in range(1, 11)]
    states = iter([*steps, ready])
    monkeypatch.setattr(client, "_get", lambda path, **k: next(states))
    clock = iter(range(0, 10_000, 20))  # 11 polls, 20 s apart: far beyond a 30 s stall window
    seen = []
    assert client.await_instance(
        llama, 30, on_progress=seen.append, sleep=lambda s: None, clock=lambda: next(clock)) is True
    assert [p.downloaded_bytes for p in seen] == [n * GIB for n in range(1, 11)]


def test_await_instance_gives_up_when_the_download_stalls(monkeypatch):
    client = exo_client.ExoClient()
    stuck = {"downloads": {"a": [_ongoing("a", 5 * GIB, 100 * GIB)]}}
    monkeypatch.setattr(client, "_get", lambda path, **k: stuck)
    clock = iter(range(0, 10_000, 20))
    assert client.await_instance(MODEL, 30, sleep=lambda s: None, clock=lambda: next(clock)) is False


def test_await_instance_counts_a_runner_changing_state_as_progress(monkeypatch):
    client = exo_client.ExoClient()
    states = iter([{"runners": {"r": {kind: {}}}} for kind in ("RunnerConnected", "RunnerLoading", "RunnerLoading")] + [_load("state-1node.json")])
    monkeypatch.setattr(client, "_get", lambda path, **k: next(states))
    clock = iter(range(0, 10_000, 20))
    assert client.await_instance(
        "mlx-community/Llama-3.2-1B-Instruct-4bit", 30, sleep=lambda s: None, clock=lambda: next(clock)) is True


def test_parse_state_reads_thunderbolt_bridge_cycles():
    data = _load("state.json")
    ids = list(data["nodeIdentities"])
    data["thunderboltBridgeCycles"] = [ids[:2], ids[:3], "junk", [ids[0], 7]]
    state = exo_client.parse_state(data)
    # Rows that are not lists of ids are skipped; the rest keep exo's order.
    assert state.bridge_cycles == (tuple(ids[:2]), tuple(ids[:3]))


def test_parse_state_without_cycles_key_has_none():
    assert exo_client.parse_state(_load("state.json")).bridge_cycles == ()


def test_node_in_topology_but_not_yet_in_node_memory_has_unknown_memory():
    # Right after a restart exo lists a node before its first memory report.
    data = {"topology": {"nodes": ["n1"], "connections": {}}}
    (node,) = exo_client.parse_state(data).nodes
    assert node.memory_total_bytes is None
    assert node.memory_available_bytes is None


def test_parse_downloads_reads_every_kind():
    rows = exo_client.parse_downloads(_load("downloads-mixed.json"))
    by_model = {row.model_id: row for row in rows}
    done = by_model["mlx-community/Llama-3.2-1B-Instruct-4bit"]
    assert done.kind == "Completed"
    assert done.done_bytes == done.total_bytes == 712593855
    pending = by_model["mlx-community/Qwen3-0.6B-4bit"]
    assert pending.kind == "Pending"
    assert pending.node_id.startswith("12D3KooWNode")
    assert pending.shard_metadata["PipelineShardMetadata"]["startLayer"] == 0
    assert pending.shard_metadata["PipelineShardMetadata"]["endLayer"] == 28


def test_parse_downloads_reads_ongoing_progress():
    ongoing = _load("downloads-live.json")["ongoing"]
    rows = exo_client.parse_downloads({"downloads": {"n1": [ongoing]}})
    assert [(r.kind, r.done_bytes > 0, r.total_bytes > 0) for r in rows] == [("Ongoing", True, True)]


def test_parse_search_reads_ids_downloads_and_tags():
    hits = exo_client.parse_search(_load("search.json"))
    assert hits[1].model_id == "mlx-community/Qwen3-0.6B-8bit"
    assert hits[1].downloads > 0
    assert "text-generation" in hits[1].tags


def _recording_client(monkeypatch, reply=None):
    client = exo_client.ExoClient()
    calls = []

    def call(method, path, **kwargs):
        calls.append((method, path, kwargs.get("payload")))
        return reply if reply is not None else {}

    monkeypatch.setattr(client, "_call", call)
    return client, calls


def test_start_download_posts_target_node_and_shard(monkeypatch):
    client, calls = _recording_client(monkeypatch)
    client.start_download("n1", {"PipelineShardMetadata": {"startLayer": 0}})
    assert calls == [("POST", "/download/start",
                      {"targetNodeId": "n1", "shardMetadata": {"PipelineShardMetadata": {"startLayer": 0}}})]


def test_delete_download_quotes_the_model_id(monkeypatch):
    client, calls = _recording_client(monkeypatch)
    client.delete_download("n1", "mlx-community/Llama-3.2-1B-Instruct-4bit")
    assert calls == [("DELETE", "/download/n1/mlx-community%2FLlama-3.2-1B-Instruct-4bit", None)]


def test_add_model_posts_the_id(monkeypatch):
    client, calls = _recording_client(monkeypatch)
    client.add_model("someone/Some-Model-4bit")
    assert calls == [("POST", "/models/add", {"model_id": "someone/Some-Model-4bit"})]


def test_parse_models_keeps_the_raw_card():
    models = exo_client.parse_models(_load("models.json"))
    assert models[0].card["quantization"] == "4bit"


def test_no_memory_text_matches_the_live_refusal():
    rows = exo_client.parse_previews(_load("previews-nomem.json"))
    assert rows and all(exo_client.NO_MEMORY in row.error for row in rows)


def test_load_progress_skips_a_mac_the_instance_does_not_use():
    # Tensor on a and b; c keeps the Pending entry `omm cluster install` left and never fetches.
    data = {
        "downloads": {"a": [_download("DownloadCompleted", "a", total=_memory(GIB))],
                      "b": [_download("DownloadCompleted", "b", total=_memory(GIB))],
                      "c": [_download("DownloadPending", "c", downloaded=_memory(0), total=_memory(GIB))]},
        "instances": {"i1": {"MlxJacclInstance": {"instanceId": "i1", "shardAssignments": {
            "modelId": MODEL, "nodeToRunner": {"a": "r1", "b": "r2"},
            "runnerToShard": {"r1": {"TensorShardMetadata": {}}, "r2": {"TensorShardMetadata": {}}}}}}},
    }
    assert exo_client.load_progress(data, MODEL).phase == "loading"
    del data["instances"]
    assert exo_client.load_progress(data, MODEL).phase == "downloading"


def test_await_instance_reports_nothing_before_exo_places_the_instance(monkeypatch):
    client = exo_client.ExoClient()
    pending = {"downloads": {"a": [_download("DownloadPending", "a", downloaded=_memory(0), total=_memory(GIB))]}}
    states = iter([pending, pending, _load("state-1node.json")])
    monkeypatch.setattr(client, "_get", lambda path, **k: next(states))
    seen = []
    assert client.await_instance("mlx-community/Llama-3.2-1B-Instruct-4bit", 30, on_progress=seen.append,
                                 sleep=lambda s: None) is True
    assert seen == []
