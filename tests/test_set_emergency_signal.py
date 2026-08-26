import json

import pytest

from omm import predictor
from scripts import set_emergency_signal


def _write_artifact(path, **extra):
    path.write_text(json.dumps({"candidates": [], **extra}))


def test_set_signal_adds_well_formed_emergency_field(tmp_path):
    artifact_path = tmp_path / "recommend-model.json"
    _write_artifact(artifact_path)

    set_emergency_signal.set_signal(
        artifact_path, id_="2026-08-19-outage", message="Firebase is down.", fixed_in_version="0.3.0"
    )

    artifact = json.loads(artifact_path.read_text())
    assert artifact["emergency"] == {
        "id": "2026-08-19-outage",
        "message": "Firebase is down.",
        "fixed_in_version": "0.3.0",
    }
    # predictor's own validator must accept what this script writes.
    assert predictor.extract_emergency_signal(artifact) == artifact["emergency"]


def test_set_signal_omits_fixed_in_version_when_not_given(tmp_path):
    artifact_path = tmp_path / "recommend-model.json"
    _write_artifact(artifact_path)

    set_emergency_signal.set_signal(artifact_path, id_="x", message="Outage, no fix version yet.", fixed_in_version=None)

    artifact = json.loads(artifact_path.read_text())
    assert "fixed_in_version" not in artifact["emergency"]


def test_set_signal_preserves_other_top_level_keys(tmp_path):
    artifact_path = tmp_path / "recommend-model.json"
    _write_artifact(artifact_path, model_version=4, trees=[{"leaf": True, "value": 1.0}])

    set_emergency_signal.set_signal(artifact_path, id_="x", message="msg", fixed_in_version=None)

    artifact = json.loads(artifact_path.read_text())
    assert artifact["model_version"] == 4
    assert artifact["trees"] == [{"leaf": True, "value": 1.0}]
    assert artifact["candidates"] == []


def test_clear_signal_removes_the_field(tmp_path):
    artifact_path = tmp_path / "recommend-model.json"
    _write_artifact(artifact_path, emergency={"id": "x", "message": "y"})

    set_emergency_signal.clear_signal(artifact_path)

    artifact = json.loads(artifact_path.read_text())
    assert "emergency" not in artifact


def test_clear_signal_is_a_noop_when_absent(tmp_path, capsys):
    artifact_path = tmp_path / "recommend-model.json"
    _write_artifact(artifact_path)

    set_emergency_signal.clear_signal(artifact_path)

    assert json.loads(artifact_path.read_text()) == {"candidates": []}
    assert "nothing to clear" in capsys.readouterr().out.lower()


def test_main_requires_message_and_id_unless_clearing(tmp_path, monkeypatch):
    artifact_path = tmp_path / "recommend-model.json"
    _write_artifact(artifact_path)
    monkeypatch.setattr("sys.argv", ["set_emergency_signal.py", str(artifact_path)])

    with pytest.raises(SystemExit):
        set_emergency_signal.main()


def test_set_signal_rejects_invalid_version_without_modifying_artifact(tmp_path):
    artifact_path = tmp_path / "recommend-model.json"
    _write_artifact(artifact_path)
    original = artifact_path.read_bytes()

    with pytest.raises(ValueError, match="X.Y.Z"):
        set_emergency_signal.set_signal(
            artifact_path,
            id_="incident",
            message="Update required",
            fixed_in_version="next",
        )

    assert artifact_path.read_bytes() == original


def test_set_signal_rejects_non_object_artifact(tmp_path):
    artifact_path = tmp_path / "recommend-model.json"
    artifact_path.write_text("[]")

    with pytest.raises(ValueError, match="JSON object"):
        set_emergency_signal.set_signal(
            artifact_path,
            id_="incident",
            message="Update required",
            fixed_in_version=None,
        )
