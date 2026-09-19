#!/usr/bin/env python3
"""Opt-in contribute smoke test with a real private Ollama and local collector.

An existing <=128 MiB GGUF and an independently checked digest are required.
The catalog/provider metadata are fixtures, transfers use a local HTTP server,
and both parent and spawned evaluator are restricted to the private ports.
No public telemetry upload, GUI interaction, or default model store is used.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


def _route_private_endpoints():
    base = os.environ.get("OMM_VERIFY_CONTRIBUTE_HOST")
    if not base:
        return
    import requests
    from omm import benchmark, quality
    benchmark.OLLAMA_HOST = quality.OLLAMA_HOST = base
    ports = {int(value) for value in os.environ["OMM_VERIFY_CONTRIBUTE_PORTS"].split(",")}
    original = requests.sessions.Session.request
    def local_request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port not in ports:
            raise RuntimeError("Verification refused an endpoint outside its private servers")
        return original(self, method, url, **kwargs)
    requests.sessions.Session.request = local_request


# multiprocessing's spawn imports this module again before evaluating a model.
_route_private_endpoints()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    binary = shutil.which("ollama")
    if binary is None or args.model.stat().st_size > 128 * 1024**2:
        raise SystemExit("An existing Ollama binary and a GGUF <=128 MiB are required.")
    model_bytes = args.model.read_bytes()
    digest = hashlib.sha256(model_bytes).hexdigest()
    if digest != args.sha256:
        raise SystemExit("The model checksum did not match; nothing was started.")
    root = Path(tempfile.mkdtemp(prefix="omm-contribute-live-"))
    hub = root / "omm-home"
    models = hub / "models"
    models.mkdir(parents=True)
    kept = models / "kept.gguf"
    kept.write_bytes(b"pre-existing user file fixture")
    events = []
    downloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            downloads.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(model_bytes)))
            self.end_headers()
            self.wfile.write(model_bytes)

        def do_POST(self):
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            events.append(event)
            (root / "received.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    local = f"http://127.0.0.1:{http.server_port}"
    os.environ.update(OMM_HOME=str(hub), OLLAMA_MODELS=str(root / "ollama-models"),
                      OLLAMA_HOST=base, OMM_VERIFY_CONTRIBUTE_HOST=base,
                      OMM_VERIFY_CONTRIBUTE_PORTS=f"{port},{http.server_port}",
                      OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="1")
    _route_private_endpoints()
    import requests
    from omm import cli, config, downloader, gguf, linker, predictor, registry
    from omm.engines.ollama import OllamaAdapter
    config.update_config(onboarding_completed=True, external_scan_done=True,
                         telemetry_endpoint=local + "/events", telemetry_backend="self_hosted",
                         telemetry_send_policy="always", contribute_always_ack=True,
                         usage_stats_policy="never", error_report_send_policy="never")
    adapter = OllamaAdapter(base)
    cli._compatibility_adapter = lambda engine: adapter
    cli._maybe_start_update_check = lambda ctx: None
    filenames = [kept.name, "kept-native-Q4_0.gguf", "smoke-15M-Q4_0.gguf", "second-15M-Q4_0.gguf"]
    candidates = [{"repo_id": "verification/tiny", "filename": name, "provider": "huggingface",
                   "size_bytes": len(model_bytes), "parameter_count_b": 0.015, "quant_bits": 4} for name in filenames]
    artifact = {"trees": [{"leaf": True, "value": 10}], "candidates": candidates}
    cli._load_recommendation_with_change_note = lambda cfg: (artifact, False)
    predictor.load_cached_model = lambda: artifact
    cli.remote_file_size = lambda *args: len(model_bytes)
    cli.remote_file_sha256 = lambda *args: digest
    cli.remote_gguf_metadata = lambda provider, repo, filename, keys: gguf.read_gguf_metadata(args.model, keys)
    cli.download_url = lambda provider, repo, filename: "https://fixture.invalid/" + filename
    def local_model_get(url, **kwargs):
        if urlsplit(url).hostname != "fixture.invalid":
            raise RuntimeError("Unexpected model provider in the smoke test")
        return requests.get(local + urlsplit(url).path, **kwargs)
    downloader._https_get = local_model_get
    report = {"root": str(root), "model_sha256": digest,
              "verification": "Real Ollama and evaluator subprocess; fixture catalog/provider metadata; local HTTP model source and persisted collector"}
    with (root / "server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([binary, "serve"], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Private Ollama could not start")
                if adapter.health().reachable:
                    break
                time.sleep(0.25)
            native_tag = linker.sanitize_ollama_tag(filenames[1])
            modelfile = root / "Modelfile"
            modelfile.write_text("FROM " + json.dumps(str(args.model)) + "\n", encoding="utf-8")
            subprocess.run([binary, "create", native_tag, "-f", str(modelfile)], check=True,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
            output, errors = io.StringIO(), io.StringIO()
            sys.argv = ["omm", "contribute", "--yes", "--max-models", "1", "--max-minutes", "2",
                        "--max-download-gb", "0.125"]
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                try:
                    cli.main()
                except SystemExit as error:
                    if error.code:
                        raise RuntimeError(f"Contribute exited {error.code}: {errors.getvalue()}") from error
            (root / "stdout.txt").write_text(output.getvalue(), encoding="utf-8")
            (root / "stderr.txt").write_text(errors.getvalue(), encoding="utf-8")
            assert kept.read_bytes() == b"pre-existing user file fixture"
            assert downloads == ["/" + filenames[2]], downloads
            assert events and events[0].get("tokens_per_sec", 0) > 0, events
            assert not any(key in events[0] for key in ("prompt", "response", "text"))
            assert registry.load_registry() == {}
            assert sorted(p.name for p in models.glob("*.gguf")) == [kept.name]
            native = adapter.list_models()
            assert len(native) == 1 and native[0].key.removesuffix(":latest") == native_tag
            assert not native[0].loaded
            assert "your model-count limit was reached" in output.getvalue()
            report.update(status="passed", downloaded_files=downloads, accepted_events=len(events),
                          prior_hub_file_preserved=True, prior_native_model_preserved=True,
                          temporary_model_removed=True, evaluator_engine_version=events[0].get("engine_version"))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
            http.shutdown()
            http.server_close()
            http_thread.join(timeout=2)
            report["owned_servers_stopped"] = server.poll() is not None and not http_thread.is_alive()
            (root / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "verification.json"), "status": report.get("status")}))


if __name__ == "__main__":
    main()
