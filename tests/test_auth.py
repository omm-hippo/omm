from __future__ import annotations

from omm import auth


def test_env_token_becomes_bearer_header(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "top-secret")
    assert auth.huggingface_headers() == {"Authorization": "Bearer top-secret"}


def test_no_env_token_means_no_header(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert auth.huggingface_headers() == {}


def test_header_never_crosses_to_a_redirect_host(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "top-secret")
    assert auth.headers_for_url("https://huggingface.co/org/repo") == {
        "Authorization": "Bearer top-secret"
    }
    assert auth.headers_for_url("https://hf.co/org/repo") == {
        "Authorization": "Bearer top-secret"
    }
    assert auth.headers_for_url("https://cdn-lfs.huggingface.co/blob") == {
        "Authorization": "Bearer top-secret"
    }
    assert auth.headers_for_url("https://some-other-cdn.example/model.gguf") == {}
    assert auth.headers_for_url("https://notarealhuggingface.co/x") == {}


def test_blank_or_control_character_token_is_rejected(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "   ")
    assert auth.huggingface_headers() == {}
    monkeypatch.setenv("HF_TOKEN", "bad\ntoken")
    assert auth.huggingface_headers() == {}


def test_oversized_token_is_rejected(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "x" * 4097)
    assert auth.huggingface_headers() == {}
