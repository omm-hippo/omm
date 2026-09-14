"""Tests for hub.resolve_model's provider dispatch across HuggingFace and
ModelScope: explicit prefixes, and bare org/repo refs that must query both
providers and disambiguate."""

from __future__ import annotations

import pytest

from omm import hub
from omm.providers import huggingface, modelscope
from omm.providers.base import AmbiguousModelError, AmbiguousProviderError, ModelResolutionError


def _stub_fetch_repo_files(monkeypatch, module, files_by_repo: dict[str, list[str]]):
    def fake(repo_id):
        if repo_id not in files_by_repo:
            raise ModelResolutionError(f"not found: {repo_id}")
        return files_by_repo[repo_id], None

    monkeypatch.setattr(module, "fetch_repo_files", fake)


def test_explicit_ms_prefix_with_filename_resolves_without_network(monkeypatch):
    resolved = hub.resolve_model("ms:org/repo:model-q4_k_m.gguf")
    assert resolved.provider == "modelscope"
    assert resolved.repo_id == "org/repo"
    assert resolved.filename == "model-q4_k_m.gguf"
    assert resolved.url == (
        "https://modelscope.cn/api/v1/models/org/repo/repo"
        "?Revision=master&FilePath=model-q4_k_m.gguf"
    )


def test_explicit_hf_prefix_still_works(monkeypatch):
    resolved = hub.resolve_model("hf:org/repo:model.gguf")
    assert resolved.provider == "huggingface"
    assert resolved.url == "https://huggingface.co/org/repo/resolve/main/model.gguf"


def test_repo_file_listing_deduplicates_case_insensitive_names(monkeypatch):
    _stub_fetch_repo_files(
        monkeypatch,
        huggingface,
        {"org/repo": ["Model.gguf", "model.gguf"]},
    )

    resolved = hub.resolve_model("hf:org/repo")

    assert resolved.filename == "Model.gguf"


def test_bare_repo_resolves_to_sole_matching_provider(monkeypatch):
    _stub_fetch_repo_files(monkeypatch, huggingface, {})
    _stub_fetch_repo_files(monkeypatch, modelscope, {"org/only-on-ms": ["model.gguf"]})
    resolved = hub.resolve_model("org/only-on-ms")
    assert resolved.provider == "modelscope"
    assert resolved.filename == "model.gguf"
    assert resolved.note is None


def test_bare_repo_reports_outage_instead_of_not_found(monkeypatch):
    def boom(repo_id):
        raise ModelResolutionError("boom", kind="unavailable")

    monkeypatch.setattr(huggingface, "fetch_repo_files", boom)
    monkeypatch.setattr(modelscope, "fetch_repo_files", boom)

    with pytest.raises(ModelResolutionError) as exc_info:
        hub.resolve_model("org/repo")

    message = str(exc_info.value)
    assert "not found" not in message
    assert "Could not check" in message
    assert "hf:org/repo" in exc_info.value.fix


def test_bare_repo_notes_when_other_provider_unavailable(monkeypatch):
    def hf_boom(repo_id):
        raise ModelResolutionError("HF is down", kind="unavailable")

    _stub_fetch_repo_files(monkeypatch, modelscope, {"org/repo": ["model.gguf"]})
    monkeypatch.setattr(huggingface, "fetch_repo_files", hf_boom)

    resolved = hub.resolve_model("org/repo")

    assert resolved.provider == "modelscope"
    assert resolved.filename == "model.gguf"
    assert resolved.note is not None
    assert "could not be checked" in resolved.note


def test_bare_repo_on_both_providers_raises_ambiguous_provider_error(monkeypatch):
    _stub_fetch_repo_files(monkeypatch, huggingface, {"org/repo": ["model.gguf"]})
    _stub_fetch_repo_files(monkeypatch, modelscope, {"org/repo": ["model.gguf"]})
    with pytest.raises(AmbiguousProviderError) as exc_info:
        hub.resolve_model("org/repo")
    assert set(exc_info.value.providers) == {"huggingface", "modelscope"}


