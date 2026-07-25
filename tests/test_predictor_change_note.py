import json

import requests

from omm import predictor
from omm.featurize import FEATURE_ORDER


def _artifact(name="a"):
    return {
        "model_version": 4,
        "feature_order": FEATURE_ORDER,
        "trees": [{"leaf": True, "value": 1.0}],
        "candidates": [{"name": name}],
    }


def test_load_model_with_change_note_flags_new_data_as_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", tmp_path / "recommend-model.json")
    expected = _artifact()
    monkeypatch.setattr(predictor, "fetch_and_cache_model", lambda url: expected)

    artifact, changed = predictor.load_model_with_change_note("http://example.com/model.json")

    assert changed is True
    assert artifact == expected


def test_load_model_with_change_note_flags_identical_refetch_as_unchanged(monkeypatch, tmp_path):
    cache_path = tmp_path / "recommend-model.json"
    expected = _artifact()
    cache_path.write_text(json.dumps(expected))
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", cache_path)
    monkeypatch.setattr(predictor, "fetch_and_cache_model", lambda url: expected)

    artifact, changed = predictor.load_model_with_change_note("http://example.com/model.json")

    assert changed is False
    assert artifact == expected


def test_load_model_with_change_note_unchanged_when_fetch_fails_and_falls_back_to_cache(
    monkeypatch, tmp_path
):
    cache_path = tmp_path / "recommend-model.json"
    expected = _artifact()
    cache_path.write_text(json.dumps(expected))
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", cache_path)

    def _raise(url):
        raise requests.RequestException("boom")

    monkeypatch.setattr(predictor, "fetch_and_cache_model", _raise)

    artifact, changed = predictor.load_model_with_change_note("http://example.com/model.json")

    assert changed is False
    assert artifact == expected
