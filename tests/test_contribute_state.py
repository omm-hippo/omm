import json

from omm import contribute_state


def test_load_returns_none_when_no_file(isolated_omm_home):
    assert contribute_state.load() is None


def test_record_exhausted_then_load_round_trips(isolated_omm_home):
    contribute_state.record_exhausted(total_candidates=26, covered_candidates=10)

    state = contribute_state.load()

    assert state["total_candidates"] == 26
    assert state["covered_candidates"] == 10
    assert "exhausted_at" in state


def test_record_exhausted_stores_metadata_on_disk(isolated_omm_home):
    contribute_state.record_exhausted(total_candidates=26, covered_candidates=10)

    data = json.loads((isolated_omm_home / "contribute_state.json").read_text())

    assert data["total_candidates"] == 26
    assert data["covered_candidates"] == 10


def test_record_exhausted_overwrites_prior_state(isolated_omm_home):
    contribute_state.record_exhausted(total_candidates=26, covered_candidates=10)
    contribute_state.record_exhausted(total_candidates=30, covered_candidates=12)

    state = contribute_state.load()

    assert state["total_candidates"] == 30
    assert state["covered_candidates"] == 12


def test_load_ignores_wrong_json_shape_and_inconsistent_counts(isolated_omm_home):
    path = isolated_omm_home / "contribute_state.json"
    path.write_text("[]")
    assert contribute_state.load() is None

    path.write_text(
        json.dumps(
            {
                "total_candidates": 2,
                "covered_candidates": 3,
                "exhausted_at": "2026-08-26T00:00:00+00:00",
            }
        )
    )
    assert contribute_state.load() is None


def test_record_exhausted_ignores_invalid_counts(isolated_omm_home):
    contribute_state.record_exhausted(total_candidates=2, covered_candidates=3)

    assert not (isolated_omm_home / "contribute_state.json").exists()
