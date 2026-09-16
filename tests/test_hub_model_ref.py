"""Tests for hub.parse_model_ref (issue #340): a provider web-page URL pasted
out of a browser has to normalize into the same canonical ref every command
already understands, and a repo that exists but holds no .gguf has to say so
instead of "not found"."""

from __future__ import annotations

import json

import pytest
import requests

from omm import hub
from omm.providers import huggingface, modelscope
from omm.providers.base import ModelResolutionError, NoGgufFilesError


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        # HuggingFace model page, with and without a scheme or www.
        ("https://huggingface.co/openbmb/MiniCPM5-2B", "hf:openbmb/MiniCPM5-2B"),
        ("http://huggingface.co/openbmb/MiniCPM5-2B", "hf:openbmb/MiniCPM5-2B"),
        ("https://www.huggingface.co/openbmb/MiniCPM5-2B", "hf:openbmb/MiniCPM5-2B"),
        ("huggingface.co/openbmb/MiniCPM5-2B", "hf:openbmb/MiniCPM5-2B"),
        ("hf.co/openbmb/MiniCPM5-2B", "hf:openbmb/MiniCPM5-2B"),
        ("  https://huggingface.co/openbmb/MiniCPM5-2B  ", "hf:openbmb/MiniCPM5-2B"),
        # Query strings and the file-browser views all still name the repo.
        ("https://huggingface.co/openbmb/MiniCPM5-2B?library=true", "hf:openbmb/MiniCPM5-2B"),
        ("https://huggingface.co/openbmb/MiniCPM5-2B/tree/main", "hf:openbmb/MiniCPM5-2B"),
        ("https://huggingface.co/api/models/openbmb/MiniCPM5-2B", "hf:openbmb/MiniCPM5-2B"),
        # A .gguf file page pins the filename too, nested paths included.
        ("https://huggingface.co/org/repo/blob/main/m-Q4_K_M.gguf", "hf:org/repo:m-Q4_K_M.gguf"),
        ("https://huggingface.co/org/repo/resolve/main/sub/m.gguf", "hf:org/repo:sub/m.gguf"),
        ("https://huggingface.co/org/repo/blob/main/a%20b.gguf", "hf:org/repo:a b.gguf"),
        # A non-GGUF file page still identifies the repo rather than failing.
        ("https://huggingface.co/org/repo/blob/main/README.md", "hf:org/repo"),
        # ModelScope's own page shapes.
        ("https://modelscope.cn/models/qwen/Qwen2-7B", "ms:qwen/Qwen2-7B"),
        ("https://modelscope.cn/models/qwen/Qwen2-7B/files", "ms:qwen/Qwen2-7B"),
        ("modelscope.cn/models/qwen/Qwen2-7B/summary", "ms:qwen/Qwen2-7B"),
        ("https://modelscope.cn/models/qwen/Qwen2-7B/resolve/master/q.gguf", "ms:qwen/Qwen2-7B:q.gguf"),
        ("https://modelscope.cn/models/qwen/Qwen2-7B/file/view/master/q.gguf", "ms:qwen/Qwen2-7B:q.gguf"),
    ],
)
def test_provider_page_urls_normalize_to_canonical_refs(pasted, expected):
    assert hub.parse_model_ref(pasted).text == expected


@pytest.mark.parametrize(
    "unchanged",
    [
        "tinyllama-1.1b-q4",
        "org/repo",
        "org/repo:model.gguf",
        "hf:org/repo:model.gguf",
        "ms:org/repo",
        # Not a model page: HF namespaces datasets/spaces under their own root.
        "https://huggingface.co/datasets/openbmb/Ultra-FineWeb",
        "https://huggingface.co/spaces/org/demo",
        # Some other host's direct download - still the direct-URL contract.
        "https://example.com/model.gguf",
        # ModelScope's REST download endpoint already is a direct URL.
        "https://modelscope.cn/api/v1/models/org/repo/repo?Revision=master&FilePath=m.gguf",
    ],
)
def test_non_page_references_pass_through_untouched(unchanged):
    ref = hub.parse_model_ref(unchanged)
    assert ref.text == unchanged
    assert ref.repo_id is None


def test_pinned_direct_url_keeps_its_digest_instead_of_becoming_a_repo_ref():
    digest = "a" * 64
    pinned = f"https://huggingface.co/org/repo/resolve/main/m.gguf#sha256={digest}"

    assert hub.parse_model_ref(pinned).text == pinned

    resolved = hub.resolve_model(pinned)
    assert resolved.expected_sha256 == digest
    assert resolved.repo_id is None


def test_search_text_is_the_repo_name_so_search_can_take_a_url():
    assert hub.parse_model_ref("https://huggingface.co/openbmb/MiniCPM5-2B").search_text == (
        "MiniCPM5-2B"
    )
    assert hub.parse_model_ref("minicpm gguf").search_text == "minicpm gguf"


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_reference_is_rejected(bad):
    with pytest.raises(ModelResolutionError, match="empty"):
        hub.parse_model_ref(bad)


def test_non_text_reference_is_rejected():
    with pytest.raises(ModelResolutionError, match="text"):
        hub.parse_model_ref(None)


def test_url_with_unsafe_repo_segment_is_still_rejected():
    with pytest.raises(ModelResolutionError, match="safe"):
        hub.parse_model_ref("https://huggingface.co/..%2F..%2Fetc/repo")


