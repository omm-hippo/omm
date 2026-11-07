"""Reversible user-level harness routing; no launch, cloud call or model load."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil

from omm import atomic, config
from omm.engines.base import LoopbackJsonClient, RuntimeAdapterError

HARNESSES = ("claude", "codex", "pi")
MAX_BYTES = 2 * 1024 * 1024
# Ollama runs every model with its own small default context (4096 on an 8 GB
# Mac) whatever the model supports, and its OpenAI/Anthropic endpoints ignore a
# per-request num_ctx. pi's system prompt alone is about 6.4K tokens, so every
# request failed with 400. connect therefore creates a derived Ollama model that
# only sets num_ctx (it shares the base model's weights; nothing is copied),
# routes the harness to it, and restore deletes it again.
RUNTIME_CONTEXTS = (65536, 32768, 16384)
# Share of total RAM the weights plus the f16 KV cache may take, leaving room for
# compute buffers, the harness and the OS. On an 8 GB Mac an 8B model (5.7 GB
# loaded) nearly froze the machine; this refuses it and gives qwen3-0.6b 16K.
MEMORY_SHARE = 0.4
_DERIVED_TAG = re.compile(r"-omm\d+k$")


class ConnectionError(ValueError):
    """An actionable error that does not contain configuration values."""


def _check_harness(harness):
    if harness not in HARNESSES:
        raise ConnectionError("Choose claude, codex or pi.")


def _paths(harness):
    home = Path.home()
    if harness == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        return [root / "settings.json"]
    if harness == "codex":
        root = Path(os.environ.get("CODEX_HOME", home / ".codex"))
        return [root / "config.toml"]
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", home / ".pi" / "agent"))
    return [root / "models.json", root / "settings.json"]


def _safe(path):
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ConnectionError("Configuration paths must be absolute and must not use symbolic links.")


def _read(path, max_bytes=MAX_BYTES):
    _safe(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise ConnectionError("Configuration must be a regular file smaller than 2 MiB.")
    return path.read_bytes()


def _parse(raw, toml=False):
    try:
        text = (raw or b"").decode("utf-8")
        if toml:
            import tomlkit
            return tomlkit.parse(text)
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        value = json.loads(text, object_pairs_hook=pairs) if text.strip() else {}
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, TypeError, UnicodeError) as error:
        raise ConnectionError("Configuration is not a supported JSON object or TOML document; no settings changed.") from error


def _dump(value, toml=False):
    if toml:
        import tomlkit
        return tomlkit.dumps(value).encode("utf-8")
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _lookup(document, keys):
    current = document
    for key in keys:
        if not hasattr(current, "keys") or key not in current:
            return False, None
        current = current[key]
    return True, current


def _set(document, keys, present, value):
    current = document
    for key in keys[:-1]:
        if key not in current:
            if not present:
                return
            current[key] = {}
        if not hasattr(current[key], "keys"):
            raise ConnectionError("A routing setting has an unexpected structure; no settings changed.")
        current = current[key]
    if present:
        current[keys[-1]] = value
    else:
        current.pop(keys[-1], None)


def _patches(harness, model, base_url, context):
    if harness == "claude":
        return [[(("model",), model), *(( ("env", key), value) for key, value in {
            "ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_API_KEY": "", "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
        }.items())]]
    if harness == "codex":
        return [[(("model",), model), (("model_provider",), "omm-local"),
                 (("model_providers", "omm-local"), {
                     "name": "OMM local Ollama", "base_url": base_url + "/v1",
                     "wire_api": "responses", "requires_openai_auth": False,
                 })]]
    return [[(("providers", "omm-local"), {
        "baseUrl": base_url + "/v1", "api": "openai-completions", "apiKey": "ollama",
        "models": [{"id": model, "name": model, "contextWindow": context,
                    "maxTokens": min(8192, context // 4), "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
    })], [(("defaultProvider",), "omm-local"), (("defaultModel",), model)]]


def preflight(model, base_url):
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", model):
        raise ConnectionError("Use an installed Ollama model tag.")
    if model.lower().endswith((":cloud", "-cloud")):
        raise ConnectionError("Choose a downloaded local model; cloud model tags are not local routing.")
    try:
        client = LoopbackJsonClient(base_url)
    except ValueError as error:
        raise ConnectionError("Use an HTTP(S) loopback origin without a path or credentials.") from error
    try:
        version = client.request("GET", "/api/version").data.get("version", "")
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(version))
        if not match or tuple(map(int, match.groups())) < (0, 14, 0):
            raise ConnectionError("Ollama 0.14.0 or newer is required for agent connections.")
        data = client.request("POST", "/api/show", payload={"model": model}).data
    except RuntimeAdapterError as error:
        raise ConnectionError("Ollama model inspection failed. Start Ollama and check the installed model tag.") from error
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, list) or "tools" not in capabilities:
        raise ConnectionError("This model does not advertise tool calling. Choose a tool-capable model with omm recommend.")
    info = data.get("model_info")
    lengths = [v for k, v in info.items() if k.endswith(".context_length") and type(v) is int] if isinstance(info, dict) else []
    context = max(lengths, default=0)
    if context < 32768:
        raise ConnectionError("Agent connections require a model context of at least 32768 tokens; 65536+ is recommended.")
    # A loopback Ollama server may still proxy a cloud model. Refuse explicit
    # cloud/remote metadata even when an alias hides the usual cloud suffix.
    if any(data.get(key) for key in ("remote_host", "remote_model", "cloud")):
        raise ConnectionError("This Ollama model points to a remote service. Choose a downloaded local model.")
    return {"model": model, "base_url": client.base_url, "context_length": context,
            "weights_bytes": _weights_bytes(client, model), "kv_bytes_per_token": _kv_bytes_per_token(info),
            "tools_advertised": True, "tool_workflow_verified": False}


def _weights_bytes(client, model):
    """Size of the model file from /api/tags, or 0 when Ollama does not say."""
    try:
        rows = client.request("GET", "/api/tags", timeout=3).data.get("models")
    except RuntimeAdapterError:
        return 0
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and model in (row.get("name"), row.get("model")) and type(row.get("size")) is int:
            return row["size"]
    return 0


def _kv_bytes_per_token(info):
    """f16 KV cache bytes per token from GGUF metadata, or None when it is missing.

    Sliding-window and hybrid models need less than this; the estimate errs high.
    """
    if not isinstance(info, dict) or not isinstance(info.get("general.architecture"), str):
        return None
    arch = info["general.architecture"]
    def number(key):
        value = info.get(f"{arch}.{key}")
        return value if type(value) is int and value > 0 else None
    layers, heads = number("block_count"), number("attention.head_count")
    width = number("embedding_length")
    kv_heads = info.get(f"{arch}.attention.head_count_kv")
    if isinstance(kv_heads, list):  # per-layer counts
        kv_total = sum(h for h in kv_heads if type(h) is int and h > 0)
    elif type(kv_heads) is int and kv_heads > 0 and layers:
        kv_total = kv_heads * layers
    else:
        return None
    head_width = width // heads if width and heads else None
    key, value = number("attention.key_length") or head_width, number("attention.value_length") or head_width
    if not kv_total or not key or not value:
        return None
    return kv_total * (key + value) * 2


def runtime_context(metadata, ram_bytes):
    """Largest of RUNTIME_CONTEXTS the model supports that fits the memory share."""
    weights, per_token = metadata.get("weights_bytes") or 0, metadata.get("kv_bytes_per_token")
    budget = ram_bytes * MEMORY_SHARE - weights
    for context in RUNTIME_CONTEXTS:
        if context > metadata["context_length"]:
            continue
        if per_token is None:
            # Without layer metadata nothing can be sized; take the smallest.
            return RUNTIME_CONTEXTS[-1]
        if per_token * context <= budget:
            return context
    raise ConnectionError(f"This model with a {RUNTIME_CONTEXTS[-1] // 1024}K-token runtime context needs more "
                          f"than {MEMORY_SHARE:.0%} of this computer's memory. Choose a smaller model with omm recommend.")


def runtime_model(model, context):
    """Derived Ollama tag, e.g. qwen3:8b -> qwen3:8b-omm32k."""
    name, _, tag = model.rpartition(":") if ":" in model.rsplit("/", 1)[-1] else (model, "", "latest")
    return f"{name}:{tag}-omm{context // 1024}k"


def _total_ram_bytes():
    import psutil
    try:
        return psutil.virtual_memory().total
    except (OSError, psutil.Error):
        return 0


def _create_runtime_model(base_url, model, derived, context):
    try:
        LoopbackJsonClient(base_url).request(
            "POST", "/api/create", timeout=120,
            payload={"model": derived, "from": model, "parameters": {"num_ctx": context}, "stream": False})
    except RuntimeAdapterError as error:
        raise ConnectionError("Ollama could not create the runtime-context model; no settings changed.") from error


def _delete_runtime_model(metadata):
    """Delete the derived model; returns its tag when that failed (Ollama off), else None."""
    derived = metadata.get("runtime_model") if isinstance(metadata, dict) else None
    if not isinstance(derived, str) or not _DERIVED_TAG.search(derived):
        return None
    try:
        LoopbackJsonClient(metadata["base_url"]).request(
            "DELETE", "/api/delete", payload={"model": derived}, timeout=10, allow_empty=True)
    except (KeyError, ValueError, RuntimeAdapterError):
        return derived
    return None


def installed_models(base_url="http://127.0.0.1:11434"):
    """Local Ollama model tags for a picker. Any failure (Ollama off, bad
    origin, odd reply) is an empty list: the caller falls back to typing."""
    try:
        rows = LoopbackJsonClient(base_url).request(
            "GET", "/api/tags", timeout=3, default_failure="server_unavailable").data.get("models")
    except (ValueError, RuntimeAdapterError):
        return []
    names = {row.get("name") for row in rows if isinstance(row, dict)} if isinstance(rows, list) else set()
    return sorted(n for n in names if isinstance(n, str) and n and not n.lower().endswith((":cloud", "-cloud"))
                  and not _DERIVED_TAG.search(n))


def _journal(harness):
    return config.OMM_HOME / "agent-connections" / (harness + ".json")


def _encode(raw):
    return base64.b64encode(raw).decode("ascii") if raw is not None else None


def _decode(raw):
    return base64.b64decode(raw, validate=True) if raw is not None else None


def _private_journal_directory(path):
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The lock helper also creates parents. Tighten an existing directory before
    # locking or writing so the caller's umask cannot expose connection backups.
    if os.name != "nt":
        path.parent.chmod(0o700)


def _store(path, data):
    _private_journal_directory(path)
    atomic.atomic_write_text(path, json.dumps(data, ensure_ascii=False))
    path.chmod(0o600)


def _write(path, raw):
    _safe(path)
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        atomic.atomic_write_bytes(path, raw)


def connect(harness, model, *, base_url="http://127.0.0.1:11434"):
    _check_harness(harness)
    if shutil.which(harness) is None:
        raise ConnectionError(f"{harness} is not installed or is not on PATH. Install the harness first.")
    journal = _journal(harness)
    _private_journal_directory(journal)
    with atomic.locked(journal):
        if journal.exists():
            raise ConnectionError("An OMM connection backup already exists. Restore with agent cloud before switching again.")
        metadata = preflight(model, base_url)
        context = runtime_context(metadata, _total_ram_bytes())
        derived = runtime_model(model, context)
        metadata.update(runtime_model=derived, runtime_context=context)
        paths = _paths(harness)
        entries = []
        for path, patches in zip(paths, _patches(harness, derived, metadata["base_url"], context)):
            raw = _read(path)
            document = _parse(raw, path.suffix == ".toml")
            for keys, value in patches:
                present, previous = _lookup(document, keys)
                if present and ((isinstance(value, str) and not isinstance(previous, str)) or
                                (isinstance(value, dict) and not hasattr(previous, "keys"))):
                    raise ConnectionError("A routing setting has an unexpected type; no settings changed.")
                _set(document, keys, True, value)
            entries.append({"path": str(path), "before": _encode(raw),
                            "mode": path.stat().st_mode & 0o777 if raw is not None else None,
                            "applied": _encode(_dump(document, path.suffix == ".toml")),
                            "fields": [list(keys) for keys, _ in patches]})
        # All files are parsed and all backups persisted before the first write.
        data = {"schema_version": 1, "harness": harness, "phase": "applying",
                "metadata": metadata, "files": entries}
        _store(journal, data)
        try:
            _create_runtime_model(metadata["base_url"], model, derived, context)
        except ConnectionError:
            journal.unlink()  # No file was written yet.
            raise
        try:
            for entry in entries:
                path = Path(entry["path"])
                if _read(path) != _decode(entry["before"]):
                    raise ConnectionError("Configuration changed during connection; backup retained for recovery.")
                _write(path, _decode(entry["applied"]))
            data["phase"] = "local"
            _store(journal, data)
        except OSError as error:
            raise ConnectionError("Connection write was interrupted. Backup retained; run agent cloud to recover.") from error
    return metadata


def _load(journal, harness):
    try:
        data = _parse(_read(journal, max_bytes=MAX_BYTES * 8 + 1024 * 1024))
        if data.get("schema_version") != 1 or data.get("harness") != harness:
            raise ValueError("version")
        if data.get("phase") not in {"applying", "local"}:
            raise ValueError("phase")
        files = data["files"]
        expected = _paths(harness)
        if len(files) != len(expected) or any(Path(e["path"]) != p for e, p in zip(files, expected)):
            raise ConnectionError("Configuration directory changed since connection; restore with the original directory overrides.")
        for entry in files:
            _decode(entry["before"])
            _decode(entry["applied"])
            mode = entry.get("mode")
            if mode is not None and (type(mode) is not int or not 0 <= mode <= 0o777):
                raise ValueError("mode")
        allowed = _patches(harness, "model", "http://127.0.0.1:11434", 32768)
        if any(e["fields"] != [list(keys) for keys, _ in patches] for e, patches in zip(files, allowed)):
            raise ValueError("fields")
        return data
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ConnectionError):
            raise
        raise ConnectionError("Connection backup is invalid; preserve it and restore manually.") from error


def restore(harness):
    """False without a backup; otherwise {"left_model": tag or None}.

    left_model names a derived Ollama model that could not be deleted
    (Ollama not running); the settings are restored either way.
    """
    _check_harness(harness)
    journal = _journal(harness)
    _private_journal_directory(journal)
    with atomic.locked(journal):
        if not journal.exists():
            return False
        data = _load(journal, harness)
        outputs = []
        for entry in data["files"]:
            path = Path(entry["path"])
            current = _read(path)
            before, applied = _decode(entry["before"]), _decode(entry["applied"])
            if current in (before, applied):
                output = before
            else:
                is_toml = path.suffix == ".toml"
                document, original, local = (_parse(raw, is_toml) for raw in (current, before, applied))
                for keys in entry["fields"]:
                    value = _lookup(document, keys)
                    if value not in (_lookup(local, keys), _lookup(original, keys)):
                        raise ConnectionError("A routing field was edited after connection. No files restored; reconcile it with the private backup first.")
                    present, value = _lookup(original, keys)
                    _set(document, keys, present, deepcopy(value))
                # Remove empty containers introduced by OMM, not preexisting ones.
                for keys in sorted(entry["fields"], key=len, reverse=True):
                    for size in range(len(keys) - 1, 0, -1):
                        prefix = keys[:size]
                        present, value = _lookup(document, prefix)
                        if present and value == {} and not _lookup(original, prefix)[0]:
                            _set(document, prefix, False, None)
                output = None if before is None and not document else _dump(document, is_toml)
            outputs.append((path, current, output, entry.get("mode")))
        # Preflight all conflicts before mutating any file. Keep the journal if
        # a later write fails; already restored files are safe on the next run.
        try:
            for path, current, output, mode in outputs:
                if _read(path) != current:
                    raise ConnectionError("Configuration changed during restoration; backup retained.")
                _write(path, output)
                if output is not None and mode is not None:
                    path.chmod(mode)
            journal.unlink()
        except OSError as error:
            raise ConnectionError("Restoration was interrupted. Backup retained; run agent cloud again.") from error
    return {"left_model": _delete_runtime_model(data.get("metadata"))}


def status(harness):
    _check_harness(harness)
    journal = _journal(harness)
    _safe(journal)
    if not journal.exists():
        return {"harness": harness, "state": "unmanaged", "installed": shutil.which(harness) is not None}
    data = _load(journal, harness)
    matches = all(_read(Path(e["path"])) == _decode(e["applied"]) for e in data["files"])
    return {"harness": harness, "state": data["phase"] if matches else "changed_or_interrupted",
            "installed": shutil.which(harness) is not None, "tool_workflow_verified": False}
