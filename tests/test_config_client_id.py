import re

from omm import config


def test_client_id_stable_and_hex(isolated_omm_home):
    a = config.client_id()
    b = config.client_id()
    assert a == b
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    assert config.CLIENT_ID_PATH.exists()


def test_client_id_regenerates_after_delete(isolated_omm_home):
    a = config.client_id()
    config.CLIENT_ID_PATH.unlink()
    b = config.client_id()
    assert a != b


def test_client_id_ephemeral_when_unwritable(isolated_omm_home, monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(config, "atomic_write_text", boom)
    config.CLIENT_ID_PATH.unlink(missing_ok=True)
    got = config.client_id()  # must not raise
    assert re.fullmatch(r"[0-9a-f]{32}", got)


def test_usage_policy_default_is_none(isolated_omm_home):
    assert config.load_config().get("usage_stats_policy") is None
