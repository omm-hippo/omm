import json
from pathlib import Path


def _rules():
    rules_path = Path(__file__).resolve().parents[1] / "database.rules.json"
    return json.loads(rules_path.read_text(encoding="utf-8"))["rules"]


def test_firebase_rules_accept_v9_contract_fields_and_reject_unknown_fields():
    rules_path = Path(__file__).resolve().parents[1] / "database.rules.json"
    event = json.loads(rules_path.read_text(encoding="utf-8"))["rules"]["telemetry"]["$event"]

    assert "benchmark_version').val() == 9" in event[".validate"]
    assert "measurement_profile" in event[".validate"]
    assert "tokens_per_sec_mad_ratio" in event[".validate"]
    assert "newData.val() <= 9" in event["benchmark_version"][".validate"]
    assert event["measurement_profile"][".validate"]
    assert event["estimated_committed_ram_gb"][".validate"]
    assert event["$other"][".validate"] is False


def test_firebase_rules_keep_error_reports_unreadable_and_append_only():
    """The whole reason error reports live outside /telemetry: that node is
    publicly readable, and error text must never be."""
    node = _rules()["error_reports"]

    assert node[".read"] is False
    assert ".read" not in node["$report"]
    assert node["$report"][".write"] == "!data.exists()"


def test_firebase_rules_bound_the_shape_and_size_of_an_error_report():
    report = _rules()["error_reports"]["$report"]

    assert "schema_version" in report[".validate"]
    assert "error_type" in report[".validate"]
    assert "install_quality_eval" in report["trigger"][".validate"]
    assert "crash" in report["trigger"][".validate"]
    assert "2000" in report["error_message"][".validate"]
    assert report["$other"][".validate"] is False


def test_firebase_rules_leave_the_public_telemetry_node_alone():
    assert _rules()["telemetry"][".read"] is True
