import json
from pathlib import Path


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
