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
