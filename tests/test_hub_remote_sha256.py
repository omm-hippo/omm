import requests

from omm.hub import remote_file_sha256


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_remote_file_sha256_returns_lfs_hash(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json, timeout: _FakeResponse(
            [{"path": "model.gguf", "lfs": {"sha256": "deadbeef"}}]
        ),
    )

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") == "deadbeef"


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


def test_remote_file_sha256_returns_none_on_request_error(monkeypatch):
    def _raise(url, json, timeout):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "post", _raise)

    assert remote_file_sha256("huggingface", "org/repo", "model.gguf") is None
