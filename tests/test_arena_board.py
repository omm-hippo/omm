"""Reading the published arena leaderboard (sub-project D, #410)."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
import requests

from omm import arena_board, config


def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(public).decode()


def _sign(private, content: bytes) -> dict:
    return {
        "schema_version": 1,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "signature": base64.b64encode(private.sign(content)).decode(),
    }


def _model(key, *, tier=1, provisional=False, display=None, repo=None, provider=None):
    return {
        "key": key,
        "display_filename": display,
        "repo_id": repo,
        "provider": provider,
        "quant_bits": 4.0,
        "battles": 30,
        "effective_battles": 30.0,
        "provisional": provisional,
        "quality": {"strength": 0.4, "ci_low": 0.1, "ci_high": 0.7, "tier": None if provisional else tier},
        "both_bad_rate": 0.0,
        "quality_warning": False,
        "efficiency": None,
        "efficiency_comparable": False,
    }


def _scope(models, *, ranked=True):
    return {
        "corpus": {"rows_used": 340, "effective_votes": 300.0, "client_count": 12},
        "models": models if ranked else [],
        "tiers": [],
        "efficiency_components": [],
        "provisional_models": [],
        "pairs": [],
        "ranked": ranked,
        "reasons": [] if ranked else ["only 40.0 effective votes, need 100"],
        "updated_at": "2026-10-16T04:00:00+00:00" if ranked else None,
    }


def _artifact(**overrides):
    overall_models = [
        _model("sha256:" + "a" * 64, display="qwen-7b-Q4_K_M.gguf", repo="org/qwen-gguf"),
        _model("sha256:" + "b" * 64, tier=2, display="gemma-9b-Q5_K_M.gguf", repo="org/gemma-gguf"),
        _model("filename:local-only.gguf", tier=2, display="local-only.gguf"),
        _model("sha256:" + "c" * 64, provisional=True, display="thin.gguf", repo="org/thin"),
    ]
    artifact = {
        "schema_version": 2,
        "generated_at": "2026-10-16T04:00:00+00:00",
        "status": "ranked",
        **_scope(overall_models),
        "groups": [
            {"key": "unified:16", "memory_kind": "unified", "memory_bucket": "16",
             **_scope(overall_models[:2])},
            {"key": "vram:8", "memory_kind": "vram", "memory_bucket": "8",
             **_scope([], ranked=False)},
        ],
    }
    artifact.update(overrides)
    return artifact


class _Response:
    def __init__(self, status: int, body: bytes = b""):
        self.status_code = status
        self.content = body
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        pass


@pytest.fixture
def served(monkeypatch):
    """Serve {url: (status, bytes)}; a missing url is a network error."""
    table: dict[str, tuple[int, bytes]] = {}

    def get(url, *args, **kwargs):
        if url not in table:
            raise requests.ConnectionError("offline")
        status, body = table[url]
        return _Response(status, body)

    monkeypatch.setattr(requests, "get", get)
    return table


def _publish(table, private, artifact: dict | bytes):
    raw = artifact if isinstance(artifact, bytes) else json.dumps(artifact).encode()
    table[config.ARENA_LEADERBOARD_URL] = (200, raw)
    table[config.ARENA_LEADERBOARD_MANIFEST_URL] = (200, json.dumps(_sign(private, raw)).encode())
    return raw


def test_the_urls_point_at_main_published():
    base = "https://raw.githubusercontent.com/omm-hippo/omm/main/published/"
    assert config.ARENA_LEADERBOARD_URL == base + "arena-leaderboard.json"
    assert config.ARENA_LEADERBOARD_MANIFEST_URL == base + "arena-leaderboard.manifest.json"


def test_a_verified_board_is_returned_and_cached(isolated_omm_home, served):
    private, public = _keypair()
    raw = _publish(served, private, _artifact())
    assert arena_board.fetch_board(public)["status"] == "ranked"
    assert (config.OMM_HOME / "arena" / "leaderboard.json").read_bytes() == raw


def test_a_tampered_artifact_raises_and_shows_nothing(isolated_omm_home, served):
    private, public = _keypair()
    raw = _publish(served, private, _artifact())
    served[config.ARENA_LEADERBOARD_URL] = (200, raw.replace(b"ranked", b"rankeD", 1))
    with pytest.raises(arena_board.BoardVerificationError):
        arena_board.fetch_board(public)
    assert not (config.OMM_HOME / "arena" / "leaderboard.json").exists()


def test_a_missing_manifest_is_a_verification_failure(isolated_omm_home, served):
    private, public = _keypair()
    _publish(served, private, _artifact())
    served[config.ARENA_LEADERBOARD_MANIFEST_URL] = (404, b"")
    with pytest.raises(arena_board.BoardVerificationError):
        arena_board.fetch_board(public)


def test_another_key_is_a_verification_failure(isolated_omm_home, served):
    private, _public = _keypair()
    _other, wrong = _keypair()
    _publish(served, private, _artifact())
    with pytest.raises(arena_board.BoardVerificationError):
        arena_board.fetch_board(wrong)


def test_a_404_means_unpublished(isolated_omm_home, served):
    served[config.ARENA_LEADERBOARD_URL] = (404, b"404: Not Found")
    _private, public = _keypair()
    assert arena_board.fetch_board(public) is None


def test_offline_uses_a_cache_that_still_verifies(isolated_omm_home, served):
    private, public = _keypair()
    _publish(served, private, _artifact())
    arena_board.fetch_board(public)
    served.clear()
    assert arena_board.fetch_board(public)["status"] == "ranked"


def test_a_tampered_cache_is_not_used_offline(isolated_omm_home, served):
    private, public = _keypair()
    _publish(served, private, _artifact())
    arena_board.fetch_board(public)
    cached = config.OMM_HOME / "arena" / "leaderboard.json"
    cached.write_bytes(cached.read_bytes().replace(b"ranked", b"rankeD", 1))
    served.clear()
    with pytest.raises(arena_board.BoardUnavailable):
        arena_board.fetch_board(public)


def test_offline_with_no_cache_is_unavailable(isolated_omm_home, served):
    _private, public = _keypair()
    with pytest.raises(arena_board.BoardUnavailable):
        arena_board.fetch_board(public)


def test_a_newer_schema_raises_too_new(isolated_omm_home, served):
    private, public = _keypair()
    _publish(served, private, _artifact(schema_version=3))
    with pytest.raises(arena_board.BoardTooNew):
        arena_board.fetch_board(public)


def test_an_oversized_board_is_refused(isolated_omm_home, served, monkeypatch):
    private, public = _keypair()
    _publish(served, private, _artifact())
    monkeypatch.setattr(arena_board, "MAX_BOARD_BYTES", 100)
    with pytest.raises(arena_board.BoardUnavailable):
        arena_board.fetch_board(public)


def test_a_ranked_machine_group_is_selected():
    selection = arena_board.select_scope(_artifact(), ("unified", "16"))
    assert selection.scope_key == "unified:16"
    assert selection.scope_label == "Mac · unified memory 16GB"
    assert selection.fell_back is False
    assert len(selection.scope["models"]) == 2


def test_unranked_group_falls_back_to_overall():
    selection = arena_board.select_scope(_artifact(), ("vram", "8"))
    assert selection.scope_key == "overall"
    assert selection.scope_label == "All machines"
    assert selection.fell_back is True
    assert selection.machine_label == "PC · GPU VRAM 8GB"
    assert selection.machine_scope["corpus"]["effective_votes"] == 300.0
    assert selection.machine_scope["ranked"] is False


def test_a_machine_with_no_group_is_not_a_fallback():
    selection = arena_board.select_scope(_artifact(), None)
    assert selection.scope_key == "overall"
    assert selection.fell_back is False
    assert selection.machine_scope is None


def test_a_group_absent_from_the_board_falls_back():
    selection = arena_board.select_scope(_artifact(), ("cpu", "16"))
    assert selection.scope_key == "overall" and selection.fell_back is True
    assert selection.machine_scope is None


def test_an_unknown_group_key_gets_its_raw_label():
    assert arena_board.group_label("vram:7") == "vram:7"
    assert arena_board.group_label("overall") == "All machines"


def test_numbers_run_across_tiers_and_skip_provisional():
    rows = arena_board.numbered_rows(_artifact(), {})
    assert [row.number for row in rows] == [1, 2, 3]
    assert [row.model["quality"]["tier"] for row in rows] == [1, 2, 2]


def test_installed_matches_by_sha_even_under_another_filename():
    rows = arena_board.numbered_rows(_artifact(), {"renamed.gguf": {"sha256": "A" * 64}})
    assert rows[0].installed_filename == "renamed.gguf"
    assert rows[0].ref == "renamed.gguf"


def test_installed_matches_by_filename_second():
    rows = arena_board.numbered_rows(_artifact(), {"Gemma-9B-Q5_K_M.gguf": {}})
    assert rows[1].installed_filename == "Gemma-9B-Q5_K_M.gguf"


def test_a_not_installed_model_gets_an_exact_install_ref():
    rows = arena_board.numbered_rows(_artifact(), {})
    assert rows[0].installed_filename is None
    assert rows[0].ref == "org/qwen-gguf:qwen-7b-Q4_K_M.gguf"


def test_a_modelscope_model_gets_its_provider_prefix():
    artifact = _artifact()
    artifact["models"][0]["provider"] = "modelscope"
    assert arena_board.numbered_rows(artifact, {})[0].ref == "ms:org/qwen-gguf:qwen-7b-Q4_K_M.gguf"


def test_a_model_without_repo_gets_an_unsourced_ref():
    rows = arena_board.numbered_rows(_artifact(), {})
    assert rows[2].ref == "arena-unsourced:local-only.gguf"
