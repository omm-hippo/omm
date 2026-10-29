"""Start a harness on local Ollama for one session; no settings file is written."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from omm import agent_connection

HARNESSES = ("claude", "codex")


class LaunchError(ValueError):
    """An actionable error that does not contain configuration values."""


def _toml_inline(table):
    # JSON strings, numbers and lowercase booleans are valid TOML values.
    return "{" + ", ".join(f"{key} = {json.dumps(value)}" for key, value in table.items()) + "}"


def command(harness, metadata, extra_args=()):
    """argv and environment overrides that route one harness run to local Ollama.

    Codex gets `-c` overrides, which outrank config.toml and profiles; Claude
    Code gets process environment plus `--model`. The context overrides keep
    a cloud-sized `model_context_window` in config.toml from disabling
    auto-compaction against a smaller local model. Ollama's Responses API
    rejects every request that lists a non-function tool, and Codex 0.165
    sends two by default (`namespace` for sub-agents, `web_search`).
    """
    if harness not in HARNESSES:
        raise LaunchError("omm code supports claude and codex. For pi, use omm setting agent local pi.")
    binary = shutil.which(harness)
    if binary is None:
        raise LaunchError(f"{harness} is not installed or is not on PATH. Install the harness first.")
    model, base_url, context = metadata["model"], metadata["base_url"], metadata["context_length"]
    if harness == "claude":
        return [binary, "--model", model, *extra_args], agent_connection.claude_env(model, base_url)
    overrides = {
        "model_provider": json.dumps("omm-local"),
        "model_providers.omm-local": _toml_inline(agent_connection.codex_provider(base_url)),
        "model": json.dumps(model),
        "model_context_window": str(context),
        "model_auto_compact_token_limit": str(context * 9 // 10),
        "features.multi_agent": "false",
        "web_search": json.dumps("disabled"),
    }
    argv = [binary]
    for key, value in overrides.items():
        argv += ["-c", f"{key}={value}"]
    return [*argv, *extra_args], {}


def run(argv, env_overrides):
    """Run the harness in the foreground and return its exit status.

    Ctrl+C belongs to the harness; omm keeps waiting so it never exits
    under a still-running TUI.
    """
    process = subprocess.Popen(argv, env={**os.environ, **env_overrides})
    while True:
        try:
            code = process.wait()
            break
        except KeyboardInterrupt:
            continue
    return code if code >= 0 else 128 - code
