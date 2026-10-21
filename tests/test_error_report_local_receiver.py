"""End-to-end consent checks against a real local HTTP receiver (#428).

A throwaway server on 127.0.0.1 (ephemeral port) records every request body,
so these tests observe what actually crosses the wire rather than what a
mocked `requests.post` was called with. Nothing here reaches a real server.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omm import config, error_report

SECRET_MESSAGE = (
    "GET https://alice:pa55word@huggingface.co/api/models/x?token=qwerty123&access_token=zxcv987 "
    "failed; headers={'Authorization': 'Bearer hf_AbCdEfGhIjKlMnOpQrStUv'} "
    r"while reading C:\Users\Alice\Documents\secret-project\model.gguf and "
    "/home/alice/.cache/huggingface/token (hf_ZyXwVuTsRqPoNmLkJiHg)"
)
SECRETS = [
    "alice", "Alice", "pa55word", "qwerty123", "zxcv987", "hf_AbCdEfGhIjKlMnOpQrStUv",
    "hf_ZyXwVuTsRqPoNmLkJiHg", r"C:\Users", "/home/",
]
# Directories *below* a home directory are kept after the `~` (existing,
# documented scrub_paths behavior), so a folder name there is not asserted
# away here; PRIVACY.md says so.
FREE_TEXT = "please summarize my private diary entry about project nightingale"


class _Receiver:
    def __init__(self):
        self.bodies: list[bytes] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib hook name
                length = int(self.headers.get("Content-Length") or 0)
                receiver.bodies.append(self.rfile.read(length))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def payloads(self) -> list[dict]:
        return [json.loads(body) for body in self.bodies]


@pytest.fixture
def receiver():
    server = _Receiver()
    server.thread.start()
    try:
        yield server
    finally:
        server.server.shutdown()
        server.server.server_close()


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    error_report.set_run_consent(None)
    monkeypatch.setattr("omm.hardware._last_scan", None, raising=False)
    yield
    error_report.set_run_consent(None)


def _configure(receiver: _Receiver, **changes):
    config.save_config({
        "telemetry_endpoint": f"http://127.0.0.1:{receiver.port}/telemetry",
        "telemetry_backend": "self_hosted",
        **changes,
    })
    assert error_report.endpoint() == f"http://127.0.0.1:{receiver.port}/error_reports"


def _crash() -> RuntimeError:
    return RuntimeError(SECRET_MESSAGE)


def test_nothing_is_sent_or_queued_before_consent(receiver, monkeypatch):
    _configure(receiver)  # policy never chosen -> behaves as never
    monkeypatch.setenv("OMM_TEST_SECRET", "env-secret-value")
    assert error_report.queue_report(_crash(), trigger="crash", subcommand="search") is None
    assert error_report.pending_count() == 0
    assert error_report.flush_pending() == 0
    assert error_report.send_report(error_report.build_report(_crash(), trigger="crash")) is False
    assert receiver.bodies == []


def test_explicit_never_stays_never_even_with_run_consent(receiver):
    _configure(receiver, error_report_send_policy="never")
    error_report.set_run_consent(True)
    assert error_report.queue_report(_crash(), trigger="crash") is None
    assert error_report.flush_pending(force=True) == 0
    assert error_report.send_report(error_report.build_report(_crash(), trigger="crash"), force=True) is False
    assert receiver.bodies == []


def test_ask_policy_waits_for_an_answer(receiver):
    _configure(receiver, error_report_send_policy="ask")
    assert error_report.queue_report(_crash(), trigger="crash") is not None
    assert error_report.flush_pending() == 0  # no answer yet
    error_report.set_run_consent(False)
    assert error_report.queue_report(_crash(), trigger="crash") is None  # declined this run
    assert receiver.bodies == []


def test_sent_payload_equals_preview_and_carries_no_secrets_or_free_text(receiver, monkeypatch):
    _configure(receiver, error_report_send_policy="always")
    monkeypatch.setattr(sys, "argv", ["omm", "search", FREE_TEXT, "--token", "hf_ArgvTokenValue1234"])
    monkeypatch.setenv("HF_TOKEN", "hf_EnvTokenValue12345")
    queued = error_report.queue_report(_crash(), trigger="crash", subcommand="search")
    assert queued is not None

    preview, is_example = error_report.preview_report()
    assert is_example is False
    previewed_text = error_report.preview_text(preview)

    assert error_report.flush_pending() == 1
    [sent] = receiver.payloads()
    assert sent == json.loads(previewed_text) == queued

    wire = receiver.bodies[0].decode("utf-8")
    for secret in SECRETS + [FREE_TEXT, "nightingale", "hf_ArgvTokenValue1234", "hf_EnvTokenValue12345"]:
        assert secret not in wire, secret
    assert "<redacted>" in sent["error_message"]
    assert set(sent) <= {
        "schema_version", "error_type", "error_message", "trigger", "recorded_at", "os_name",
        "os_version", "client_version", "subcommand", "catalog_ref", "engine", "cpu_score",
        "cpu_tier", "gpu_score", "gpu_tier", "cpu_arch",
    }
    assert error_report.pending_count() == 0
