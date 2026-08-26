import copy
import json

import pytest
import requests

from omm import predictor
from omm.featurize import FEATURE_ORDER


def artifact(version=4):
    return {
        "model_version": version,
        "feature_order": FEATURE_ORDER.copy(),
        "trees": [
            {
                "feature": 0,
                "threshold": 16.0,
                "left": {"leaf": True, "value": 1.5},
                "right": {"leaf": True, "value": 2.5},
            }
        ],
        "candidates": [],
    }


@pytest.mark.parametrize("version", [3, 4])
def test_validate_model_artifact_accepts_supported_versions(version):
    assert predictor.validate_model_artifact(artifact(version))["model_version"] == version


def test_validate_model_artifact_rejects_mismatched_feature_order_and_future_version():
    mismatched = artifact()
    mismatched["feature_order"] = list(reversed(FEATURE_ORDER))
    with pytest.raises(ValueError):
        predictor.validate_model_artifact(mismatched)

    with pytest.raises(ValueError):
        predictor.validate_model_artifact(artifact(5))


@pytest.mark.parametrize(
    "change",
    [
        lambda node: node.update(feature=len(FEATURE_ORDER)),
        lambda node: node.update(threshold=float("nan")),
    ],
)
def test_validate_model_artifact_rejects_bad_branch_values(change):
    invalid = artifact()
    change(invalid["trees"][0])
    with pytest.raises(ValueError):
        predictor.validate_model_artifact(invalid)


def test_invalid_remote_does_not_replace_cache_and_falls_back(monkeypatch, tmp_path):
    cache_path = tmp_path / "recommend-model.json"
    cached = artifact()
    cache_path.write_text(json.dumps(cached))
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", cache_path)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            invalid = copy.deepcopy(cached)
            invalid["trees"] = []
            return invalid

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response())

    assert predictor.load_model("https://example.test/model.json") == cached
    assert json.loads(cache_path.read_text()) == cached


def test_load_cached_model_returns_none_for_invalid_json_or_schema(monkeypatch, tmp_path):
    cache_path = tmp_path / "recommend-model.json"
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", cache_path)
    cache_path.write_text("not json")
    assert predictor.load_cached_model() is None

    cache_path.write_text(json.dumps({"candidates": []}))
    assert predictor.load_cached_model() is None


@pytest.mark.parametrize(
    "candidate",
    [
        "not-an-object",
        {"repo_id": "org/model", "filename": ""},
        {"repo_id": "org/model", "filename": "model.gguf", "provider": "unknown"},
        {"repo_id": "org/model", "filename": "model.gguf", "size_bytes": True},
    ],
)
def test_validate_model_artifact_rejects_malformed_candidates(candidate):
    invalid = artifact()
    invalid["candidates"] = [candidate]

    with pytest.raises(ValueError):
        predictor.validate_model_artifact(invalid)


def test_required_memory_rejects_boolean_and_infinite_size_metadata():
    assert predictor.estimate_required_memory_gb({"size_bytes": True}) is None
    assert predictor.estimate_required_memory_gb({"size_bytes": float("inf")}) is None
