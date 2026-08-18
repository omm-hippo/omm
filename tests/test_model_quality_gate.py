from __future__ import annotations

import math

import pytest

from scripts.model_quality_gate import (
    compare_artifacts,
    evaluate_artifact,
    validate_artifact,
    validate_dataset,
)


FEATURES = ["ram", "vram"]
X = [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]]
Y = [1.0, 2.0, 3.0]


def artifact(value: float, *, feature_order=FEATURES, tree=None) -> dict:
    return {
        "model_version": 1,
        "feature_order": feature_order,
        "trees": [tree or {"leaf": True, "value": value}],
    }


def test_compare_rejects_missing_selection_evidence():
    baseline = artifact(2.0)
    candidate = artifact(0.0, tree={"feature": 0, "threshold": 1.5, "left": {"leaf": True, "value": 1.5}, "right": {"leaf": True, "value": 3.0}})

    report = compare_artifacts(candidate, baseline, X, Y, min_selection_groups=3)

    assert report["passed"] is False
    assert any("selection groups" in failure for failure in report["failures"])
    assert report["candidate"]["rmsle"] < report["baseline"]["rmsle"]


def test_compare_blocks_rmsle_regression():
    report = compare_artifacts(artifact(8.0), artifact(2.0), X, Y)

    assert report["passed"] is False
    assert any("rmsle" in failure for failure in report["failures"])


def test_compare_blocks_p90_ape_regression():
    report = compare_artifacts(
        artifact(2.5), artifact(2.0), X, Y, max_rmsle_regression=10.0
    )

    assert report["passed"] is False
    assert any("p90_absolute_percentage_error" in failure for failure in report["failures"])


def test_validate_dataset_rejects_insufficient_or_over_rejected_data():
    with pytest.raises(ValueError, match="too few"):
        validate_dataset({"raw_rows": 10, "rejected_rows": 0, "unique_configurations": 19, "direct_v6_unique_configurations": 19})
    with pytest.raises(ValueError, match="rejection"):
        validate_dataset({"raw_rows": 10, "rejected_rows": 3, "unique_configurations": 20, "direct_v6_unique_configurations": 20})
    with pytest.raises(ValueError, match="too few"):
        validate_dataset({"raw_rows": 0, "rejected_rows": 0, "unique_configurations": 0, "direct_v6_unique_configurations": 0})


@pytest.mark.parametrize(
    ("bad_y", "bad_artifact", "match"),
    [
        ([math.nan], None, "finite"),
        ([-1.0], None, "non-negative"),
        ([1.0], artifact(1.0, tree={"feature": 2, "threshold": 0.0, "left": {"leaf": True, "value": 1.0}, "right": {"leaf": True, "value": 1.0}}), "outside"),
    ],
)
def test_evaluation_rejects_invalid_data_or_artifact(bad_y, bad_artifact, match):
    with pytest.raises(ValueError, match=match):
        evaluate_artifact(bad_artifact or artifact(1.0), [[0.0, 0.0]], bad_y)


def test_validate_artifact_rejects_non_finite_threshold_and_leaf():
    with pytest.raises(ValueError, match="feature_order"):
        validate_artifact(artifact(1.0, feature_order=["vram", "ram"]), FEATURES)
    with pytest.raises(ValueError, match="threshold"):
        validate_artifact(
            artifact(1.0, tree={"feature": 0, "threshold": math.nan, "left": {"leaf": True, "value": 1.0}, "right": {"leaf": True, "value": 1.0}}),
            FEATURES,
        )
    with pytest.raises(ValueError, match="value"):
        validate_artifact(artifact(math.inf), FEATURES)


def test_evaluation_requires_exact_feature_vector_length():
    with pytest.raises(ValueError, match="expected 2"):
        evaluate_artifact(artifact(1.0), [[0.0]], [1.0])