def test_pasted_url_resolves_the_same_file_as_the_equivalent_ref(monkeypatch):
    monkeypatch.setattr(
        huggingface,
        "fetch_repo_files",
        lambda repo_id: (["model-Q4_K_M.gguf"], None),
    )

    resolved = hub.resolve_model("https://huggingface.co/org/repo/tree/main")

    assert resolved.provider == "huggingface"
    assert resolved.repo_id == "org/repo"
    assert resolved.filename == "model-Q4_K_M.gguf"
    assert resolved.url == "https://huggingface.co/org/repo/resolve/main/model-Q4_K_M.gguf"


# --- "the repo is real, it just has no GGUF" -------------------------------


def _no_quantization_tree(monkeypatch):
    monkeypatch.setattr(huggingface, "fetch_gguf_quantizations", lambda repo_id, limit=3: [])


def test_repo_without_gguf_says_so_instead_of_not_found(monkeypatch):
    monkeypatch.setattr(huggingface, "fetch_repo_files", lambda repo_id: ([], None))
    monkeypatch.setattr(
        huggingface,
        "fetch_gguf_quantizations",
        lambda repo_id, limit=3: ["bartowski/MiniCPM5-2B-GGUF", "mradermacher/MiniCPM5-2B-i1-GGUF"],
    )

    with pytest.raises(NoGgufFilesError) as caught:
        hub.resolve_model("https://huggingface.co/openbmb/MiniCPM5-2B")

    error = caught.value
    assert error.repo_id == "openbmb/MiniCPM5-2B"
    assert error.kind == "no_gguf"
    assert "has no .gguf file" in str(error)
    assert "not found" not in str(error)
    assert error.suggestions == [
        "bartowski/MiniCPM5-2B-GGUF",
        "mradermacher/MiniCPM5-2B-i1-GGUF",
    ]
    assert "bartowski/MiniCPM5-2B-GGUF" in (error.fix or "")


def test_bare_repo_without_gguf_on_either_provider_reports_the_one_that_has_it(monkeypatch):
    monkeypatch.setattr(huggingface, "fetch_repo_files", lambda repo_id: ([], None))
    monkeypatch.setattr(
        modelscope,
        "fetch_repo_files",
        lambda repo_id: (_ for _ in ()).throw(
            ModelResolutionError("not found", kind="not_found")
        ),
    )
    _no_quantization_tree(monkeypatch)

    with pytest.raises(NoGgufFilesError) as caught:
        hub.resolve_model("openbmb/MiniCPM5-2B")

    assert caught.value.provider == "huggingface"
    assert "HuggingFace" in str(caught.value)
    # Nothing to suggest - the fix still points at a way to find a GGUF build.
    assert caught.value.suggestions == []
    assert "omm search MiniCPM5-2B GGUF" in (caught.value.fix or "")


def test_repo_missing_everywhere_still_reports_not_found(monkeypatch):
    for module in (huggingface, modelscope):
        monkeypatch.setattr(
            module,
            "fetch_repo_files",
            lambda repo_id: (_ for _ in ()).throw(
                ModelResolutionError("not found", kind="not_found")
            ),
        )

    with pytest.raises(ModelResolutionError, match="was not found on HuggingFace or ModelScope"):
        hub.resolve_model("openbmb/nope-does-not-exist")


def test_quantization_refs_are_empty_for_a_provider_without_a_model_tree():
    assert hub.gguf_quantization_refs("modelscope", "org/repo") == []


def test_quantization_refs_drop_unsafe_ids_from_the_provider(monkeypatch):
    monkeypatch.setattr(
        huggingface,
        "fetch_gguf_quantizations",
        lambda repo_id, limit=3: ["../../etc/passwd", "good/One-GGUF"],
    )

    assert hub.gguf_quantization_refs("huggingface", "org/repo") == ["good/One-GGUF"]


# --- the HuggingFace model-tree query itself -------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self.headers = {}
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        pass

    def close(self):
        pass


def test_fetch_gguf_quantizations_asks_hf_for_gguf_children_by_downloads(monkeypatch):
    seen: dict = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["params"] = kwargs.get("params")
        return _FakeResponse(
            [
                {"id": "bartowski/MiniCPM5-2B-GGUF", "downloads": 9072},
                {"modelId": "other/MiniCPM5-2B-GGUF", "downloads": 12},
                {"id": "bartowski/MiniCPM5-2B-GGUF"},  # duplicate
                "not a dict",
            ]
        )

    monkeypatch.setattr(requests, "get", fake_get)

    refs = huggingface.fetch_gguf_quantizations("openbmb/MiniCPM5-2B", limit=3)

    assert refs == ["bartowski/MiniCPM5-2B-GGUF", "other/MiniCPM5-2B-GGUF"]
    assert seen["url"] == huggingface.HF_MODELS
    assert ("filter", "base_model:quantized:openbmb/MiniCPM5-2B") in seen["params"]
    assert ("filter", "gguf") in seen["params"]
    assert ("sort", "downloads") in seen["params"]


def test_fetch_gguf_quantizations_never_raises_on_a_bad_response(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )

    assert huggingface.fetch_gguf_quantizations("org/repo") == []
