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


def test_fetch_repo_param_count_never_raises_on_malformed_gguf_metadata(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, timeout: _FakeResponse(payload={"gguf": "not-an-object"}),
    )

    assert huggingface.fetch_repo_param_count_b("org/repo") is None


def test_download_url_quotes_filename_without_flattening_nested_paths():
    assert huggingface.download_url(
        "org/repo", "nested/model #1?.gguf"
    ) == (
        "https://huggingface.co/org/repo/resolve/main/"
        "nested/model%20%231%3F.gguf"
    )


def test_fetch_repo_files_accepts_case_insensitive_gguf_suffix(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, timeout: _FakeResponse(
            payload={"siblings": [{"rfilename": "MODEL.GGUF"}, {"rfilename": "README.md"}]}
        ),
    )

    files, _ = huggingface.fetch_repo_files("org/repo")

    assert files == ["MODEL.GGUF"]


# Shape recorded from a live GET https://huggingface.co/api/models/
# bartowski/Qwen2.5-7B-Instruct-GGUF (2026-09-01) - trimmed to the keys
# fetch_repo_metadata reads.
_METADATA_PAYLOAD = {
    "author": "bartowski",
    "downloads": 319887,
    "likes": 74,
    "pipeline_tag": "text-generation",
    "lastModified": "2024-09-19T12:54:25.000Z",
    "gated": False,
    "tags": ["gguf", "license:apache-2.0", "region:us"],
    "cardData": {"base_model": "Qwen/Qwen2.5-7B-Instruct", "license": "apache-2.0"},
    "gguf": {"total": 7615616512, "architecture": "qwen2", "context_length": 32768},
}


def test_fetch_repo_metadata_reads_the_live_payload_shape(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, timeout: _FakeResponse(payload=_METADATA_PAYLOAD)
    )

    metadata = huggingface.fetch_repo_metadata("org/repo")

    assert metadata == {
        "author": "bartowski",
        "downloads": 319887,
        "likes": 74,
        "license": "apache-2.0",
        "task": "text-generation",
        "base_model": "Qwen/Qwen2.5-7B-Instruct",
        "architecture": "qwen2",
        "context_length": 32768,
        "last_modified": "2024-09-19T12:54:25.000Z",
        "url": "https://huggingface.co/org/repo",
    }


def test_fetch_repo_metadata_falls_back_to_the_license_tag(monkeypatch):
    payload = {**_METADATA_PAYLOAD, "cardData": {}}
    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse(payload=payload))

    assert huggingface.fetch_repo_metadata("org/repo")["license"] == "apache-2.0"


def test_fetch_repo_metadata_keeps_a_gated_repo_flagged(monkeypatch):
    payload = {**_METADATA_PAYLOAD, "gated": "manual"}
    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse(payload=payload))

    assert huggingface.fetch_repo_metadata("org/repo")["gated"] == "manual"


def test_fetch_repo_metadata_omits_keys_the_repo_has_no_value_for(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse(payload={}))

    metadata = huggingface.fetch_repo_metadata("org/repo")

    assert metadata == {"url": "https://huggingface.co/org/repo"}


def test_fetch_repo_metadata_returns_empty_instead_of_raising(monkeypatch):
    def _explode(url, timeout):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", _explode)

    assert huggingface.fetch_repo_metadata("org/repo") == {}


def test_fetch_repo_metadata_returns_empty_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, timeout: _FakeResponse(json_error=ValueError("bad json"))
    )

    assert huggingface.fetch_repo_metadata("org/repo") == {}
