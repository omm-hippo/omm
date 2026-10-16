"""Publication gate for the arena leaderboard artifact."""

from __future__ import annotations

from scripts import arena_quality_gate


def _artifact(*, votes=500.0, share=0.2, ranked=8) -> dict:
    models = [
        {
            "key": f"filename:model-{index}.gguf",
            "provisional": False,
            "quality": {"tier": 1},
        }
        for index in range(ranked)
    ]
    return {
        "schema_version": 1,
        "generated_at": "2026-09-27T04:00:00+00:00",
        "corpus": {
            "rows_fetched": 900,
            "rows_used": 880,
            "rows_dropped": {},
            "effective_votes": votes,
            "client_count": 40,
            "largest_client_share": share,
        },
        "models": models,
        "tiers": [{"tier": 1, "model_keys": [m["key"] for m in models]}],
        "efficiency_components": [],
        "provisional_models": [],
    }


def test_a_healthy_artifact_passes():
    assert arena_quality_gate.evaluate(_artifact()).status == "passed"


def test_too_little_data_is_blocked():
    result = arena_quality_gate.evaluate(_artifact(votes=42.0))
    assert result.status == "blocked"
    assert any("effective votes" in reason for reason in result.reasons)


def test_one_machine_dominating_is_blocked():
    result = arena_quality_gate.evaluate(_artifact(share=0.81))
    assert result.status == "blocked"
    assert any("client" in reason for reason in result.reasons)


def test_too_few_ranked_models_is_blocked():
    result = arena_quality_gate.evaluate(_artifact(ranked=3))
    assert result.status == "blocked"
    assert any("ranked models" in reason for reason in result.reasons)


def test_a_collapse_against_the_incumbent_is_blocked():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=6), incumbent=_artifact(ranked=20)
    )
    assert result.status == "blocked"
    assert any("shrank" in reason for reason in result.reasons)


def test_a_mild_change_against_the_incumbent_passes():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=18), incumbent=_artifact(ranked=20)
    )
    assert result.status == "passed"


def test_growth_against_the_incumbent_passes():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=40), incumbent=_artifact(ranked=20)
    )
    assert result.status == "passed"


def test_the_first_run_has_no_incumbent_and_skips_the_collapse_check():
    result = arena_quality_gate.evaluate(_artifact(ranked=6), incumbent=None)
    assert result.status == "passed"


def test_an_incumbent_with_no_ranked_models_skips_the_collapse_check():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=6), incumbent=_artifact(ranked=0)
    )
    assert result.status == "passed"


def test_every_failing_condition_is_reported_not_just_the_first():
    result = arena_quality_gate.evaluate(_artifact(votes=10.0, share=0.9, ranked=1))
    assert result.status == "blocked"
    assert len(result.reasons) == 3


def test_provisional_models_do_not_count_as_ranked():
    artifact = _artifact(ranked=8)
    for model in artifact["models"][4:]:
        model["provisional"] = True
        model["quality"]["tier"] = None
    assert arena_quality_gate.ranked_model_count(artifact) == 4


# --- sub-project D: per-scope gates and the publish merge ------------------


def _scope(*, votes=500.0, share=0.2, ranked=8, tag="m") -> dict:
    scope = _artifact(votes=votes, share=share, ranked=ranked)
    for model in scope["models"]:
        model["key"] = model["key"].replace("model", tag)
    scope["tiers"] = [{"tier": 1, "model_keys": [m["key"] for m in scope["models"]]}]
    scope["pairs"] = []
    for key in ("schema_version", "generated_at"):
        del scope[key]
    return scope


def _fresh(overall, groups=None, generated_at="2026-10-17T04:00:00+00:00") -> dict:
    return {
        "schema_version": 2,
        "generated_at": generated_at,
        **overall,
        "groups": [
            {"key": key, "memory_kind": key.split(":")[0], "memory_bucket": key.split(":")[1], **scope}
            for key, scope in (groups or {}).items()
        ],
    }


THIN = dict(votes=37.0, ranked=1)


def test_a_group_dominated_by_one_client_is_blocked():
    result = arena_quality_gate.evaluate(
        _scope(votes=150.0, share=0.8, ranked=4),
        min_votes=arena_quality_gate.MIN_GROUP_EFFECTIVE_VOTES,
        min_ranked=arena_quality_gate.MIN_GROUP_RANKED_MODELS,
    )
    assert result.status == "blocked"
    assert any("80%" in reason for reason in result.reasons)


