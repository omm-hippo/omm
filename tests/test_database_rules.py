import json
from pathlib import Path

from localfit_server.app import BUSY_CPU_LOAD_PERCENT
from omm.hardware import BUSY_CPU_PERCENT


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


def test_firebase_rules_use_the_same_busy_cpu_threshold_as_the_python_code():
    """Security rules cannot import Python, so the threshold is written out
    as a literal. If BUSY_CPU_PERCENT ever moves, the rules and the collector
    must move with it, or a client would send a label the server rejects."""
    assert BUSY_CPU_LOAD_PERCENT == BUSY_CPU_PERCENT
    literal = (
        str(int(BUSY_CPU_PERCENT))
        if float(BUSY_CPU_PERCENT).is_integer()
        else str(BUSY_CPU_PERCENT)
    )
    validate = _rules()["telemetry"]["$event"][".validate"]

    assert f"newData.child('host_cpu_load_percent').val() >= {literal}" in validate
    assert f"newData.child('host_cpu_load_percent').val() < {literal}" in validate


def test_firebase_rules_make_the_host_load_reading_optional_and_bounded():
    """Old clients send v9 rows with no reading at all, and `$other` rejects
    any key the rules do not declare - so the field has to be declared here
    and accepted as absent."""
    event = _rules()["telemetry"]["$event"]
    field = event["host_cpu_load_percent"][".validate"]

    assert field.startswith("!newData.exists() ||")
    assert "newData.val() >= 0" in field
    assert "newData.val() <= 100" in field
    assert "'loaded'" in event["measurement_quality"][".validate"]
    # `loaded` never validates on its own: the label needs the evidence.
    assert (
        "newData.child('measurement_quality').val() == 'loaded' && "
        "newData.child('host_cpu_load_percent').exists()"
    ) in event[".validate"]


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
