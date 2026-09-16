from copy import deepcopy
import json

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.evaluation import describe_evaluation
from scripts.retrain_decision import publication_outputs


def complete_report():
    metrics = {
        "rows": 77, "selection_group_count": 3,
        "rmsle": 0.2, "p90_absolute_percentage_error": 0.3,
        "top1_selection_accuracy": 0.7, "mean_normalized_regret": 0.1,
        "p90_normalized_regret": 0.2, "fit_balanced_accuracy": 0.9,
        "fit_false_positive_rate": 0.1,
    }
    return {
        "passed": True, "fit_negative_examples": 5,
        "candidate": metrics, "baseline": deepcopy(metrics),
        "thresholds": {
            "min_selection_groups": 3, "min_fit_negative_examples": 5,
            "max_rmsle_regression": 0.02, "max_p90_ape_regression": 0.05,
            "max_selection_metric_regression": 0.05,
        },
    }


@pytest.mark.parametrize("negatives", [0, 1, 4])
def test_legacy_pass_never_hides_unassessed_fit(negatives):
    report = complete_report()
    report["fit_negative_examples"] = negatives
    result = describe_evaluation(report)
    assert result["publication_gate_passed"] is True
    assert result["status"] == "insufficient_data"
    assert result["fully_evaluated"] is False
    assert result["checks"]["rmsle"]["status"] == "passed"
    assert result["checks"]["fit_false_positive_rate"]["status"] == "insufficient_data"
    assert result["data_gaps"]["fit"]["additional_needed"] == 5 - negatives


def test_sufficient_evidence_marks_only_regressions_failed():
    report = complete_report()
    assert describe_evaluation(report)["status"] == "passed"
    report["candidate"]["fit_false_positive_rate"] = 0.8
    result = describe_evaluation(report)
    assert result["status"] == "failed"
    assert result["checks"]["fit_false_positive_rate"]["status"] == "failed"
    assert result["checks"]["rmsle"]["status"] == "passed"


def test_missing_selection_count_is_not_evidence_of_good_selection():
    report = complete_report()
    report["candidate"]["selection_group_count"] = 2
    result = describe_evaluation(report)
    assert result["checks"]["top1_selection_accuracy"]["status"] == "insufficient_data"
    assert result["data_gaps"]["selection"]["additional_needed"] == 1


@pytest.mark.parametrize("bad", [None, {}, [], {"passed": True}, {"candidate": []}])
def test_missing_or_malformed_historical_report_is_not_a_pass(bad):
    assert describe_evaluation(bad)["status"] == "insufficient_data"


def test_nonfinite_metrics_and_boolean_counts_are_not_evidence():
    report = complete_report()
    report["candidate"]["rmsle"] = float("nan")
    report["fit_negative_examples"] = True
    result = describe_evaluation(report)
    assert result["checks"]["rmsle"]["status"] == "insufficient_data"
    assert result["checks"]["fit_balanced_accuracy"]["status"] == "insufficient_data"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("invalid", [10**1000, -1, float("inf")])
def test_invalid_historical_numbers_do_not_crash_status(invalid):
    report = complete_report()
    report["candidate"]["rmsle"] = invalid
    assert describe_evaluation(report)["checks"]["rmsle"]["status"] == "insufficient_data"


def test_publication_description_discloses_unassessed_checks_without_changing_policy():
    report = complete_report()
    report["fit_negative_examples"] = 1
    output = publication_outputs(report, b"candidate", b"incumbent")
    assert output["quality_gate_status"] == "passed"
    assert "coverage is incomplete" in output["pr_body"]
    assert "fit_false_positive_rate" in output["pr_body"]


def test_catalog_status_reports_cached_evidence_without_fetching(monkeypatch):
    report = complete_report()
    report["fit_negative_examples"] = 1
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"evaluation": report})
    result = CliRunner().invoke(cli.app, ["setting", "catalog-status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["evaluation"]["checks"]["rmsle"]["status"] == "passed"
    assert data["evaluation"]["checks"]["fit_balanced_accuracy"]["status"] == "insufficient_data"
    text = CliRunner().invoke(cli.app, ["setting", "catalog-status"])
    assert "4 known-unfit measurements" in text.stdout
