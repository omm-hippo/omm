import json
import time

from omm import config, usage


def _enable(monkeypatch):
    monkeypatch.setattr(
        config, "load_config", lambda: {"usage_stats_policy": "enabled"}
    )


def test_record_run_noop_when_unset(isolated_omm_home):
    usage.record_run("install", "ok", None)
    assert usage.pending_count() == 0


def test_record_run_appends_when_enabled(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    usage.record_run("install", "failed", "DownloadError")
    assert usage.pending_count() == 2


def test_build_payload_shape(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    usage.record_run("install", "failed", "DownloadError")
    usage.record_run("search", "ok", None)
    p = usage.build_payload()
    for key in (
        "schema_version", "client_id", "client_version", "install_source",
        "os_name", "cpu_arch", "ram_gb_bucket", "vram_gb_bucket",
        "gpu_vendor", "recorded_at", "update_channel",
    ):
        assert key in p, key
    assert p["schema_version"] == 1
    assert isinstance(p["ram_gb_bucket"], str)
    assert p["commands"]["install ok"] == 1
    assert p["commands"]["install failed"] == 1
    assert p["commands"]["search ok"] == 1
    assert p["errors"]["install DownloadError"] == 1


def test_error_class_never_leaks_message(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "failed", type(RuntimeError("secret /home/x")).__name__)
    text = json.dumps(usage.build_payload())
    assert "secret" not in text and "/home/x" not in text


def test_tally_capped(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    for i in range(250):
        usage.record_run(f"cmd{i}", "ok", None)
    p = usage.build_payload()
    assert len(p["commands"]) <= usage._TALLY_MAX_KEYS


def test_discard_pending(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    assert usage.discard_pending() == 1
    assert usage.pending_count() == 0