def test_bare_repo_on_neither_provider_raises_model_resolution_error(monkeypatch):
    _stub_fetch_repo_files(monkeypatch, huggingface, {})
    _stub_fetch_repo_files(monkeypatch, modelscope, {})
    with pytest.raises(ModelResolutionError) as exc_info:
        hub.resolve_model("org/nowhere")
    assert "org/nowhere" in str(exc_info.value)
    assert exc_info.value.fix is not None


def test_unknown_bare_model_name_raises_with_fix_listing_alternatives():
    with pytest.raises(ModelResolutionError) as exc_info:
        hub.resolve_model("totally-bogus-name")
    assert "Unknown model 'totally-bogus-name'" in str(exc_info.value)
    assert "org/repo:file.gguf" in exc_info.value.fix


def test_url_from_known_modelscope_host_is_tagged(monkeypatch):
    resolved = hub.resolve_model(
        "https://modelscope.cn/api/v1/models/org/repo/repo?FilePath=x.gguf#sha256="
        + "a" * 64
    )
    assert resolved.provider == "modelscope"
    assert resolved.filename == "x.gguf"


def test_fetch_repo_files_routes_to_provider_module(monkeypatch):
    def fake(repo_id):
        return ["a.Q4_K_M.gguf", "a.Q8_0.gguf"], 7.0

    monkeypatch.setattr(huggingface, "fetch_repo_files", fake)

    files, param_count_b = hub.fetch_repo_files("huggingface", "org/repo")

    assert files == ["a.Q4_K_M.gguf", "a.Q8_0.gguf"]
    assert param_count_b == 7.0


def test_fetch_repo_metadata_routes_to_provider_module(monkeypatch):
    monkeypatch.setattr(huggingface, "fetch_repo_metadata", lambda repo_id: {"author": "hf"})
    monkeypatch.setattr(modelscope, "fetch_repo_metadata", lambda repo_id: {"author": "ms"})

    assert hub.fetch_repo_metadata("huggingface", "org/repo") == {"author": "hf"}
    assert hub.fetch_repo_metadata("modelscope", "org/repo") == {"author": "ms"}


def test_fetch_repo_metadata_rejects_an_unknown_provider():
    with pytest.raises(ModelResolutionError):
        hub.fetch_repo_metadata("nowhere", "org/repo")


def test_repo_listing_excludes_split_gguf_parts(monkeypatch):
    _stub_fetch_repo_files(
        monkeypatch,
        huggingface,
        {
            "org/repo": [
                "m-Q4_K_M.gguf",
                "m-Q8_0-00001-of-00002.gguf",
                "m-Q8_0-00002-of-00002.gguf",
            ]
        },
    )

    resolved = hub.resolve_model("hf:org/repo")

    assert resolved.filename == "m-Q4_K_M.gguf"


def test_repo_with_only_split_parts_raises_clear_error(monkeypatch):
    _stub_fetch_repo_files(
        monkeypatch,
        huggingface,
        {
            "org/repo": [
                "m-00001-of-00003.gguf",
                "m-00002-of-00003.gguf",
                "m-00003-of-00003.gguf",
            ]
        },
    )

    with pytest.raises(ModelResolutionError) as exc_info:
        hub.resolve_model("hf:org/repo")

    assert not isinstance(exc_info.value, AmbiguousModelError)
    assert "split" in str(exc_info.value)


def test_explicit_split_part_filename_is_rejected(monkeypatch):
    with pytest.raises(ModelResolutionError):
        hub.resolve_model("hf:org/repo:m-00001-of-00002.gguf")


def test_single_part_of_one_is_not_treated_as_shard(monkeypatch):
    _stub_fetch_repo_files(
        monkeypatch,
        huggingface,
        {"org/repo": ["m-00001-of-00001.gguf"]},
    )

    resolved = hub.resolve_model("hf:org/repo")

    assert resolved.filename == "m-00001-of-00001.gguf"
