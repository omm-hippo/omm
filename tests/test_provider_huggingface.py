import requests

from omm.providers import huggingface
from omm.providers.base import ModelResolutionError


class _FakeResponse:
    def __init__(self, *, json_error=None, payload=None):
        self._json_error = json_error
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def test_fetch_repo_files_raises_model_resolution_error_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, timeout: _FakeResponse(json_error=ValueError("bad json"))
    )

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass


def test_fetch_repo_files_raises_model_resolution_error_on_missing_key(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, timeout: _FakeResponse(payload={"siblings": [{"unexpected": "shape"}]}),
    )

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass


def test_remote_file_sha256_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda url, json, timeout: _FakeResponse(json_error=ValueError("bad json"))
    )

    assert huggingface.remote_file_sha256("org/repo", "model.gguf") is None


def test_download_url_quotes_filename_without_flattening_nested_paths():
    assert huggingface.download_url(
        "org/repo", "nested/model #1?.gguf"
    ) == (
        "https://huggingface.co/org/repo/resolve/main/"
        "nested/model%20%231%3F.gguf"
    )
