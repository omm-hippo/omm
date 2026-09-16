"""Per-model runtime presets, verified with bounded generation before saving."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import struct
from typing import Callable

from omm import config, contribute_memory, tuning
from omm.atomic import atomic_write_text, locked
from omm.engines.base import LoadOptions, ProbeRequest, RuntimeAdapterError, RuntimeModelRef
from omm.gguf import read_gguf_metadata
from omm.hardware import HardwareInfo, calculate_memory_budget
from omm.hashutil import sha256_file


class ProfileError(RuntimeError):
    pass


def _path() -> Path:
    return config.OMM_HOME / "runtime-profiles.json"


def _read() -> dict:
    path = _path()
    if not path.exists():
        return {"schema_version": 1, "models": {}}
    try:
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("unsafe or oversized profile file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("models"), dict):
            raise ValueError("invalid profile file")
        return data
    except (OSError, ValueError) as error:
        raise ProfileError("Could not read runtime profiles; the file was left unchanged.") from error


def _slot(data: dict, filename: str, engine: str) -> dict:
    if engine not in {"ollama", "lmstudio"}:
        raise ProfileError("Profiles support Ollama and LM Studio.")
    models = data["models"].get(filename, {})
    if not isinstance(models, dict):
        raise ProfileError("Invalid model profile entry.")
    slot = models.get(engine, {"revision": 0, "active": None, "previous": None})
    if not isinstance(slot, dict) or type(slot.get("revision")) is not int or slot["revision"] < 0:
        raise ProfileError("Invalid profile revision.")
    return slot


def _options(record: dict, engine: str) -> LoadOptions:
    values = record.get("options")
    allowed = {"context_length", "cpu_threads", "batch_size", "gpu_layers"}
    if not isinstance(values, dict) or not values or set(values) - allowed:
        raise ProfileError("Invalid saved runtime settings.")
    try:
        options = LoadOptions(**values, verify_applied=True)
    except (TypeError, ValueError) as error:
        raise ProfileError("Saved runtime settings are out of range.") from error
    if engine == "lmstudio" and (options.cpu_threads is not None or options.gpu_layers is not None):
        raise ProfileError("Saved settings contain fields unsupported by LM Studio's load API.")
    return options


def describe(filename: str, engine: str, digest: str | None) -> dict:
    slot = _slot(_read(), filename, engine)
    active = slot.get("active")
    if active is None:
        return {"status": "default", "revision": slot["revision"], "active": None}
    if not isinstance(active, dict):
        raise ProfileError("Invalid active profile.")
    _options(active, engine)
    return {"status": "active" if active.get("sha256") == digest else "stale",
            "revision": slot["revision"], "active": active}


def saved_options(filename: str, engine: str, digest: str | None) -> LoadOptions | None:
    state = describe(filename, engine, digest)
    return _options(state["active"], engine) if state["status"] == "active" else None


def saved_options_for_file(filename: str, engine: str, path: Path) -> LoadOptions | None:
    slot = _slot(_read(), filename, engine)
    if slot.get("active") is None:
        return None
    digest = sha256_file(path)
    state = describe(filename, engine, digest)
    if state["status"] == "stale":
        raise ProfileError("Model bytes changed; run `omm tune --apply --save` again before using this profile.")
    return _options(state["active"], engine)


def _metadata(path: Path) -> dict:
    try:
        header = read_gguf_metadata(path, {"general.architecture"})
        architecture = header.get("general.architecture")
        if isinstance(architecture, str) and architecture:
            header.update(read_gguf_metadata(path, {
                f"{architecture}.block_count", f"{architecture}.context_length",
                *contribute_memory.metadata_keys_for_architecture(architecture),
            }))
        return header
    except (OSError, ValueError, struct.error) as error:
        raise ProfileError("Could not read model metadata for a safe settings trial.") from error


def proposed_options(profile: tuning.RuntimeProfile, path: Path, engine: str) -> LoadOptions:
    metadata = _metadata(path)
    architecture = metadata.get("general.architecture")
    maximum = metadata.get(f"{architecture}.context_length")
    context = profile.context_length
    if type(maximum) is int and maximum > 0:
        if maximum < 128:
            raise ProfileError("This model's context limit is below the supported 128-token minimum.")
        context = min(context, maximum)
    if engine == "lmstudio":
        # Native v1 exposes these settings, but not CPU threads or GPU layers.
        return LoadOptions(context, batch_size=min(profile.num_batch, context), verify_applied=True)
    options, _ = tuning.contribute_ollama_options(profile, metadata)
    return LoadOptions(context, cpu_threads=profile.cpu_threads,
                       batch_size=min(profile.num_batch, context),
                       gpu_layers=options.get("num_gpu"), verify_applied=True)


def ensure_memory(path: Path, options: LoadOptions, hardware: HardwareInfo) -> None:
    metadata = _metadata(path)
    layers = metadata.get(f"{metadata.get('general.architecture')}.block_count")
    if options.gpu_layers == -1:
        percent = 100
    elif options.gpu_layers is None:
        # LM Studio chooses placement; require enough headroom for either pool.
        percent = 0
    elif type(layers) is int and layers > 0:
        percent = min(100, round(options.gpu_layers * 100 / layers))
    else:
        percent = 0
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": path.stat().st_size}, hardware,
        # The existing estimator supports >=256; using 256 for tiny 128-token
        # models is conservative and never understates their required memory.
        context_length=max(256, options.context_length), num_batch=options.batch_size or 128,
        gpu_offload_percent=percent, metadata=metadata,
    )
    budget = calculate_memory_budget(hardware)
    if estimate is None or estimate.resident_ram_gb > budget.ram_budget_gb:
        raise ProfileError("Not enough free RAM for this settings trial; other models and applications were left alone.")
    if estimate.required_vram_gb > (budget.vram_budget_gb or 0):
        raise ProfileError("Not enough free VRAM for this settings trial.")
    if options.gpu_layers is None and not hardware.unified_memory and hardware.vram_total_gb:
        total = path.stat().st_size / 1024**3 + estimate.kv_cache_gb + estimate.compute_buffer_gb + estimate.runtime_overhead_gb
        if total > (budget.vram_budget_gb or 0):
            raise ProfileError("LM Studio chooses GPU placement; this trial requires sufficient GPU headroom.")


def trial(path: Path, adapter, model: RuntimeModelRef, options: LoadOptions,
          hardware_supplier: Callable[[], HardwareInfo]) -> dict:
    """Load an idle runtime, generate briefly, confirm cleanup, and return evidence."""
    health = adapter.health()
    if not health.reachable:
        raise ProfileError("Start the local runtime API before applying settings.")
    if any(item.loaded for item in adapter.list_models()):
        raise ProfileError("The runtime already has loaded models. Retry when it is idle; nothing was unloaded.")
    ensure_memory(path, options, hardware_supplier())
    receipt = None
    failure = None
    probe = None
    try:
        receipt = adapter.load(model, options)
        if receipt.was_already_loaded or not receipt.loaded_by_omm:
            raise ProfileError("A model was loaded concurrently; its settings were preserved.")
        if not receipt.applied_options:
            raise ProfileError("The runtime did not confirm its applied load settings.")
        probe = adapter.generate(receipt, ProbeRequest(max_output_tokens=8, timeout_seconds=60))
        if not probe.text.strip():
            raise ProfileError("The runtime returned no text.")
    except (ProfileError, RuntimeAdapterError) as error:
        failure = error
    finally:
        if receipt is not None and receipt.loaded_by_omm:
            try:
                released = adapter.unload(receipt).unloaded
            except RuntimeAdapterError:
                released = False
            if not released:
                failure = ProfileError("The temporary load could not be confirmed as released; the previous profile was kept.")
    if failure is not None:
        raise ProfileError(str(failure)) from failure
    speed = probe.tokens_per_second
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
        speed = None
    return {
        "status": "passed", "engine": adapter.key, "runtime_version": health.version,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "options": {key: value for key, value in asdict(options).items() if key != "verify_applied" and value is not None},
        "observed_options": dict(receipt.applied_options),
        "tokens_per_second": speed, "output_tokens": probe.output_tokens,
        "temporary_load_released": True,
    }


def save_trial(filename: str, engine: str, path: Path, digest: str, evidence: dict, *, expected_revision: int) -> dict:
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or evidence.get("status") != "passed"
            or evidence.get("engine") != engine or evidence.get("temporary_load_released") is not True):
        raise ProfileError("Only a successful, cleaned-up trial can be saved.")
    if sha256_file(path) != digest:
        raise ProfileError("The model changed during verification; the previous profile was kept.")
    record = {**evidence, "sha256": digest}
    _options(record, engine)
    with locked(_path()):
        data = _read()
        slot = _slot(data, filename, engine)
        if slot["revision"] != expected_revision:
            raise ProfileError("The profile changed concurrently; the newer settings were kept.")
        data["models"].setdefault(filename, {})[engine] = {
            "revision": expected_revision + 1, "active": record, "previous": slot.get("active"),
        }
        atomic_write_text(_path(), json.dumps(data, indent=2, allow_nan=False) + "\n")
    return record


def restore(filename: str, engine: str, digest: str) -> dict:
    with locked(_path()):
        data = _read()
        slot = _slot(data, filename, engine)
        previous = slot.get("previous")
        if previous is not None:
            if not isinstance(previous, dict) or previous.get("sha256") != digest:
                raise ProfileError("The previous profile belongs to different model bytes.")
            _options(previous, engine)
        data["models"].setdefault(filename, {})[engine] = {
            "revision": slot["revision"] + 1, "active": previous, "previous": slot.get("active"),
        }
        atomic_write_text(_path(), json.dumps(data, indent=2, allow_nan=False) + "\n")
    return describe(filename, engine, digest)


def launch_with_profile(path: Path, adapter, model: RuntimeModelRef, options: LoadOptions,
                        hardware_supplier: Callable[[], HardwareInfo], launch: Callable) -> tuple[object, bool]:
    """Use saved settings in the native launcher while preserving preloaded work.

    Ollama's CLI reads model defaults, so a temporary configuration alias is
    necessary; preloading alone would let its next request reset num_ctx.
    LM Studio loads the configured local API instance, then opens its native UI.
    """
    from omm.engines.base import find_runtime_model

    models = adapter.list_models()
    selected = find_runtime_model(models, model)
    if selected is not None and selected.loaded:
        return launch(model.key), False
    if any(item.loaded for item in models):
        raise ProfileError("Other models are loaded. Retry when the runtime is idle; nothing was unloaded.")
    ensure_memory(path, options, hardware_supplier())
    if adapter.key == "ollama":
        marker = None
        def record_intent(reference):
            nonlocal marker
            import psutil

            marker = config.OMM_HOME / "runtime-sessions" / f"{reference.key}.json"
            atomic_write_text(marker, json.dumps({
                "schema_version": 1, "engine": "ollama", "alias": reference.key,
                "model": path.name, "pid": os.getpid(),
                "process_started_at": psutil.Process().create_time(),
            }))
        try:
            alias = adapter.create_profile_model(model, options, on_prepare=record_intent)
        except BaseException:
            # The adapter attempts cleanup after uncertain creation. Re-read
            # actual state before removing its durable recovery hint.
            if marker is not None:
                try:
                    if find_runtime_model(adapter.list_models(), RuntimeModelRef(marker.stem)) is None:
                        marker.unlink(missing_ok=True)
                except (RuntimeAdapterError, OSError):
                    pass
            raise
        try:
            receipt = adapter.load(alias, options)
            if not receipt.loaded_by_omm or not receipt.applied_options:
                raise ProfileError("The temporary profile settings were not confirmed.")
            return launch(alias.key), True
        finally:
            adapter.remove_profile_model(alias)
            if marker is not None:
                marker.unlink(missing_ok=True)
    receipt = adapter.load(model, options)
    keep = False
    try:
        if not receipt.loaded_by_omm or not receipt.applied_options:
            raise ProfileError("The LM Studio load settings were not confirmed.")
        result = launch(model.key)
        keep = bool(result.ok)
        return result, True
    finally:
        if receipt.loaded_by_omm and not keep and not adapter.unload(receipt).unloaded:
            raise ProfileError("The failed launch's temporary model could not be released.")


def interrupted_runs() -> list[dict]:
    """Read-only hints for temporary aliases whose owning CLI process died."""
    import psutil

    root = config.OMM_HOME / "runtime-sessions"
    if not root.is_dir() or root.is_symlink():
        return []
    pending = []
    for path in sorted(root.glob("omm-profile-*.json"))[:1024]:
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > 4096:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not re.fullmatch(r"omm-profile-[0-9a-f]{32}", str(data.get("alias"))):
                continue
            if path.stem != data["alias"] or type(data.get("pid")) is not int or data["pid"] <= 0:
                continue
            try:
                if psutil.Process(data["pid"]).create_time() == data.get("process_started_at"):
                    continue
            except psutil.NoSuchProcess:
                pass
            pending.append(data)
        except (OSError, ValueError, psutil.Error):
            continue
    return pending
