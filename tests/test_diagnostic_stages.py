import json
from pathlib import Path

from omm import diagnostic_stages, support_report, usage


def write_run(home, name, command, outcome=None, events=()):
    root = home / "logs"
    root.mkdir(exist_ok=True)
    records = [{"event": "run_start", "argv": [command, "/private/secret/model.gguf"], "message": "token=TEST_SECRET"}]
    records.extend({"event": e, "file": "private-model.gguf", "url": "https://user:TEST_SECRET@example.test/"} for e in events)
    if outcome:
        records.append({"event": "run_end", "outcome": outcome})
    (root / name).write_text("\n".join(map(json.dumps, records)))


def test_local_stage_counts_explain_failure_and_later_success_without_identity(isolated_omm_home):
    write_run(isolated_omm_home, "1.jsonl", "install", "failed", ["download-start", "download-failed"])
    write_run(isolated_omm_home, "2.jsonl", "install", "ok", ["download-start", "download-complete", "link"])
    report = diagnostic_stages.collect()
    stages = {r["stage"]: r for r in report["stages"]}
    assert stages["install"]["failed"] == stages["install"]["succeeded"] == 1
    assert stages["download"]["later_success_after_failure"] == 1
    assert stages["link"]["succeeded"] == 1
    text = json.dumps(report)
    assert all(x not in text for x in ("TEST_SECRET", "private-model", "/private/secret", "example.test"))


def test_missing_finish_is_incomplete_and_unknown_commands_are_not_echoed(isolated_omm_home):
    write_run(isolated_omm_home, "1.jsonl", "setup")
    write_run(isolated_omm_home, "2.jsonl", "PRIVATE_SECRET", "failed")
    report = diagnostic_stages.collect()
    assert report["stages"][0]["incomplete"] == 1
    assert "PRIVATE_SECRET" not in json.dumps(report)


def test_symlinked_logs_are_not_read(isolated_omm_home, tmp_path):
    root = isolated_omm_home / "logs"
    root.mkdir()
    secret = tmp_path / "private.jsonl"
    secret.write_text("not a log")
    (root / "1.jsonl").symlink_to(secret)
    report = diagnostic_stages.collect()
    assert report["files_scanned"] == 0
    assert report["files_skipped_or_truncated"] == 1


def test_usage_payload_does_not_copy_arbitrary_pending_args(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(usage, "_snapshot", lambda **k: {})
    payload = usage.build_payload([{"c": "/private/TEST_SECRET", "o": "https://TEST_SECRET", "e": "TEST_SECRET /private/path"}])
    assert "TEST_SECRET" not in json.dumps(payload)
    assert payload["commands"] == {"unknown unknown": 1}
    assert usage.build_payload([{"c": [], "o": {}}])["commands"] == {"unknown unknown": 1}
    assert usage.build_payload([{"c": "install", "o": "failed", "e": "hf_PRIVATE_TOKEN"}])["errors"] == {"install OtherError": 1}


def test_usage_opt_out_and_wire_payload_with_real_local_receiver(isolated_omm_home, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from omm import config, telemetry

    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(json.loads(envelope["event_json"]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(config, "USAGE_GATEWAY_ENDPOINT", f"http://127.0.0.1:{server.server_port}/usage")
    monkeypatch.setattr(telemetry, "_solve_proof_of_work", lambda payload: (1, 0))
    monkeypatch.setattr(usage, "_snapshot", lambda **kwargs: {"schema_version": 1, "client_id": "test-local-device"})
    try:
        usage._pending_path().write_text(json.dumps([{"c": "install", "o": "failed", "e": "RuntimeError", "message": "TEST_SECRET /private/model.gguf"}]))
        config.update_config(usage_stats_policy="never")
        assert not usage.flush_pending(force=True)
        assert received == []
        preview = usage.build_payload()
        config.update_config(usage_stats_policy="enabled")
        assert usage.flush_pending(force=True)
        assert received == [preview]
        assert "TEST_SECRET" not in json.dumps(received)
        assert usage.pending_count() == 0
        config.update_config(usage_stats_policy="never")
        assert not usage.flush_pending(force=True)
        assert len(received) == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_optional_policy_group_does_not_echo_invalid_config_values(isolated_omm_home, monkeypatch):
    from types import SimpleNamespace
    support_report.config.CONFIG_PATH.write_text(json.dumps({"telemetry_send_policy": "TEST_SECRET", "error_report_send_policy": "TEST_SECRET"}))
    report = support_report.build(SimpleNamespace(status="PASS", checks=[]), include=["policies", "stages"])
    assert "TEST_SECRET" not in support_report.preview(report)
    assert "stage_diagnostics" in report
