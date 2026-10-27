"""Cluster model recommendation and placement choice (#503)."""

from __future__ import annotations

from omm.cluster.exo_client import ClusterNode, ClusterState, ExoModel, Placement
from omm.cluster.recommend import HEADROOM, Candidate, choose_placement, prefilter, rank

GiB = 1024 ** 3


def pl(sharding, deltas, error=None, model_id="m"):
    return Placement(model_id=model_id, sharding=sharding, instance={"s": sharding}, memory_delta_by_node=deltas, error=error)


def m(model_id, size):
    return ExoModel(model_id=model_id, size_bytes=size)


def st(available):
    nodes = tuple(
        ClusterNode(node_id=f"n{i}", name=f"mac-{i}", memory_total_bytes=a, memory_available_bytes=a, rdma="enabled", os_version="26.2")
        for i, a in enumerate(available)
    )
    return ClusterState(master_id="n0", nodes=nodes, links=(), instances=())


def test_headroom_constant():
    assert HEADROOM == 0.85


def test_choose_placement_prefers_pipeline_then_smallest_peak():
    p = [pl("Tensor", {"a": 10, "b": 10}), pl("Pipeline", {"a": 30, "b": 5}), pl("Pipeline", {"a": 15, "b": 15}),
         pl("Pipeline", {"a": 1}, error="no fit")]
    assert choose_placement(p) == p[2]


def test_choose_placement_falls_back_to_other_sharding():
    p = [pl("Tensor", {"a": 10}), pl("Pipeline", {"a": 1}, error="no fit")]
    assert choose_placement(p) == p[0]


def test_choose_placement_none_when_every_row_errors():
    assert choose_placement([pl("Pipeline", {"a": 1}, error="no fit")]) is None
    assert choose_placement([]) is None


def test_rank_drops_models_above_headroom_and_sorts_biggest_first():
    state = st(available=[64 * GiB] * 4)
    models = [m("small", 20 * GiB), m("big", 200 * GiB), m("huge", 230 * GiB)]
    previews = {
        "small": [pl("Pipeline", {"a": 20 * GiB})],
        "big": [pl("Pipeline", {"a": 50 * GiB, "b": 50 * GiB, "c": 50 * GiB, "d": 50 * GiB})],
        "huge": [pl("Pipeline", {"a": 58 * GiB, "b": 58 * GiB, "c": 58 * GiB, "d": 58 * GiB})],
    }
    ranked = rank(models, previews, state)
    assert [c.model_id for c in ranked] == ["big", "small"]
    assert isinstance(ranked[0], Candidate)
    assert abs(ranked[0].memory_used_fraction - 200 / 256) < 1e-9


def test_rank_skips_models_without_previews_and_respects_limit():
    state = st(available=[64 * GiB] * 4)
    models = [m(f"x{i}", (i + 1) * GiB) for i in range(8)] + [m("nopreview", 5 * GiB)]
    previews = {f"x{i}": [pl("Pipeline", {"a": (i + 1) * GiB})] for i in range(8)}
    ranked = rank(models, previews, state, limit=3)
    assert [c.model_id for c in ranked] == ["x7", "x6", "x5"]


def test_prefilter_skips_unknown_and_oversized_sizes():
    state = st(available=[64 * GiB])
    assert prefilter([m("x", None)], state) == []
    assert prefilter([m("fits", 50 * GiB), m("too-big", 60 * GiB)], state) == [m("fits", 50 * GiB)]


def test_empty_cluster_recommends_nothing():
    assert rank([m("a", 1)], {"a": [pl("Pipeline", {"a": 1})]}, st(available=[])) == []


def _real_previews():
    import json
    from pathlib import Path

    from omm.cluster.exo_client import parse_previews

    data = json.loads((Path(__file__).parent / "fixtures" / "exo" / "previews-3node.json").read_text(encoding="utf-8"))
    return parse_previews(data)


def test_choose_placement_prefers_tensor_over_rdma_on_a_real_three_mac_preview():
    # Qwen3.5-9B-4bit on three Mac minis, 2026-10-25: Tensor over RDMA (Jaccl)
    # on 2 Macs ran 72.4 tok/s, the Pipeline placement omm used to pick 42.2.
    chosen = choose_placement(_real_previews())
    backend = next(iter(chosen.instance))
    assert (chosen.sharding, backend) == ("Tensor", "MlxJacclInstance")
    assert len(chosen.instance[backend]["shardAssignments"]["nodeToRunner"]) == 2


def tensor(backend, deltas, error=None):
    runners = {f"node{i}": f"r{i}" for i in range(len(deltas))}
    return Placement(model_id="m", sharding="Tensor", memory_delta_by_node=deltas, error=error,
                     instance={backend: {"shardAssignments": {"nodeToRunner": runners}}})


def test_choose_placement_ignores_tensor_without_rdma_or_on_one_mac():
    single = tensor("MlxJacclInstance", {"a": 10})
    ring = tensor("MlxRingInstance", {"a": 5, "b": 5})
    pipe = pl("Pipeline", {"a": 6, "b": 6})
    assert choose_placement([single, ring, pipe]) == pipe