def test_evaluation_uses_one_tok_per_second_floor_for_zero_target_ape():
    metrics = evaluate_artifact(artifact(4.0), [[0.0, 0.0]], [0.0])

    assert math.isfinite(metrics["p90_absolute_percentage_error"])
    assert metrics["p90_absolute_percentage_error"] == 4.0


def test_compare_requires_matching_feature_contracts():
    with pytest.raises(ValueError, match="feature_order must match"):
        compare_artifacts(artifact(1.0), artifact(1.0, feature_order=["ram"]), X, Y)


def test_validate_dataset_prefers_explicit_direct_unique_over_sum():
    # Same shape a real train_model.py audit produces: v6=1, v7=1, but the
    # true union is 1 (one config measured under both schema versions).
    # The sum (2) would wrongly pass a min=2 gate; the explicit union (1)
    # correctly does not.
    audit = {
        "raw_rows": 2, "rejected_rows": 0, "unique_configurations": 1,
        "direct_v6_unique_configurations": 1, "direct_v7_unique_configurations": 1,
        "direct_unique_configurations": 1,
    }
    validate_dataset(audit, min_unique_configurations=1)  # must not raise
    with pytest.raises(ValueError, match="too few unique direct-v6"):
        validate_dataset(audit, min_unique_configurations=2)


def test_validate_dataset_falls_back_to_sum_when_union_field_is_absent():
    # Pre-fix audits (e.g. hand-built in older tests/callers) never had
    # direct_unique_configurations at all - validate_dataset must still
    # accept them, reproducing the old sum-based count exactly.
    audit = {
        "raw_rows": 2, "rejected_rows": 0, "unique_configurations": 2,
        "direct_v6_unique_configurations": 1, "direct_v7_unique_configurations": 1,
    }
    validate_dataset(audit, min_unique_configurations=2)  # must not raise
    with pytest.raises(ValueError, match="too few unique direct-v6"):
        validate_dataset(audit, min_unique_configurations=3)


def test_validate_dataset_rejects_union_exceeding_the_sum_it_is_drawn_from():
    audit = {
        "raw_rows": 2, "rejected_rows": 0, "unique_configurations": 5,
        "direct_v6_unique_configurations": 1, "direct_v7_unique_configurations": 1,
        "direct_unique_configurations": 3,  # impossible: |A union B| <= |A| + |B|
    }
    with pytest.raises(ValueError, match="cannot exceed the sum"):
        validate_dataset(audit, min_unique_configurations=1)


def test_validate_dataset_accepts_v8_only_corpus():
    # Steady-state future: telemetry is 100% v8 (no v6/v7 rows at all). The
    # ceiling check must count v8 or this raises a bare ValueError instead of
    # the caught InsufficientTelemetryError, hard-failing the training job.
    audit = {
        "raw_rows": 5, "rejected_rows": 0, "unique_configurations": 5,
        "direct_v6_unique_configurations": 0, "direct_v7_unique_configurations": 0,
        "direct_v8_unique_configurations": 5,
        "direct_unique_configurations": 5,
    }
    validate_dataset(audit, min_unique_configurations=5)  # must not raise


def test_intentional_v9_quality_exclusions_do_not_inflate_rejection_rate():
    audit = {
        "raw_rows": 10,
        "rejected_rows": 9,
        "unique_configurations": 1,
        "direct_v6_unique_configurations": 0,
        "direct_v7_unique_configurations": 0,
        "direct_v8_unique_configurations": 0,
        "direct_v9_unique_configurations": 1,
        "direct_unique_configurations": 1,
        "rejections": {"pressured_measurement_excluded": 5, "unstable_measurement_excluded": 4},
    }

    validate_dataset(audit, min_unique_configurations=1, max_rejection_rate=0.25)


def test_dataset_rejection_rate_must_be_a_fraction():
    with pytest.raises(ValueError, match="at most 1"):
        validate_dataset(
            {"raw_rows": 20, "rejected_rows": 0, "unique_configurations": 20, "direct_v6_unique_configurations": 20},
            max_rejection_rate=1.1,
        )


