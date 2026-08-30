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


def test_flush_noop_before_interval(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    (config.OMM_HOME).mkdir(parents=True, exist_ok=True)
    (config.OMM_HOME / "usage-state.json").write_text(
        json.dumps({"last_sent": time.time()})
    )
    usage.record_run("install", "ok", None)
    calls = []
    monkeypatch.setattr(usage, "_post", lambda p: calls.append(p) or True)
    assert usage.flush_pending() is False
    assert calls == []


def test_flush_sends_and_clears_on_2xx(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    sent = []
    monkeypatch.setattr(usage, "_post", lambda p: sent.append(p) or True)
    assert usage.flush_pending(force=True) is True
    assert sent and sent[0]["commands"]["install ok"] == 1
    assert usage.pending_count() == 0


def test_flush_keeps_pending_on_failure(isolated_omm_home, monkeypatch):
    _enable(monkeypatch)
    usage.record_run("install", "ok", None)
    monkeypatch.setattr(usage, "_post", lambda p: False)
    assert usage.flush_pending(force=True) is False
    assert usage.pending_count() == 1


def test_flush_noop_when_policy_unset(isolated_omm_home, monkeypatch):
    # opted out: even with pending rows written under an earlier consent
    monkeypatch.setattr(config, "load_config", lambda: {"usage_stats_policy": "enabled"})
    usage.record_run("install", "ok", None)
    monkeypatch.setattr(config, "load_config", lambda: {})
    sent = []
    monkeypatch.setattr(usage, "_post", lambda p: sent.append(p) or True)
    assert usage.flush_pending(force=True) is False
    assert sent == []


def test_post_to_refuses_non_gateway_endpoint(isolated_omm_home):
    assert usage._post_to("https://evil.example/usage", {"schema_version": 1}) is False


def test_cli_main_records_usage_run(isolated_omm_home, monkeypatch):
    from omm import cli

    monkeypatch.setattr(
        config, "load_config",
        lambda: {"usage_stats_policy": "enabled", "theme": "dark"},
    )
    monkeypatch.setattr("sys.argv", ["omm", "--version"])
    try:
        cli.main()
    except SystemExit:
        pass
    rows = usage._read_pending()
    assert rows and rows[0]["o"] == "ok"
