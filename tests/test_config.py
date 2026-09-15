from __future__ import annotations

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