def test_group_thresholds_are_lower_than_overall():
    group = _scope(votes=120.0, ranked=3)
    assert arena_quality_gate.evaluate(group).status == "blocked"
    assert (
        arena_quality_gate.evaluate(
            group,
            min_votes=arena_quality_gate.MIN_GROUP_EFFECTIVE_VOTES,
            min_ranked=arena_quality_gate.MIN_GROUP_RANKED_MODELS,
        ).status
        == "passed"
    )


def test_nothing_ranked_ever_gives_a_collecting_file_with_counts_and_reasons():
    out, results = arena_quality_gate.publish(_fresh(_scope(**THIN)), None)
    assert out["status"] == "collecting"
    assert out["ranked"] is False
    assert out["models"] == [] and out["tiers"] == [] and out["pairs"] == []
    assert out["corpus"]["client_count"] == 40
    assert out["corpus"]["effective_votes"] == 37.0
    assert out["reasons"] and out["reasons"] == list(results["overall"].reasons)
    assert out["updated_at"] is None


def test_a_group_can_rank_while_overall_collects():
    out, _ = arena_quality_gate.publish(
        _fresh(_scope(**THIN), {"unified:16": _scope(votes=150.0, ranked=4)}), None
    )
    assert out["status"] == "ranked"
    assert out["ranked"] is False and out["models"] == []
    (group,) = out["groups"]
    assert group["ranked"] is True and len(group["models"]) == 4
    assert group["updated_at"] == "2026-10-17T04:00:00+00:00"
    assert (group["key"], group["memory_kind"], group["memory_bucket"]) == (
        "unified:16", "unified", "16",
    )


def test_an_unranked_group_keeps_its_counts():
    out, _ = arena_quality_gate.publish(
        _fresh(_scope(), {"vram:8": _scope(votes=40.0, ranked=1)}), None
    )
    (group,) = out["groups"]
    assert group["ranked"] is False
    assert group["corpus"]["effective_votes"] == 40.0
    assert group["models"] == []


def test_collecting_never_overwrites_a_ranked_scope():
    incumbent, _ = arena_quality_gate.publish(
        _fresh(_scope(), generated_at="2026-10-16T04:00:00+00:00"), None
    )
    out, _ = arena_quality_gate.publish(_fresh(_scope(**THIN)), incumbent)
    assert out["status"] == "ranked"
    assert out["ranked"] is True
    assert out["models"] == incumbent["models"]
    assert out["updated_at"] == "2026-10-16T04:00:00+00:00"


def test_a_ranked_model_collapse_keeps_the_previous_board():
    incumbent, _ = arena_quality_gate.publish(_fresh(_scope(ranked=20)), None)
    out, results = arena_quality_gate.publish(_fresh(_scope(ranked=6)), incumbent)
    assert results["overall"].status == "blocked"
    assert len(out["models"]) == 20


def test_a_ranked_group_missing_from_the_fresh_corpus_is_kept():
    incumbent, _ = arena_quality_gate.publish(
        _fresh(_scope(), {"unified:16": _scope(votes=150.0, ranked=4)}), None
    )
    out, _ = arena_quality_gate.publish(_fresh(_scope()), incumbent)
    assert [g["key"] for g in out["groups"]] == ["unified:16"]
    assert out["groups"][0]["ranked"] is True


def test_a_newly_passing_group_publishes_while_another_is_blocked():
    out, _ = arena_quality_gate.publish(
        _fresh(
            _scope(**THIN),
            {"unified:16": _scope(votes=150.0, ranked=4), "vram:8": _scope(votes=10.0, ranked=0)},
        ),
        None,
    )
    ranked = {g["key"]: g["ranked"] for g in out["groups"]}
    assert ranked == {"unified:16": True, "vram:8": False}


def test_unchanged_content_keeps_updated_at():
    first, _ = arena_quality_gate.publish(
        _fresh(_scope(), generated_at="2026-10-16T04:00:00+00:00"), None
    )
    second, _ = arena_quality_gate.publish(_fresh(_scope()), first)
    assert second["updated_at"] == "2026-10-16T04:00:00+00:00"
    changed, _ = arena_quality_gate.publish(_fresh(_scope(votes=600.0)), first)
    assert changed["updated_at"] == "2026-10-17T04:00:00+00:00"


def test_a_schema_1_incumbent_is_not_merged():
    legacy = _artifact()
    out, _ = arena_quality_gate.publish(_fresh(_scope(**THIN)), legacy)
    assert out["status"] == "collecting"


def test_ungated_publishes_every_scope():
    out, _ = arena_quality_gate.publish(_fresh(_scope(**THIN)), None, gated=False)
    assert out["status"] == "ranked" and out["ranked"] is True
