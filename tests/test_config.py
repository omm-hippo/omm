from __future__ import annotations

import json
from pathlib import Path

from omm import config


def test_load_config_removes_a_leftover_legacy_firebase_auth_cache(isolated_omm_home):
    cache = isolated_omm_home / "firebase_auth.json"
    cache.write_text("{}", encoding="utf-8")

    config.load_config()

    assert not cache.exists()


def test_load_config_is_fine_when_no_legacy_cache_exists(isolated_omm_home):
    cache = isolated_omm_home / "firebase_auth.json"
    assert not cache.exists()

    config.load_config()  # must not raise


def test_load_config_tolerates_a_cache_it_cannot_delete(isolated_omm_home, monkeypatch):
    cache = isolated_omm_home / "firebase_auth.json"
    cache.write_text("{}", encoding="utf-8")

    real_unlink = Path.unlink

    def denying_unlink(self, *args, **kwargs):
        if self == cache:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denying_unlink)

    loaded = config.load_config()  # must not raise

    assert isinstance(loaded, dict)


def test_arena_vote_policy_defaults_to_ask(isolated_omm_home):
    assert config.DEFAULT_CONFIG["arena_vote_send_policy"] == "ask"
    assert config.load_config()["arena_vote_send_policy"] == "ask"


def test_arena_vote_policy_rejects_an_unknown_value(isolated_omm_home):
    """Same coercion telemetry_send_policy gets: an unreadable policy must
    fall back to the safe default, never be treated as consent."""
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text(
        json.dumps({"arena_vote_send_policy": "yes-please"}), encoding="utf-8"
    )
    assert config.load_config()["arena_vote_send_policy"] == "ask"


def test_votes_gateway_endpoint_is_the_shared_worker():
    assert config.VOTES_GATEWAY_ENDPOINT.endswith("/votes")
    assert config.VOTES_GATEWAY_ENDPOINT.startswith("https://")
    # Same Worker host as every other channel - a second host would need its
    # own PoW/rate-limit deployment.
    assert (
        config.VOTES_GATEWAY_ENDPOINT.rsplit("/", 1)[0]
        == config.USAGE_GATEWAY_ENDPOINT.rsplit("/", 1)[0]
    )
