import pytest

from omm import hub
from omm.hub import resolve_model
from omm.providers import huggingface
from omm.providers.base import ModelResolutionError


def test_resolve_model_bare_repo_with_filename_makes_zero_network_calls(monkeypatch):
    """org/repo:filename with no provider prefix is the most common install
    form and was zero-network-call in the pre-multi-provider hub.py: the
    filename is already fully known, so nothing needs to be listed or
    disambiguated. Guard against regressing back to the multi-provider probe
    loop (which called fetch_repo_files per provider even when filename was
    already given) by failing hard if fetch_repo_files is ever invoked."""

    def _fail_if_called(repo_id):
        raise AssertionError(
            f"fetch_repo_files({repo_id!r}) should not be called when the "
            "filename is already known - this is the fast path."
        )

    monkeypatch.setattr(huggingface, "fetch_repo_files", _fail_if_called)

    resolved = resolve_model("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")

    assert resolved.provider == "huggingface"
    assert resolved.repo_id == "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"
    assert resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    assert resolved.url == (
        "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/"
        "resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    )


def test_resolve_model_appends_gguf_suffix_when_missing():
    resolved = resolve_model("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Meta-Llama-3.1-8B-Instruct-Q4_K_M")

    assert resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    assert resolved.url.endswith("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")


def test_resolve_model_leaves_existing_gguf_suffix_untouched():
    resolved = resolve_model("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")

    assert resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"


@pytest.mark.parametrize(
    "reference",
    [
        "hf:org/repo:../../outside",
        "hf:org/repo:./model.gguf",
        "hf:org/repo:a/./model.gguf",
        "hf:org/repo:a//model.gguf",
        "hf:org/repo:e\u0301.gguf",
        r"hf:org/repo:..\..\outside.gguf",
        "hf:../repo:model.gguf",
        "hf:org/repo#fragment:model.gguf",
        "hf:org/repo?query:model.gguf",
        "hf:org/repo name:model.gguf",
        "https://example.com/not-a-model.txt",
        r"https://example.com/..\..\outside.gguf",
    ],
)
def test_resolve_model_rejects_paths_that_can_escape_managed_directories(reference):
    with pytest.raises(ModelResolutionError, match="safe|unsafe"):
        resolve_model(reference)


def test_resolve_model_allows_safe_nested_provider_filename():
    resolved = resolve_model("hf:org/repo:quantized/model.gguf")

    assert resolved.filename == "quantized/model.gguf"


@pytest.mark.parametrize(
    "provider", [["huggingface"], {"name": "huggingface"}, "unknown"]
)
def test_validate_provider_rejects_non_string_and_unknown_values(provider):
    with pytest.raises(ModelResolutionError):
        hub.validate_provider(provider)
