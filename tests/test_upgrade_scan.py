"""Unit tests for the pure `omm upgrade` scan logic in `src/omm/upgrade.py`.

Every network/prediction dependency is injected as a plain callable, so
these tests use lambdas/stubs instead of monkeypatching real modules.
"""

from __future__ import annotations

import pytest

from omm.hub import ModelResolutionError
from omm import upgrade


# ---------------------------------------------------------------------------
# find_quant_upgrade
# ---------------------------------------------------------------------------


def _list_repo_files(filenames):
    def _fn(provider, repo_id):
        return list(filenames), None

    return _fn


def _file_size(sizes: dict[str, int]):
    def _fn(provider, repo_id, filename):
        return sizes.get(filename)

    return _fn


def _fits_all(candidate: dict) -> bool:
    return True


def _predict_none(candidate: dict) -> float | None:
    return None


def test_quant_upgrade_picks_highest_bits_within_budget():
    filenames = [
        "model-Q4_K_M.gguf",  # installed
        "model-Q5_K_M.gguf",
        "model-Q6_K.gguf",
        "model-Q8_0.gguf",
    ]

    def fits_budget(candidate: dict) -> bool:
        return candidate["filename"] != "model-Q8_0.gguf"

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=_file_size({}),
        predict_tps=_predict_none,
        fits_budget=fits_budget,
    )

    assert result is not None
    assert result.filename == "model-Q6_K.gguf"
    assert result.kind == upgrade.KIND_QUANT
    assert result.ref == "hf:org/repo:model-Q6_K.gguf"


def test_quant_upgrade_excludes_lower_or_equal_bits():
    filenames = [
        "model-Q4_K_M.gguf",  # installed
        "model-Q2_K.gguf",
        "model-Q3_K_M.gguf",
        "model-Q4_K_S.gguf",
    ]
    calls: list[tuple[str, str, str]] = []

    def file_size(provider, repo_id, filename):
        calls.append((provider, repo_id, filename))
        return 1

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=file_size,
        predict_tps=_predict_none,
        fits_budget=_fits_all,
    )

    assert result is None
    assert calls == []


def test_quant_upgrade_excludes_candidates_over_budget():
    filenames = ["model-Q4_K_M.gguf", "model-Q6_K.gguf"]

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=_file_size({}),
        predict_tps=_predict_none,
        fits_budget=lambda candidate: False,
    )

    assert result is None


def test_quant_upgrade_tiebreaks_by_speed_then_size():
    # Same bits (Q6_K), different predicted speed -> faster one wins.
    filenames = ["model-Q4_K_M.gguf", "model-a-Q6_K.gguf", "model-b-Q6_K.gguf"]
    tps = {"model-a-Q6_K.gguf": 30.0, "model-b-Q6_K.gguf": 10.0}

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=_file_size({}),
        predict_tps=lambda candidate: tps[candidate["filename"]],
        fits_budget=_fits_all,
    )

    assert result is not None
    assert result.filename == "model-a-Q6_K.gguf"

    # Same bits, same speed -> smaller file size wins.
    sizes = {"model-a-Q6_K.gguf": 2000, "model-b-Q6_K.gguf": 1000}
    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=_file_size(sizes),
        predict_tps=lambda candidate: 20.0,
        fits_budget=_fits_all,
    )

    assert result is not None
    assert result.filename == "model-b-Q6_K.gguf"


def test_quant_upgrade_skips_unparseable_mmproj_and_shard_files():
    filenames = [
        "model-Q4_K_M.gguf",  # installed
        "mmproj-model-f16.gguf",
        "model-00001-of-00003.gguf",
        "random-name.gguf",
    ]

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=_file_size({}),
        predict_tps=_predict_none,
        fits_budget=_fits_all,
    )

    assert result is None


def test_quant_upgrade_returns_none_when_installed_quant_unparseable():
    calls: list[tuple[str, str]] = []

    def list_repo_files(provider, repo_id):
        calls.append((provider, repo_id))
        return [], None

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="weird-name.gguf",
        list_repo_files=list_repo_files,
        file_size=_file_size({}),
        predict_tps=_predict_none,
        fits_budget=_fits_all,
    )

    assert result is None
    assert calls == []


def test_quant_upgrade_propagates_provider_errors():
    def list_repo_files(provider, repo_id):
        raise ModelResolutionError("boom")

    with pytest.raises(ModelResolutionError):
        upgrade.find_quant_upgrade(
            provider="huggingface",
            repo_id="org/repo",
            installed_filename="model-Q4_K_M.gguf",
            list_repo_files=list_repo_files,
            file_size=_file_size({}),
            predict_tps=_predict_none,
            fits_budget=_fits_all,
        )


def test_quant_upgrade_excludes_candidate_when_fits_budget_is_none_treated_as_false():
    # Edge case: a fits_budget stub that fails closed on an unknown size
    # (size_bytes=None) must exclude the candidate, not crash.
    filenames = ["model-Q4_K_M.gguf", "model-Q6_K.gguf"]

    def fits_budget(candidate: dict) -> bool:
        return candidate["size_bytes"] is not None

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=_file_size({}),  # no entry -> None
        predict_tps=_predict_none,
        fits_budget=fits_budget,
    )

    assert result is None


