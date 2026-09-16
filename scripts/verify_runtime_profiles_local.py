#!/usr/bin/env python3
"""Opt-in real Ollama profile smoke test in a private model store and port.

Provide an existing small GGUF and its independently obtained SHA-256. This
does not download weights, use the user's Ollama model store, or open a GUI.
Only endpoint routing and child-output capture are overridden; loads, generated
responses, preset persistence, native CLI chat, and cleanup use real services.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    binary = shutil.which("ollama")
    if binary is None:
        raise SystemExit("Ollama CLI is required; nothing was installed.")
    if args.model.stat().st_size > 128 * 1024 * 1024:
        raise SystemExit("Use a small GGUF of at most 128 MiB for this smoke test.")
    actual = hashlib.sha256(args.model.read_bytes()).hexdigest()
    if actual != args.sha256:
        raise SystemExit("Model checksum mismatch; no runtime was started.")
    root = Path(tempfile.mkdtemp(prefix="omm-profile-live-"))
    hub = root / "omm-home"
    models = hub / "models"
    models.mkdir(parents=True)
    model = models / "model.gguf"
    shutil.copyfile(args.model, model)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "OMM_HOME": str(hub), "OLLAMA_MODELS": str(root / "ollama-models"),
           "OLLAMA_HOST": base_url, "OLLAMA_NUM_PARALLEL": "1",
           "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_KEEP_ALIVE": "30s"}
    # Import OMM only after configuring its task-owned data locations.
    os.environ.update({key: env[key] for key in ("OMM_HOME", "OLLAMA_MODELS", "OLLAMA_HOST")})
    from omm import benchmark, cli, config, registry, runtime_profiles
    from omm.engines.ollama import OllamaAdapter

    config.update_config(onboarding_completed=True, auto_import_enabled=False,
                         telemetry_send_policy="never", usage_stats_policy="never",
                         error_report_send_policy="never")
    benchmark.OLLAMA_HOST = base_url
    adapter = OllamaAdapter(base_url)
    cli._compatibility_adapter = lambda engine: adapter if engine == "ollama" else None
    report = {"root": str(root), "model_sha256": actual, "base_url": base_url,
              "verification": "real local Ollama and native CLI; private endpoint routing"}
    with (root / "server.log").open("w") as log:
        server = subprocess.Popen([binary, "serve"], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while not adapter.health().reachable:
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"Private Ollama did not start; see {root / 'server.log'}")
                time.sleep(0.25)
            modelfile = root / "Modelfile"
            modelfile.write_text(f"FROM {json.dumps(str(model))}\nPARAMETER num_predict 16\nPARAMETER temperature 0\n")
            imported = subprocess.run([binary, "create", "omm-profile-smoke", "-f", str(modelfile)],
                                      env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            if imported.returncode:
                raise RuntimeError("Private model import failed: " + imported.stderr[-1500:])
            registry.upsert_entry(model.name, sha256=actual, size_bytes=model.stat().st_size,
                                  ollama_name="omm-profile-smoke", ollama_runtime_name="omm-profile-smoke:latest",
                                  linked={"ollama": True})

            def invoke(arguments):
                stdout, stderr = io.StringIO(), io.StringIO()
                original_args = sys.argv
                try:
                    sys.argv = ["omm", *arguments]
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        try:
                            cli.main()
                            code = 0
                        except SystemExit as error:
                            code = error.code or 0
                    if code:
                        raise RuntimeError(f"CLI command failed ({arguments[0]}): {stderr.getvalue()}")
                    return stdout.getvalue()
                finally:
                    sys.argv = original_args

            tuned = json.loads(invoke(["tune", model.name, "--apply", "--save", "--engine", "ollama", "--yes", "--json"]))
            assert tuned["saved"] and tuned["proposed"]["temporary_load_released"]
            persisted = json.loads((hub / "runtime-profiles.json").read_text())
            assert persisted["models"][model.name]["ollama"]["active"]["sha256"] == actual
            report["tune"] = tuned
            invoke(["verify", model.name, "--engine", "ollama", "--yes"])
            report["verify"] = registry.load_registry()[model.name]["compatibility"]["ollama"]
            prompt = root / "input.txt"
            prompt.write_text("Once upon a time\n")
            native_calls = []
            original_call = subprocess.call
            def capture_native(command, **kwargs):
                if len(command) < 3 or command[1] != "run":
                    raise RuntimeError("Unexpected launch in the smoke test")
                with prompt.open() as stdin:
                    result = subprocess.run(command, stdin=stdin, env=env, capture_output=True,
                                            text=True, encoding="utf-8", errors="replace", timeout=120)
                resident = adapter._client.request("GET", "/api/ps").data.get("models", [])
                observed = next((item.get("context_length") for item in resident
                                 if item.get("name") in {command[2], command[2] + ":latest"}), None)
                assert observed == tuned["proposed"]["options"]["context_length"], "Native CLI reset the saved context"
                native_calls.append({"returncode": result.returncode,
                                     "nonempty_output": bool(result.stdout.strip()),
                                     "observed_context_after_chat": observed})
                return result.returncode
            try:
                subprocess.call = capture_native
                invoke(["run", model.name, "--engine", "ollama", "--yes"])
            finally:
                subprocess.call = original_call
            assert native_calls and native_calls[0]["returncode"] == 0 and native_calls[0]["nonempty_output"]
            report["native_run"] = native_calls
            assert not any(item.loaded for item in adapter.list_models())
            assert not any(item.key.startswith("omm-profile-") for item in adapter.list_models()
                           if item.key != "omm-profile-smoke:latest")
            assert not list((hub / "runtime-sessions").glob("*.json"))
            restored = json.loads(invoke(["setting", "runtime-profile", model.name, "--restore", "--json"]))
            assert restored["status"] == "default"
            report["restore"] = restored
            assert runtime_profiles.saved_options_for_file(model.name, "ollama", model) is None
            report["status"] = "passed"
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
            report["owned_server_stopped"] = server.poll() is not None
            (root / "verification.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({"report": str(root / "verification.json"), "status": report.get("status"),
                      "owned_server_stopped": report["owned_server_stopped"]}))


if __name__ == "__main__":
    main()
