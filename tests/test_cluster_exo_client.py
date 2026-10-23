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


def test_await_instance_times_out(monkeypatch):
    client = exo_client.ExoClient()
    monkeypatch.setattr(client, "_get", lambda path, **k: {})
    clock = iter(range(0, 1000, 5))
    assert client.await_instance("m", 12, sleep=lambda s: None, clock=lambda: next(clock)) is False


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