def test_quant_upgrade_dedupes_case_only_filename_variants():
    # HF allows filenames that only differ by case; treat them as the same
    # candidate rather than double-counting / double-fetching.
    filenames = ["model-Q4_K_M.gguf", "model-Q6_K.GGUF", "MODEL-Q6_K.gguf"]
    calls: list[str] = []

    def file_size(provider, repo_id, filename):
        calls.append(filename)
        return 1

    result = upgrade.find_quant_upgrade(
        provider="huggingface",
        repo_id="org/repo",
        installed_filename="model-Q4_K_M.gguf",
        list_repo_files=_list_repo_files(filenames),
        file_size=file_size,
        predict_tps=_predict_none,
        fits_budget=_fits_all,
    )

    assert result is not None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# find_successor
# ---------------------------------------------------------------------------


def test_find_successor_matches_exact_coordinates():
    candidates = [
        {
            "name": "llama3.1-8b-instruct-q4",
            "repo_id": "org/llama31",
            "filename": "llama-3.1-8b-Q4_K_M.gguf",
            "provider": "huggingface",
        },
        {
            "name": "llama3.3-8b-instruct-q4",
            "repo_id": "org/llama33",
            "filename": "llama-3.3-8b-Q4_K_M.gguf",
            "provider": "huggingface",
            "supersedes": ["llama3.1-8b-instruct-q4"],
        },
    ]

    result = upgrade.find_successor(
        candidates,
        repo_id="org/llama31",
        filename="llama-3.1-8b-Q4_K_M.gguf",
        provider="huggingface",
    )

    assert result is not None
    assert result["name"] == "llama3.3-8b-instruct-q4"


def test_find_successor_returns_none_when_installed_model_not_curated():
    candidates = [
        {
            "name": "llama3.1-8b-instruct-q4",
            "repo_id": "org/llama31",
            "filename": "llama-3.1-8b-Q4_K_M.gguf",
            "provider": "huggingface",
        },
    ]

    result = upgrade.find_successor(
        candidates,
        repo_id="someone-else/repo",
        filename="not-in-list.gguf",
        provider="huggingface",
    )

    assert result is None


def test_find_successor_picks_first_of_multiple_claimants():
    candidates = [
        {
            "name": "old-model",
            "repo_id": "org/old",
            "filename": "old.gguf",
            "provider": "huggingface",
        },
        {
            "name": "claimant-a",
            "repo_id": "org/a",
            "filename": "a.gguf",
            "provider": "huggingface",
            "supersedes": ["old-model"],
        },
        {
            "name": "claimant-b",
            "repo_id": "org/b",
            "filename": "b.gguf",
            "provider": "huggingface",
            "supersedes": ["old-model"],
        },
    ]

    result = upgrade.find_successor(
        candidates,
        repo_id="org/old",
        filename="old.gguf",
        provider="huggingface",
    )

    assert result is not None
    assert result["name"] == "claimant-a"


def test_find_successor_ignores_non_list_supersedes():
    candidates = [
        {
            "name": "old-model",
            "repo_id": "org/old",
            "filename": "old.gguf",
            "provider": "huggingface",
        },
        {
            "name": "bad-string",
            "repo_id": "org/bad1",
            "filename": "bad1.gguf",
            "provider": "huggingface",
            "supersedes": "old-model",
        },
        {
            "name": "bad-none",
            "repo_id": "org/bad2",
            "filename": "bad2.gguf",
            "provider": "huggingface",
            "supersedes": None,
        },
        {
            "name": "bad-number",
            "repo_id": "org/bad3",
            "filename": "bad3.gguf",
            "provider": "huggingface",
            "supersedes": 123,
        },
    ]

    result = upgrade.find_successor(
        candidates,
        repo_id="org/old",
        filename="old.gguf",
        provider="huggingface",
    )

    assert result is None


def test_find_successor_defaults_missing_provider_to_huggingface():
    # candidates.json entries only ever carry ['description', 'filename',
    # 'name', 'provider', 'repo_id'] today, but be defensive about a
    # missing provider field either on the installed lookup or the
    # candidate entries themselves.
    candidates = [
        {"name": "old-model", "repo_id": "org/old", "filename": "old.gguf"},
        {
            "name": "new-model",
            "repo_id": "org/new",
            "filename": "new.gguf",
            "supersedes": ["old-model"],
        },
    ]

    result = upgrade.find_successor(
        candidates,
        repo_id="org/old",
        filename="old.gguf",
        provider="huggingface",
    )

    assert result is not None
    assert result["name"] == "new-model"


def test_find_successor_skips_self_succession():
    # A malformed candidates.json where a *different* entry object shares
    # the installed model's exact coordinates (duplicate row) and claims to
    # supersede it must not be returned as its own successor.
    candidates = [
        {
            "name": "old-model",
            "repo_id": "org/old",
            "filename": "old.gguf",
            "provider": "huggingface",
        },
        {
            "name": "old-model-duplicate-row",
            "repo_id": "org/old",
            "filename": "old.gguf",
            "provider": "huggingface",
            "supersedes": ["old-model"],
        },
    ]

    result = upgrade.find_successor(
        candidates,
        repo_id="org/old",
        filename="old.gguf",
        provider="huggingface",
    )

    assert result is None


# ---------------------------------------------------------------------------
# provider_prefix / quant_label
# ---------------------------------------------------------------------------


def test_provider_prefix_maps_both_providers():
    assert upgrade.provider_prefix("huggingface") == "hf"
    assert upgrade.provider_prefix("modelscope") == "ms"


def test_quant_label_extracts_token_or_falls_back_to_bits():
    assert upgrade.quant_label("model-Q4_K_M.gguf") == "Q4_K_M"
    assert upgrade.quant_label("model-IQ2_XS.gguf") is not None
    assert upgrade.quant_label("totally-unparseable.gguf") is None
