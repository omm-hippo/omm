import json

import requests

from omm.httpjson import MAX_PROVIDER_RESPONSE_BYTES
from omm.providers import huggingface
from omm.providers.base import ModelResolutionError


class _FakeResponse:
    def __init__(self, *, json_error=None, payload=None):
        self._json_error = json_error
        self._payload = payload
        self.headers = {}
        # read_bounded_json_response reads `.content` (no iter_content on
        # these fakes), then json.loads()s it - json_error is simulated with
        # bytes that fail to parse rather than a raise from `.json()`.
        self.content = b"not valid json" if json_error is not None else json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        pass

    def close(self):
        pass

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeErrorResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        raise requests.HTTPError(response=self)


def test_fetch_repo_files_404_is_kind_not_found(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeErrorResponse(404))

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError as e:
        assert e.kind == "not_found"


def test_fetch_repo_files_503_is_kind_unavailable(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeErrorResponse(503))

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError as e:
        assert e.kind == "unavailable"


def test_fetch_repo_files_raises_model_resolution_error_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(json_error=ValueError("bad json"))
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
        lambda *a, **k: _FakeResponse(payload={"siblings": [{"unexpected": "shape"}]}),
    )

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass


def test_remote_file_sha256_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: _FakeResponse(json_error=ValueError("bad json"))
    )

    assert huggingface.remote_file_sha256("org/repo", "model.gguf") is None


def test_remote_file_sha256_raises_on_request_failure_instead_of_returning_none(monkeypatch):
    # A transient failure (timeout, 429, 5xx, DNS) must be distinguishable
    # from a legitimate "no LFS hash" result - both used to collapse to None,
    # which made `omm install` hard-fail with "did not provide a SHA-256
    # digest" on a simple rate limit or network blip.
    def _raise_timeout(*a, **k):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "post", _raise_timeout)

    try:
        huggingface.remote_file_sha256("org/repo", "model.gguf")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass


def test_fetch_repo_param_count_never_raises_on_malformed_gguf_metadata(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResponse(payload={"gguf": "not-an-object"}),
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
        lambda *a, **k: _FakeResponse(
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
        requests, "get", lambda *a, **k: _FakeResponse(payload=_METADATA_PAYLOAD)
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
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload=payload))

    assert huggingface.fetch_repo_metadata("org/repo")["license"] == "apache-2.0"


def test_fetch_repo_metadata_keeps_a_gated_repo_flagged(monkeypatch):
    payload = {**_METADATA_PAYLOAD, "gated": "manual"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload=payload))

    assert huggingface.fetch_repo_metadata("org/repo")["gated"] == "manual"


def test_legacy_boolean_gated_is_reported(monkeypatch):
    payload = {**_METADATA_PAYLOAD, "gated": True}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload=payload))

    assert huggingface.fetch_repo_metadata("org/repo")["gated"] == "yes"


def test_gated_false_stays_absent(monkeypatch):
    payload = {**_METADATA_PAYLOAD, "gated": False}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload=payload))

    assert "gated" not in huggingface.fetch_repo_metadata("org/repo")


def test_gated_manual_is_preserved(monkeypatch):
    payload = {**_METADATA_PAYLOAD, "gated": "manual"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload=payload))

    assert huggingface.fetch_repo_metadata("org/repo")["gated"] == "manual"


def test_fetch_repo_metadata_omits_keys_the_repo_has_no_value_for(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload={}))

    metadata = huggingface.fetch_repo_metadata("org/repo")

    assert metadata == {"url": "https://huggingface.co/org/repo"}


def test_fetch_repo_metadata_returns_empty_instead_of_raising(monkeypatch):
    def _explode(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", _explode)

    assert huggingface.fetch_repo_metadata("org/repo") == {}


def test_fetch_repo_metadata_returns_empty_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(json_error=ValueError("bad json"))
    )

    assert huggingface.fetch_repo_metadata("org/repo") == {}


def test_remote_file_sha256_matches_the_requested_path(monkeypatch):
    payload = [
        {"path": "other.gguf", "lfs": {"oid": "a" * 64}},
        {"path": "nested/model.gguf", "lfs": {"oid": "b" * 64}},
    ]
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: _FakeResponse(payload=payload),
    )

    assert huggingface.remote_file_sha256("org/repo", "nested/model.gguf") == "b" * 64


class _OversizedResponse:
    """Declares a Content-Length past MAX_PROVIDER_RESPONSE_BYTES - the
    bounded reader must reject it before ever touching the body."""

    headers = {"Content-Length": str(MAX_PROVIDER_RESPONSE_BYTES + 1)}
    content = b"{}"

    def raise_for_status(self):
        pass

    def close(self):
        pass


def test_fetch_repo_files_rejects_response_over_the_size_limit(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _OversizedResponse())

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass
