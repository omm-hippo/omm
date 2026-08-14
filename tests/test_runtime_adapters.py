from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from omm.engines import (
    LoadOptions,
    ProbeRequest,
    RuntimeAdapterError,
    RuntimeModelRef,
)
from omm.engines.lmstudio import LMStudioAdapter
from omm.engines.ollama import OllamaAdapter


class _RuntimeHandler(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, format, *args):
        return

    def _json(self, status, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _payload(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        self.state.setdefault("calls", []).append(("GET", self.path, None))
        if self.state.get("old_api") and self.path == "/api/v1/models":
            self._json(404, {"error": "not found"})
            return
        if self.path == "/api/version":
            self._json(200, {"version": "0.12.6"})
            return
        if self.path == "/api/tags":
            self._json(200, {"models": [{"name": "local-model:latest"}]})
            return
        if self.path == "/api/ps":
            rows = [{"name": "local-model:latest"}] if self.state.get("loaded") else []
            self._json(200, {"models": rows})
            return
        if self.path == "/api/v1/models":
            instances = [{"id": "local/model"}] if self.state.get("loaded") else []
            self._json(
                200,
                {
                    "models": [
                        {
                            "type": "llm",
                            "key": "local/model",
                            "display_name": "Local Model",
                            "loaded_instances": instances,
                            "variants": ["local/model@q4_k_m"],
                        }
                    ]
                },
                {"X-LM-Studio-Version": "0.4.1"},
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        payload = self._payload()
        self.state.setdefault("calls", []).append(("POST", self.path, payload))
        self.state["authorization"] = self.headers.get("Authorization")
        if self.state.get("unauthorized"):
            self._json(401, {"error": "secret must not leak"})
            return
        if self.path == "/api/generate":
            if self.state.get("oom"):
                self._json(500, {"error": "out of memory"})
                return
            if payload.get("keep_alive") == 0:
                if not self.state.get("unload_fails"):
                    self.state["loaded"] = False
                self._json(200, {"response": ""})
                return
            self.state["loaded"] = True
            response = "" if not payload.get("prompt") or self.state.get("empty") else "OK"
            if self.state.get("reasoning_model") and payload.get("think") is not False:
                self._json(200, {"response": "", "thinking": "still reasoning"})
            else:
                self._json(200, {"response": response})
            return
        if self.path == "/api/v1/models/load":
            if self.state.get("oom"):
                self._json(500, {"error": "insufficient memory"})
                return
            self.state["loaded"] = True
            self._json(200, {"status": "loaded", "instance_id": "local/model"})
            return
        if self.path == "/api/v1/chat":
            content = "" if self.state.get("empty") else "OK"
            self._json(200, {"output": [{"type": "message", "content": content}]})
            return
        if self.path == "/api/v1/models/unload":
            if self.state.get("unload_fails"):
                self._json(500, {"error": "busy"})
                return
            self.state["loaded"] = False
            self._json(200, {"instance_id": payload.get("instance_id")})
            return
        self._json(404, {"error": "not found"})


@pytest.fixture
def runtime_server():
    state = {"loaded": False, "calls": []}
    handler = type("RuntimeHandler", (_RuntimeHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("factory", "reference", "version"),
    [
        (OllamaAdapter, RuntimeModelRef("local-model"), "0.12.6"),
        (LMStudioAdapter, RuntimeModelRef("local/model"), "0.4.1"),
    ],
)
def test_adapter_contract_loads_generates_and_releases_omm_load(
    runtime_server, factory, reference, version
):
    base_url, state = runtime_server
    adapter = factory(base_url)

    assert adapter.health().version == version
    receipt = adapter.load(reference, LoadOptions(context_length=512))
    assert receipt.loaded_by_omm is True
    assert receipt.was_already_loaded is False
    assert adapter.generate(receipt, ProbeRequest()).text == "OK"
    assert adapter.unload(receipt).unloaded is True
    assert state["loaded"] is False


@pytest.mark.parametrize(
    ("factory", "reference", "unload_path"),
    [
        (OllamaAdapter, RuntimeModelRef("local-model"), "/api/generate"),
        (LMStudioAdapter, RuntimeModelRef("local/model"), "/api/v1/models/unload"),
    ],
)
def test_adapter_contract_preserves_preloaded_model(
    runtime_server, factory, reference, unload_path
):
    base_url, state = runtime_server
    state["loaded"] = True
    adapter = factory(base_url)

    receipt = adapter.load(reference, LoadOptions())
    assert receipt.was_already_loaded is True
    assert receipt.loaded_by_omm is False
    assert adapter.unload(receipt).unloaded is True
    assert state["loaded"] is True
    assert not any(method == "POST" and path == unload_path for method, path, _ in state["calls"])


def test_ollama_probe_disables_thinking_for_bounded_visible_answer(runtime_server):
    base_url, state = runtime_server
    state["reasoning_model"] = True
    adapter = OllamaAdapter(base_url)
    receipt = adapter.load(RuntimeModelRef("local-model"), LoadOptions())

    assert adapter.generate(receipt, ProbeRequest()).text == "OK"
    probe_payload = next(
        payload
        for method, path, payload in reversed(state["calls"])
        if method == "POST" and path == "/api/generate" and payload.get("prompt")
    )
    assert probe_payload["think"] is False


@pytest.mark.parametrize(
    ("factory", "reference"),
    [
        (OllamaAdapter, RuntimeModelRef("local-model")),
        (LMStudioAdapter, RuntimeModelRef("local/model")),
    ],
)
def test_adapter_rejects_empty_generation(runtime_server, factory, reference):
    base_url, state = runtime_server
    adapter = factory(base_url)
    receipt = adapter.load(reference, LoadOptions())
    state["empty"] = True

    with pytest.raises(RuntimeAdapterError) as error:
        adapter.generate(receipt, ProbeRequest())

    assert error.value.reason == "empty_response"


@pytest.mark.parametrize(
    ("factory", "reference"),
    [
        (OllamaAdapter, RuntimeModelRef("local-model")),
        (LMStudioAdapter, RuntimeModelRef("local/model")),
    ],
)
def test_adapter_classifies_out_of_memory(runtime_server, factory, reference):
    base_url, state = runtime_server
    state["oom"] = True

    with pytest.raises(RuntimeAdapterError) as error:
        factory(base_url).load(reference, LoadOptions())

    assert error.value.reason == "out_of_memory"


def test_lmstudio_token_comes_from_memory_and_never_appears_in_error(runtime_server):
    base_url, state = runtime_server
    state["unauthorized"] = True
    secret = "top-secret-token"
    adapter = LMStudioAdapter(base_url, api_token=secret)

    with pytest.raises(RuntimeAdapterError) as error:
        adapter.load(RuntimeModelRef("local/model"), LoadOptions())

    assert state["authorization"] == f"Bearer {secret}"
    assert secret not in str(error.value)
    assert secret not in repr(error.value)


def test_lmstudio_old_api_is_reported_as_unsupported(runtime_server):
    base_url, state = runtime_server
    state["old_api"] = True

    health = LMStudioAdapter(base_url).health()

    assert health.reachable is False
    assert health.failure_reason == "unsupported_runtime"


@pytest.mark.parametrize("factory", [OllamaAdapter, LMStudioAdapter])
def test_adapter_rejects_non_loopback_runtime(factory):
    with pytest.raises(ValueError, match="loopback"):
        factory("https://example.com")


def test_generation_timeout_is_bounded_and_classified(monkeypatch):
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ReadTimeout("slow")),
    )
    adapter = OllamaAdapter()
    receipt = type(
        "Receipt",
        (),
        {"model": type("Model", (), {"key": "model"})()},
    )()

    with pytest.raises(RuntimeAdapterError) as error:
        adapter.generate(receipt, ProbeRequest(timeout_seconds=1))

    assert error.value.reason == "generation_timeout"


def test_loopback_client_ignores_environment_proxies(runtime_server, monkeypatch):
    base_url, _ = runtime_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    assert OllamaAdapter(base_url).health().reachable is True


@pytest.mark.parametrize(
    ("factory", "reference", "load_path"),
    [
        (OllamaAdapter, RuntimeModelRef("local-model"), "/api/generate"),
        (LMStudioAdapter, RuntimeModelRef("local/model"), "/api/v1/models/load"),
    ],
)
def test_uncertain_failed_load_cleans_up_if_model_became_resident(
    runtime_server, factory, reference, load_path, monkeypatch
):
    base_url, state = runtime_server
    adapter = factory(base_url)
    original = adapter._client.request
    failed_once = False

    def fail_after_runtime_loaded(method, path, **kwargs):
        nonlocal failed_once
        if method == "POST" and path == load_path and not failed_once:
            failed_once = True
            state["loaded"] = True
            raise RuntimeAdapterError("load_failed", "simulated lost load response")
        return original(method, path, **kwargs)

    monkeypatch.setattr(adapter._client, "request", fail_after_runtime_loaded)

    with pytest.raises(RuntimeAdapterError) as error:
        adapter.load(reference, LoadOptions())

    assert error.value.reason == "load_failed"
    assert state["loaded"] is False


@pytest.mark.parametrize(
    ("factory", "reference", "load_path"),
    [
        (OllamaAdapter, RuntimeModelRef("local-model"), "/api/generate"),
        (LMStudioAdapter, RuntimeModelRef("local/model"), "/api/v1/models/load"),
    ],
)
def test_uncertain_failed_load_reports_cleanup_failure(
    runtime_server, factory, reference, load_path, monkeypatch
):
    base_url, state = runtime_server
    state["unload_fails"] = True
    adapter = factory(base_url)
    original = adapter._client.request
    failed_once = False

    def fail_after_runtime_loaded(method, path, **kwargs):
        nonlocal failed_once
        if method == "POST" and path == load_path and not failed_once:
            failed_once = True
            state["loaded"] = True
            raise RuntimeAdapterError("load_failed", "simulated lost load response")
        return original(method, path, **kwargs)

    monkeypatch.setattr(adapter._client, "request", fail_after_runtime_loaded)

    with pytest.raises(RuntimeAdapterError) as error:
        adapter.load(reference, LoadOptions())

    assert error.value.reason == "unload_failed"
