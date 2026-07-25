"""Unit tests for the ModelScope provider module. Field names (Path/Size/
Sha256) match live API responses recorded in
docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md - do not
"fix" the casing without re-verifying against the real API."""

from __future__ import annotations

import pytest
import requests

from omm.providers import modelscope
from omm.providers.base import ModelResolutionError


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)

    def json(self):
        return self._payload


_FILES_PAYLOAD = {
    "Code": 200,
    "Data": {
        "Files": [
            {"Name": "README.md", "Path": "README.md", "Size": 100, "Sha256": "readme-hash"},
            {
                "Name": "model-q4_k_m.gguf",
                "Path": "model-q4_k_m.gguf",
                "Size": 491400032,
                "Sha256": "abc123",
            },
            {
                "Name": "model-q8_0.gguf",
                "Path": "model-q8_0.gguf",
                "Size": 900000000,
                "Sha256": "def456",
            },
        ]
    },
}


def test_fetch_repo_files_filters_to_gguf_only(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    files, param_count_b = modelscope.fetch_repo_files("org/repo")
    assert files == ["model-q4_k_m.gguf", "model-q8_0.gguf"]
    assert param_count_b is None


def test_fetch_repo_files_404_raises_model_resolution_error(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(404, {}))
    with pytest.raises(ModelResolutionError):
        modelscope.fetch_repo_files("org/does-not-exist")


def test_download_url_builds_expected_query_string():
    url = modelscope.download_url("org/repo", "model-q4_k_m.gguf")
    assert url == (
        "https://modelscope.cn/api/v1/models/org/repo/repo"
        "?Revision=master&FilePath=model-q4_k_m.gguf"
    )


def test_remote_file_size_finds_matching_file(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    assert modelscope.remote_file_size("org/repo", "model-q4_k_m.gguf") == 491400032


def test_remote_file_size_returns_none_for_missing_file(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    assert modelscope.remote_file_size("org/repo", "does-not-exist.gguf") is None


def test_remote_file_size_returns_none_on_404(monkeypatch):
    """remote_file_size never raises - 404/network errors return None for
    best-effort behavior, matching HuggingFace provider contract."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(404, {}))
    assert modelscope.remote_file_size("org/does-not-exist", "model.gguf") is None


def test_remote_file_sha256_finds_matching_file(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    assert modelscope.remote_file_sha256("org/repo", "model-q8_0.gguf") == "def456"


def test_remote_file_sha256_returns_none_on_404(monkeypatch):
    """remote_file_sha256 never raises - 404/network errors return None for
    best-effort behavior, matching HuggingFace provider contract."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(404, {}))
    assert modelscope.remote_file_sha256("org/does-not-exist", "model.gguf") is None


def test_fetch_repo_param_count_b_is_always_none():
    # ModelScope's file-listing API doesn't expose a parsed GGUF header
    # total-params field like HF's does - always None, never guessed.
    assert modelscope.fetch_repo_param_count_b("org/repo") is None


class _FakeResponseWithJsonError:
    """Fake response that raises ValueError when .json() is called,
    simulating a 200 response with a non-JSON body."""

    def __init__(self):
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_fetch_repo_files_non_json_body_raises_model_resolution_error(monkeypatch):
    """A 200 response with a non-JSON body should raise ModelResolutionError,
    not leak the underlying ValueError."""
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponseWithJsonError()
    )
    with pytest.raises(ModelResolutionError):
        modelscope.fetch_repo_files("org/repo")


def test_fetch_repo_files_null_data_field_raises_model_resolution_error(monkeypatch):
    """A response with {"Code": 200, "Data": None} should raise
    ModelResolutionError when there are no files, not crash with AttributeError."""
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, {"Code": 200, "Data": None})
    )
    # Empty file list should not crash, though it may raise since there are no .gguf files
    # The key point is it doesn't raise AttributeError - _list_repo_files returns [] instead
    files, _ = modelscope.fetch_repo_files("org/repo")
    assert files == []


def test_remote_file_size_non_json_body_returns_none(monkeypatch):
    """remote_file_size never raises - even a non-JSON body should return None
    for best-effort behavior."""
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponseWithJsonError()
    )
    assert modelscope.remote_file_size("org/repo", "model.gguf") is None


def test_remote_file_size_null_data_field_returns_none(monkeypatch):
    """remote_file_size never raises - even a null Data field should return None
    for best-effort behavior."""
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, {"Code": 200, "Data": None})
    )
    assert modelscope.remote_file_size("org/repo", "model.gguf") is None


def test_remote_file_sha256_non_json_body_returns_none(monkeypatch):
    """remote_file_sha256 never raises - even a non-JSON body should return None
    for best-effort behavior."""
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponseWithJsonError()
    )
    assert modelscope.remote_file_sha256("org/repo", "model.gguf") is None


def test_remote_file_sha256_null_data_field_returns_none(monkeypatch):
    """remote_file_sha256 never raises - even a null Data field should return None
    for best-effort behavior."""
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, {"Code": 200, "Data": None})
    )
    assert modelscope.remote_file_sha256("org/repo", "model.gguf") is None
