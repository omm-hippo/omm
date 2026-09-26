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
