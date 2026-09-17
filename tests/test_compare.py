from __future__ import annotations

import pytest

from omm import compare, recommend_status
from omm.hardware import HardwareInfo


def hardware() -> HardwareInfo:
    return HardwareInfo("macOS", "", "Apple M5", 24, 10, True, "Apple M5", 24, 10)


def candidate(name: str, size_gb: float, *, tags=None) -> dict:
    return {
        "name": name,
        "repo_id": f"org/{name}",
        "filename": f"{name}-Q4_K_M.gguf",
        "provider": "huggingface",
        "size_bytes": int(size_gb * 1024**3),
        "pipeline_tag": "text-generation",
        "tags": tags or [],
    }


def artifact(*candidates: dict) -> dict:
    return {"trees": [{}], "candidates": list(candidates)}


def test_resolve_requires_two_to_five_exact_unique_models():
    one = candidate("One-7B", 4)
    two = candidate("Two-7B", 4)
    with pytest.raises(compare.CompareInputError, match="2 to 5"):
        compare.resolve_candidates([one], ["One-7B"])
    with pytest.raises(compare.CompareInputError, match="more than once"):
        compare.resolve_candidates([one, two], ["One-7B", "org/One-7B"])


def test_resolve_accepts_name_repo_filename_and_exact_ref():
    one = candidate("One-7B", 4)
    two = candidate("Two-7B", 4)
    assert compare.resolve_candidates([one, two], ["org/One-7B", "Two-7B-Q4_K_M.gguf"]) == [one, two]
    assert compare.resolve_candidates(
        [one, two], ["org/One-7B:One-7B-Q4_K_M.gguf", "Two-7B"]
    ) == [one, two]


def test_resolve_rejects_unknown_and_ambiguous_without_guessing():
    q4 = candidate("Model-7B", 4)
    q2 = {**q4, "filename": "Model-7B-Q2_K.gguf"}
    with pytest.raises(compare.CompareInputError, match="not in"):
        compare.resolve_candidates([q4], ["missing", "Model-7B"])
    with pytest.raises(compare.CompareInputError, match="ambiguous"):
        compare.resolve_candidates([q4, q2], ["org/Model-7B", "Model-7B-Q2_K.gguf"])


def test_compare_separates_best_fit_fastest_and_declared_purpose(monkeypatch):
    large = candidate("Large-12B", 8, tags=["coding"])
    fast = candidate("Fast-7B", 4)
    data = artifact(large, fast)
    monkeypatch.setattr(
        compare.predictor,
        "rank_candidates",
        lambda artifact, hw: [(fast, 40.0), (large, 15.0)],
    )
    result = compare.compare_candidates(data, ["Large-12B", "Fast-7B"], hardware(), profile="balanced")
    assert result.best_fit_ref.endswith("Large-12B-Q4_K_M.gguf")
    assert result.fastest_ref.endswith("Fast-7B-Q4_K_M.gguf")
    [large_row, fast_row] = result.rows
    assert large_row.declared_purpose == "Coding"
    assert fast_row.declared_purpose == "General"
    assert all(row.measured_quality is None for row in result.rows)
    assert result.quality_complete is False


def test_compare_uses_quality_only_when_every_eligible_model_is_measured(monkeypatch):
    one = candidate("One-7B", 4, tags=["coding"])
    two = candidate("Two-7B", 5, tags=["coding"])
    monkeypatch.setattr(
        compare.predictor,
        "rank_candidates",
        lambda artifact, hw: [(one, 20.0), (two, 18.0)],
    )
    evidence = {
        compare.candidate_key(one): [compare.QualityEvidence("Coding", "coding", "1", 0.8, "8/10")],
        compare.candidate_key(two): [compare.QualityEvidence("Coding", "coding", "1", 0.9, "9/10")],
    }
    result = compare.compare_candidates(
        artifact(one, two), ["One-7B", "Two-7B"], hardware(), purpose="coding", quality_index=evidence
    )
    assert result.quality_complete is True
    assert result.best_measured_quality_ref.endswith("Two-7B-Q4_K_M.gguf")
    assert result.best_match_ref == result.best_measured_quality_ref

    incomplete = {compare.candidate_key(one): evidence[compare.candidate_key(one)]}
    result = compare.compare_candidates(
        artifact(one, two), ["One-7B", "Two-7B"], hardware(), purpose="coding", quality_index=incomplete
    )
    assert result.quality_complete is False
    assert result.best_match_ref == result.best_fit_ref


def test_warning_and_profile_failures_are_not_eligible(monkeypatch):
    huge = candidate("Huge-70B", 40)
    unsafe = candidate("Model-Uncensored-7B", 4)
    monkeypatch.setattr(
        compare.predictor,
        "rank_candidates",
        lambda artifact, hw: [(unsafe, 50.0), (huge, 12.0)],
    )
    result = compare.compare_candidates(artifact(huge, unsafe), ["Huge-70B", "Model-Uncensored-7B"], hardware())
    assert result.best_fit_ref is None
    assert all(not row.eligible for row in result.rows)


def test_installation_state_is_reported_without_changing_ranking(monkeypatch):
    one = candidate("One-7B", 4)
    two = candidate("Two-7B", 5)
    monkeypatch.setattr(
        compare.predictor,
        "rank_candidates",
        lambda artifact, hw: [(one, 20.0), (two, 18.0)],
    )
    installed = recommend_status.InstallationStatus(True, True, ("ollama",), one["filename"], "exact")
    result = compare.compare_candidates(
        artifact(one, two), ["One-7B", "Two-7B"], hardware(), installations=[installed, recommend_status.NOT_INSTALLED]
    )
    assert result.rows[0].installed is True
    assert result.rows[0].installed_engines == ("ollama",)
