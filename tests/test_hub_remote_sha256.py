import requests

from omm.hub import remote_file_sha256
from omm.providers.base import ModelResolutionError


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_remote_file_sha256_returns_lfs_hash(monkeypatch):
    digest = "a" * 64
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json, timeout: _FakeResponse(
            [
                {
                    "path": "model.gguf",
                    "size": 123,
                    "lfs": {"oid": digest, "size": 123, "pointerSize": 130},
                    "xetHash": "d" * 64,
                }
            ]
        ),
    )

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") == digest


def test_remote_file_sha256_accepts_prefixed_lfs_oid(monkeypatch):
    digest = "b" * 64
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json, timeout: _FakeResponse(
            [{"path": "model.gguf", "lfs": {"oid": f"sha256:{digest}"}}]
        ),
    )

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") == digest


def test_remote_file_sha256_accepts_legacy_sha256_field(monkeypatch):
    digest = "c" * 64
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json, timeout: _FakeResponse(
            [{"path": "model.gguf", "lfs": {"sha256": digest}}]
        ),
    )

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") == digest


def test_remote_file_sha256_rejects_invalid_lfs_oid(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json, timeout: _FakeResponse(
            [{"path": "model.gguf", "lfs": {"oid": "not-a-sha256"}}]
        ),
    )

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") is None


def test_remote_file_sha256_returns_none_when_not_lfs(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json, timeout: _FakeResponse([{"path": "model.gguf"}]),
    )

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") is None


def test_remote_file_sha256_returns_none_when_path_missing(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeResponse([]))

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") is None


def test_remote_file_sha256_raises_on_request_error(monkeypatch):
    # A transient request failure must stay distinguishable from a genuine
    # "this file has no LFS hash" answer: None means only the latter now, so
    # a rate limit or network blip no longer reads as an unverifiable model.
    def _raise(url, json, timeout):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "post", _raise)

    try:
        remote_file_sha256("huggingface", "org/repo", "model.gguf")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass
