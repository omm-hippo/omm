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


def test_parallel_first_reads_share_one_persisted_id(isolated_omm_home, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    start = threading.Barrier(8)
    original_write = config.atomic_write_text

    def slow_write(path, content):
        time.sleep(0.02)
        original_write(path, content)

    def read_id(_):
        start.wait(timeout=5)
        return config.client_id()

    monkeypatch.setattr(config, "atomic_write_text", slow_write)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(read_id, range(8)))

    assert len(set(ids)) == 1
    assert config.CLIENT_ID_PATH.read_text().strip() == ids[0]


def test_client_id_recovers_non_utf8_file(isolated_omm_home):
    config.CLIENT_ID_PATH.write_bytes(b"\xff\xfe")

    value = config.client_id()

    assert re.fullmatch(r"[0-9a-f]{32}", value)
    assert config.CLIENT_ID_PATH.read_text().strip() == value


def test_client_id_remains_best_effort_when_lock_is_busy(isolated_omm_home, monkeypatch):
    from filelock import Timeout

    def busy(*args, **kwargs):
        raise Timeout(str(config.CLIENT_ID_PATH))

    monkeypatch.setattr(config, "locked", busy)

    assert re.fullmatch(r"[0-9a-f]{32}", config.client_id())
    assert not config.CLIENT_ID_PATH.exists()