def test_choose_placement_breaks_a_pipeline_tie_towards_rdma():
    ring = Placement("m", "Pipeline", {"MlxRingInstance": {}}, {"a": 5, "b": 5})
    rdma = Placement("m", "Pipeline", {"MlxJacclInstance": {}}, {"a": 5, "b": 5})
    assert choose_placement([ring, rdma]) == rdma


# --- the admin's placement choice (#532) -------------------------------------

import json
from pathlib import Path

from omm.cluster import recommend
from omm.cluster.exo_client import NO_MEMORY, parse_previews

FIX = Path(__file__).parent / "fixtures" / "exo"
N4 = "12D3KooWEeJp88EYKV6XE9QDpdMY2kijiriMq1erJHw7UtAsqrsN"
N3 = "12D3KooWEGWXuYAEJcR7BaLAR1iFM3sGXoNbjz4ywPed7Sb1Tv1b"
N1 = "12D3KooWA6ubTyXAp9NiFguJGxKeSqSUqkvm4J2kKKZ1ECmfuvgS"


def three_macs():
    # The real 3-Mac rig: exo offers one set of Macs per sharding, backend and Mac count.
    return parse_previews(json.loads((FIX / "previews-3node.json").read_text(encoding="utf-8")))


def shape(placement):
    return placement.sharding, recommend.link(placement), sorted(placement.memory_delta_by_node)


def test_named_sharding_and_mac_count_pick_that_row_over_rdma():
    assert shape(choose_placement(three_macs(), sharding="Tensor", macs=2)) == ("Tensor", "RDMA", sorted([N3, N4]))
    # The flags are typed lower case; exo spells them capitalised.
    assert shape(choose_placement(three_macs(), sharding="pipeline", macs=2)) == ("Pipeline", "RDMA", sorted([N3, N4]))


def test_named_sharding_alone_prefers_rdma_then_least_memory_per_mac():
    assert shape(choose_placement(three_macs(), sharding="Pipeline")) == ("Pipeline", "RDMA", sorted([N1, N3, N4]))
    assert shape(choose_placement(three_macs(), sharding="Tensor")) == ("Tensor", "RDMA", sorted([N3, N4]))


def test_mac_count_alone_keeps_the_automatic_preference_among_those_rows():
    assert shape(choose_placement(three_macs(), macs=2)) == ("Tensor", "RDMA", sorted([N3, N4]))
    assert shape(choose_placement(three_macs(), macs=3)) == ("Pipeline", "RDMA", sorted([N1, N3, N4]))
    assert shape(choose_placement(three_macs(), macs=1)) == ("Pipeline", "none", [N4])


def test_a_choice_exo_does_not_offer_picks_nothing():
    assert choose_placement(three_macs(), sharding="Tensor", macs=3) is None
    assert choose_placement(three_macs(), macs=4) is None


def test_no_choice_is_the_automatic_pick():
    assert choose_placement(three_macs()) == choose_placement(three_macs(), sharding=None, macs=None)
    assert shape(choose_placement(three_macs())) == ("Tensor", "RDMA", sorted([N3, N4]))


def test_placement_rows_show_each_distinct_placement_once_with_exos_refusal():
    names = {N1: "node1", N3: "node3", N4: "node4"}
    rows = recommend.placement_rows(three_macs(), names)
    fitting = [(r["sharding"], r["link"], r["macs"]) for r in rows if r["fits"]]
    assert fitting == [("Pipeline", "none", 1), ("Pipeline", "RDMA", 2), ("Pipeline", "TCP", 2),
                       ("Pipeline", "RDMA", 3), ("Pipeline", "TCP", 3),
                       ("Tensor", "none", 1), ("Tensor", "RDMA", 2), ("Tensor", "TCP", 2)]
    refused = [r for r in rows if not r["fits"]]
    assert len(refused) == 1 and refused[0]["sharding"] == "Tensor"
    assert refused[0]["error"].startswith("No tensor sharding found") and refused[0]["macs"] is None
    picked = [r for r in rows if r["automatic"]]
    assert len(picked) == 1 and (picked[0]["sharding"], picked[0]["link"], picked[0]["macs"]) == ("Tensor", "RDMA", 2)
    assert picked[0]["nodes"] == ["node3", "node4"]
    assert picked[0]["memory_per_mac_bytes"] == 2975031280
    # Without names (an employee's view) the Macs are not listed.
    assert all("nodes" not in r for r in recommend.placement_rows(three_macs()))


def test_placement_rows_when_nothing_fits():
    rows = recommend.placement_rows(parse_previews(json.loads((FIX / "previews-nomem.json").read_text(encoding="utf-8"))))
    assert [(r["sharding"], r["fits"], r["error"]) for r in rows] == [("Pipeline", False, NO_MEMORY),
                                                                      ("Tensor", False, NO_MEMORY)]


def test_possible_and_describe_texts():
    assert recommend.possible(three_macs()) == "Pipeline on 1, 2 or 3 Macs; Tensor on 1 or 2 Macs"
    assert recommend.possible([]) == "nothing fits the cluster's free memory now"
    assert recommend.describe("Tensor", 2) == "Tensor on 2 Macs"
    assert recommend.describe("Pipeline", 1) == "Pipeline on 1 Mac"
    assert recommend.describe(None, 3) == "3 Macs"
    assert recommend.describe("Tensor", None) == "Tensor"
    assert recommend.describe(None, None) == "automatic"
