from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import requests
from typer.testing import CliRunner

from omm import cli, config, network_policy, predictor, rules, telemetry


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"local-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def test_offline_blocks_reserved_external_address_before_dns_or_connect():
    network_policy.set_mode("offline")
    try:
        requests.get("https://198.51.100.17/model.gguf", timeout=0.1)
    except requests.ConnectionError as error:
        assert "offline" in str(error)
        assert "models-only" in str(error)
    else:
        raise AssertionError("external request was not blocked")


def test_offline_keeps_loopback_http_available():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        network_policy.set_mode("offline")
        response = requests.get(f"http://127.0.0.1:{server.server_port}/", timeout=2)
        assert response.text == "local-ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_models_only_allows_model_hosts_but_not_collectors():
    network_policy.set_mode("models-only")
    assert network_policy.request_allowed("https://huggingface.co/api/models/x")
    assert network_policy.request_allowed("https://raw.githubusercontent.com/omm-hippo/omm/main/a")
    assert not network_policy.request_allowed(
        "https://omm-telemetry-gateway.seong381400.workers.dev/usage"
    )
    with network_policy.model_transfer():
        assert network_policy.request_allowed("https://downloads.example/model.gguf")


def test_network_setting_and_one_shot_offline_do_not_confuse_persistence(isolated_omm_home):
    runner = CliRunner()
    changed = runner.invoke(cli.app, ["setting", "network", "--mode", "models-only", "--json"])
    assert changed.exit_code == 0, changed.output
    assert json.loads(changed.stdout)["mode"] == "models-only"
    assert config.load_config()["network_mode"] == "models-only"

    one_shot = runner.invoke(cli.app, ["--offline", "setting", "network", "--json"])
    assert one_shot.exit_code == 0, one_shot.output
    # The setting command reports the saved value; --offline controls only
    # this process and does not silently rewrite config.json.
    assert json.loads(one_shot.stdout)["mode"] == "models-only"
    assert json.loads(one_shot.stdout)["effective_mode"] == "offline"
    assert config.load_config()["network_mode"] == "models-only"


def test_offline_flag_after_command_is_active_before_root_prelude(isolated_omm_home):
    config.update_config(network_mode="online")
    result = CliRunner().invoke(cli.app, ["list", "--offline", "--json"])
    assert result.exit_code == 0, result.output
    assert network_policy.current_mode() == "offline"
    assert config.load_config()["network_mode"] == "online"


def test_models_only_does_not_send_or_queue_telemetry(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    network_policy.set_mode("models-only")
    monkeypatch.setattr(
        telemetry,
        "_post_event",
        lambda event: (_ for _ in ()).throw(AssertionError("must not post")),
    )
    assert telemetry.send_event({"benchmark_version": 1}) is False
    assert telemetry.last_send_status().outcome == "skipped_network_mode"
    assert telemetry.flush_pending() == 0
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    assert cli._resolve_upload_decision("send?") is False


def test_offline_rejects_unsigned_legacy_catalog_and_hosted_rules_cache(
    isolated_omm_home, monkeypatch
):
    artifact = {
        "model_version": 1,
        "feature_order": list(predictor.FEATURE_ORDER),
        "trees": [{"value": 1.0}],
        "candidates": [],
    }
    config.RECOMMEND_MODEL_PATH.write_text(json.dumps(artifact), encoding="utf-8")
    config.RULES_PATH.write_text(
        json.dumps([{"name": "remote", "min_ram_gb": 0, "min_vram_gb": 0}]),
        encoding="utf-8",
    )
    network_policy.set_mode("offline")
    assert predictor.load_cached_model() is None
    assert rules.load_rules() == rules.DEFAULT_RULES