SELECTION_FEATURES = [
    "quant_bits",
    "ram_gb",
    "param_count_b",
    "model_size_gb",
    "active_param_count_b",
]
SELECTION_X = [
    [4.0, 16.0, 3.0, 2.0, 3.0],
    [8.0, 16.0, 7.0, 5.0, 7.0],
]
SELECTION_Y = [0.0, 2.0]


def selection_artifact(*, reversed_prediction: bool = False) -> dict:
    # Uses model feature names, not positions: quant_bits is deliberately index 0.
    return artifact(
        0.0,
        feature_order=SELECTION_FEATURES,
        tree={
            "feature": 0,
            "threshold": 6.0,
            "left": {"leaf": True, "value": 3.0 if reversed_prediction else 0.0},
            "right": {"leaf": True, "value": 0.0 if reversed_prediction else 3.0},
        },
    )


def test_selection_and_fit_metrics_are_finite_and_use_feature_names():
    metrics = evaluate_artifact(selection_artifact(), SELECTION_X, SELECTION_Y)

    assert metrics["selection_group_count"] == 1
    assert metrics["top1_selection_accuracy"] == 1.0
    assert metrics["mean_normalized_regret"] == 0.0
    assert metrics["p90_normalized_regret"] == 0.0
    assert metrics["fit_balanced_accuracy"] == 1.0
    assert metrics["fit_false_positive_rate"] == 0.0
    assert all(math.isfinite(value) for value in metrics.values() if isinstance(value, float))


def test_selection_mistake_has_expected_normalized_regret():
    metrics = evaluate_artifact(selection_artifact(reversed_prediction=True), SELECTION_X, SELECTION_Y)

    assert metrics["top1_selection_accuracy"] == 0.0
    assert metrics["mean_normalized_regret"] == 1.0
    assert metrics["p90_normalized_regret"] == 1.0
    assert metrics["fit_balanced_accuracy"] == 0.0
    assert metrics["fit_false_positive_rate"] == 1.0


def test_selection_without_model_candidates_and_single_fit_class_is_null():
    metrics = evaluate_artifact(artifact(2.0), X, Y)

    assert metrics["selection_group_count"] == 0
    assert metrics["top1_selection_accuracy"] is None
    assert metrics["mean_normalized_regret"] is None
    assert metrics["p90_normalized_regret"] is None
    assert metrics["fit_balanced_accuracy"] is None
    assert metrics["fit_false_positive_rate"] is None


def test_selection_regression_blocks_and_missing_evidence_rejects():
    report = compare_artifacts(
        selection_artifact(reversed_prediction=True),
        selection_artifact(),
        SELECTION_X,
        SELECTION_Y,
        max_rmsle_regression=10.0,
        max_p90_ape_regression=10.0, min_selection_groups=1,
    )
    assert report["passed"] is False
    assert any("top1_selection_accuracy" in failure for failure in report["failures"])
    assert any("mean_normalized_regret" in failure for failure in report["failures"])

    no_evidence = compare_artifacts(artifact(2.0), artifact(2.0), X, Y)
    assert no_evidence["passed"] is False
    assert any("missing evidence" in failure for failure in no_evidence["failures"])
    assert no_evidence["thresholds"]["max_selection_metric_regression"] == 0.05


def test_compare_allows_sufficient_selection_evidence():
    X = [
        [quant, ram, parameters, parameters * 0.7, parameters]
        for ram in (16.0, 32.0, 64.0)
        for quant, parameters in ((4.0, 3.0), (8.0, 7.0))
    ]
    y = [0.0, 2.0] * 3

    report = compare_artifacts(selection_artifact(), selection_artifact(), X, y)

    assert report["passed"] is True
    assert report["failures"] == []


# --- explicit fit labels (v7 outcome), backward-compatible fallback -------


