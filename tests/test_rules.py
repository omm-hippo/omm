from omm import rules


def _rule(name, ram, vram):
    return {"name": name, "min_ram_gb": ram, "min_vram_gb": vram}


def test_matching_rules_ranks_by_the_pool_it_filtered_on():
    """A GPU host is filtered on min_vram_gb, so it must be ranked on it
    too; ranking on min_ram_gb put the worse VRAM fit first (and `--yes`
    auto-installs the first match)."""
    candidates = [_rule("ram-heavy", 64, 2), _rule("gpu-heavy", 8, 6), _rule("tiny", 4, 1)]

    with_gpu = rules.matching_rules(candidates, 8.0, has_gpu=True)
    assert [r["name"] for r in with_gpu] == ["gpu-heavy", "ram-heavy", "tiny"]

    without_gpu = rules.matching_rules(candidates, 64.0, has_gpu=False)
    assert [r["name"] for r in without_gpu] == ["ram-heavy", "gpu-heavy", "tiny"]


def test_matching_rules_drops_rules_that_do_not_fit():
    candidates = [_rule("big", 32, 24), _rule("small", 8, 4)]

    assert [r["name"] for r in rules.matching_rules(candidates, 8.0, has_gpu=True)] == ["small"]
    assert rules.matching_rules(candidates, 2.0, has_gpu=False) == []
