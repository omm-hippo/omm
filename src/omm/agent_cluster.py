"""Route a harness to the company cluster gateway instead of local Ollama (#502/#503).

The head address and employee token come from `omm rag remote set`
(rag_remote.py), so an employee types the token once for both documents and
the model. The edit reuses agent_connection's journal and field list exactly,
so `omm setting agent cloud` restores it like a local connection. Only the
values differ: the gateway URL and the employee token instead of loopback
Ollama and a placeholder key, which is why every file written here is made
owner-only (rag_connect does the same).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from urllib import error as urlerror, request as urlrequest

from omm import agent_connection as agent, atomic, rag_remote
from omm.agent_connection import ConnectionError

MIN_CONTEXT = 32768
TIMEOUT = 15


def _remote() -> rag_remote.Remote:
    try:
        remote = rag_remote.load()
    except rag_remote.RemoteError as error:
        raise ConnectionError(str(error)) from error
    if remote is None:
        raise ConnectionError("Set the cluster head and your employee token first: omm rag remote set <head address>")
    return remote


def preflight(model: str, remote: rag_remote.Remote) -> dict:
    """Check the token and the model against the gateway's /v1/models."""
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", model):
        raise ConnectionError("Use an exo model id such as mlx-community/Qwen3.5-9B-4bit.")
    request = urlrequest.Request(remote.url + "/v1/models", headers={
        "Authorization": f"Bearer {remote.token}", "Accept": "application/json"})
    # No proxy: the token must go straight to the head (rag_remote does the same).
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read() or b"{}")
    except urlerror.HTTPError as error:
        if error.code == 401:
            raise ConnectionError("The cluster gateway rejected the token. Ask the admin for a new one "
                                  "and run omm rag remote set again.") from None
        raise ConnectionError(f"The cluster gateway answered {error.code}. Check that the head runs omm cluster.") from None
    except (urlerror.URLError, OSError, ValueError):
        raise ConnectionError(f"Could not reach the cluster gateway at {remote.url}. "
                              "Check the office network and that the head Mac is on.") from None
    entries = data.get("data") if isinstance(data, dict) else None
    entry = next((m for m in entries or [] if isinstance(m, dict) and m.get("id") == model), None)
    if entry is None:
        # The gateway lists only models with a ready instance (#531).
        raise ConnectionError(f"{model} is not loaded on the cluster. Ask the admin which model "
                              "omm cluster status shows as serving.")
    context = entry.get("context_length")
    context = context if type(context) is int else 0
    if context < MIN_CONTEXT:
        raise ConnectionError("Agent connections require a model context of at least 32768 tokens; 65536+ is recommended.")
    return {"model": model, "base_url": remote.url, "context_length": context, "target": "cluster",
            "tools_advertised": False, "tool_workflow_verified": False}


def _patches(harness, model, remote, context):
    """agent_connection's fields with the gateway's values; same keys, so restore accepts them."""
    files = agent._patches(harness, model, remote.url, context)
    result = []
    for patches in files:
        changed = []
        for keys, value in patches:
            if keys == ("env", "ANTHROPIC_AUTH_TOKEN"):
                value = remote.token
            elif keys == ("model_providers", "omm-local"):
                value = {**value, "name": "OMM cluster", "experimental_bearer_token": remote.token}
            elif keys == ("providers", "omm-local"):
                value = {**value, "apiKey": remote.token}
            changed.append((keys, value))
        result.append(changed)
    return result


def connect(harness: str, model: str) -> dict:
    agent._check_harness(harness)
    if shutil.which(harness) is None:
        raise ConnectionError(f"{harness} is not installed or is not on PATH. Install the harness first.")
    remote = _remote()
    journal = agent._journal(harness)
    agent._private_journal_directory(journal)
    with atomic.locked(journal):
        if journal.exists():
            raise ConnectionError("An OMM connection backup already exists. Restore with agent cloud before switching again.")
        metadata = preflight(model, remote)
        entries = []
        for path, patches in zip(agent._paths(harness), _patches(harness, model, remote, metadata["context_length"])):
            raw = agent._read(path)
            document = agent._parse(raw, path.suffix == ".toml")
            for keys, value in patches:
                present, previous = agent._lookup(document, keys)
                if present and ((isinstance(value, str) and not isinstance(previous, str)) or
                                (isinstance(value, dict) and not hasattr(previous, "keys"))):
                    raise ConnectionError("A routing setting has an unexpected type; no settings changed.")
                agent._set(document, keys, True, value)
            entries.append({"path": str(path), "before": agent._encode(raw),
                            "mode": path.stat().st_mode & 0o777 if raw is not None else None,
                            "applied": agent._encode(agent._dump(document, path.suffix == ".toml")),
                            "fields": [list(keys) for keys, _ in patches]})
        data = {"schema_version": 1, "harness": harness, "phase": "applying",
                "metadata": metadata, "files": entries}
        agent._store(journal, data)
        try:
            for entry in entries:
                path = Path(entry["path"])
                if agent._read(path) != agent._decode(entry["before"]):
                    raise ConnectionError("Configuration changed during connection; backup retained for recovery.")
                agent._write(path, agent._decode(entry["applied"]))
                path.chmod(0o600)  # The file now holds the employee token.
            data["phase"] = "local"
            agent._store(journal, data)
        except OSError as error:
            raise ConnectionError("Connection write was interrupted. Backup retained; run agent cloud to recover.") from error
    return metadata