def test_evaluate_artifact_prefers_explicit_fit_labels_over_speed_threshold():
    # The artifact predicts 0.0 (unfit) for feature [0.0, 0.0] and 3.0 (fit)
    # for [1.0, 0.0]; y >= 1.0 would call BOTH of these "fit" under the
    # legacy heuristic (y=[2.0, 2.0]), but the explicit label says the first
    # one is actually a known-unfit configuration.
    tree = {
        "feature": 0, "threshold": 0.5,
        "left": {"leaf": True, "value": 0.0},
        "right": {"leaf": True, "value": 3.0},
    }
    model = artifact(0.0, tree=tree)

    metrics = evaluate_artifact(
        model, X, Y,
        fit_examples=[([0.0, 0.0], False), ([1.0, 0.0], True)],
    )

    assert metrics["fit_balanced_accuracy"] == 1.0
    assert metrics["fit_false_positive_rate"] == 0.0


def test_evaluate_artifact_falls_back_to_legacy_threshold_without_fit_examples():
    # Same artifact/X/Y as the selection tests above: omitting fit_examples
    # must reproduce the exact pre-v7 behavior (inferred from y >= 1.0).
    metrics_without = evaluate_artifact(selection_artifact(), SELECTION_X, SELECTION_Y)
    metrics_with_empty = evaluate_artifact(selection_artifact(), SELECTION_X, SELECTION_Y, fit_examples=[])

    assert metrics_without["fit_balanced_accuracy"] == 1.0
    assert metrics_without["fit_false_positive_rate"] == 0.0
    assert metrics_with_empty == metrics_without


def test_compare_artifacts_forwards_fit_examples_to_both_models():
    fit_examples = [([0.0, 0.0], False), ([1.0, 0.0], False), ([2.0, 0.0], True)]

    report = compare_artifacts(
        artifact(2.0), artifact(2.0), X, Y, fit_examples=fit_examples,
    )

    # Both models predict a constant 2.0 (>= 1.0) for every row, so every
    # explicit-False example is a false positive under the explicit labels.
    assert report["candidate"]["fit_false_positive_rate"] == 1.0
    assert report["baseline"]["fit_false_positive_rate"] == 1.0


def test_compare_artifacts_ignores_fit_regression_with_too_few_negative_examples():
    # Regression test for the 2026-08-07 incident: real telemetry had
    # exactly one model_unfit/performance_unfit row, and a single flipped
    # prediction on it swung fit_balanced_accuracy 0.994 -> 0.5 and
    # fit_false_positive_rate 0.0 -> 1.0, hard-failing every nightly
    # retrain. min_fit_negative_examples guards the fit metrics the same
    # way min_selection_groups already guards the selection metrics.
    inner = {
        "feature": 0, "threshold": 6.0,
        "left": {"leaf": True, "value": 0.0},
        "right": {"leaf": True, "value": 3.0},
    }

    def outer(outlier_value: float) -> dict:
        # ram_gb <= 50 reaches the shared `inner` subtree (both SELECTION_X
        # rows have ram_gb=16), so candidate and baseline predict
        # identically on SELECTION_X - only the single fit_examples row
        # with ram_gb=999 can differ between them.
        return {
            "feature": 1, "threshold": 50.0,
            "left": inner,
            "right": {"leaf": True, "value": outlier_value},
        }

    baseline = artifact(0.0, feature_order=SELECTION_FEATURES, tree=outer(0.0))
    candidate = artifact(0.0, feature_order=SELECTION_FEATURES, tree=outer(3.0))
    fit_examples = [
        ([8.0, 16.0, 7.0, 5.0, 7.0], True),
        ([5.0, 999.0, 1.0, 1.0, 1.0], False),
    ]

    report = compare_artifacts(
        candidate, baseline, SELECTION_X, SELECTION_Y,
        min_selection_groups=1, fit_examples=fit_examples,
    )

    assert report["candidate"]["fit_false_positive_rate"] == 1.0
    assert report["baseline"]["fit_false_positive_rate"] == 0.0
    assert report["passed"] is True
    assert report["failures"] == []
