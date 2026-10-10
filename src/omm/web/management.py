"""Bounded local views and explicit connectivity checks for the manager."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from omm import config, doctor, hardware, linker, registry, runtime_profiles, tuning
from omm.atomic import atomic_write_text, locked

POLICIES = {"telemetry_send_policy": {"ask", "never", "always"},
            "error_report_send_policy": {"ask", "never", "always"},
            "usage_stats_policy": {"never", "enabled"},
            "memory_guard_policy": {"ask", "block", "observe"}}


def settings() -> dict:
    # Inspection does not migrate, back up or initialize the configuration.
    raw = {}
    if config.CONFIG_PATH.exists():
        if config.CONFIG_PATH.is_symlink():
            raise ValueError("설정 파일의 연결 경로를 확인해 주세요.")
        raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("설정 파일 형식을 확인해 주세요. 파일은 변경하지 않았어요.")
    values = config._merge_config(raw)
    return {key: values.get(key) or ("never" if key != "memory_guard_policy" else "ask") for key in POLICIES}


def save_settings(changes: dict) -> dict:
    if not isinstance(changes, dict) or not changes or set(changes) - POLICIES.keys():
        raise ValueError("지원하지 않는 설정 항목이에요.")
    for key, value in changes.items():
        if not isinstance(value, str) or value not in POLICIES[key]:
            raise ValueError("설정값이 올바르지 않아요.")
    with locked(config.CONFIG_PATH):
        settings()  # Refuse corrupt data rather than silently replacing it.
        raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8")) if config.CONFIG_PATH.exists() else {}
        values = config._merge_config(raw)
        values.update(changes)
        atomic_write_text(config.CONFIG_PATH, json.dumps(values, indent=2) + "\n")
    return settings()


def connections() -> dict:
    from omm.web.probe import adapter_for
    rows = []
    for spec in linker.ENGINES:
        row = {"key": spec.key, "name": spec.label, "installed": linker.is_engine_installed(spec.key),
               "api_status": "unsupported", "version": None}
        if spec.key in {"ollama", "lmstudio"}:
            try:
                health = adapter_for(spec.key).health()
                row.update(api_status="ready" if health.reachable else "unreachable", version=health.version)
            except Exception:
                row["api_status"] = "unreachable"
        rows.append(row)
    return {"engines": rows, "checked_at": datetime.now(timezone.utc).isoformat()}


def check_network() -> dict:
    import requests
    rows = []
    # Fixed destinations only; no model download, retries or redirect following.
    for name, url in (("모델 다운로드 서버", "https://huggingface.co/"),
                      ("추천 자료 서버", "https://raw.githubusercontent.com/omm-hippo/omm/main/README.md")):
        try:
            response = requests.head(url, timeout=3, allow_redirects=False)
            state = "ready" if 200 <= response.status_code < 400 else "unavailable"
            rows.append({"name": name, "status": state, "http_status": response.status_code})
            response.close()
        except requests.RequestException:
            rows.append({"name": name, "status": "unavailable", "http_status": None})
    return {"targets": rows, "checked_at": datetime.now(timezone.utc).isoformat(), "requests": len(rows)}


def diagnostics() -> dict:
    return {**doctor.collect_report().as_dict(), "checked_at": datetime.now(timezone.utc).isoformat(),
            "changed": False}


def model_settings(filename: str, entry: dict) -> dict:
    from omm import cli
    path = cli._managed_model_path(filename)
    candidate = {**entry, "filename": filename, "size_bytes": path.stat().st_size}
    metadata = runtime_profiles._metadata(path)
    candidate["context_length"] = metadata.get(f"{metadata.get('general.architecture')}.context_length")
    profile = tuning.recommend_runtime_settings(hardware.scan_hardware(), candidate)
    digest = entry.get("sha256")
    engines = {}
    for engine in ("ollama", "lmstudio"):
        if entry.get("linked", {}).get(engine):
            options = runtime_profiles.proposed_options(profile, path, engine)
            engines[engine] = {"proposed": {k: v for k, v in asdict(options).items() if k != "verify_applied" and v is not None},
                               "saved": runtime_profiles.describe(filename, engine, digest)}
    return {"filename": filename, "recommended": asdict(profile), "engines": engines}


def files() -> dict:
    from omm.web.service import identifier
    root = config.MODELS_DIR
    partials = []
    if root.is_symlink():
        raise ValueError("모델 폴더의 연결 경로를 확인해 주세요.")
    if root.exists():
        for path in sorted(root.glob("*.part"))[:100]:
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                partials.append({"id": identifier(path.name), "filename": path.name,
                                 "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    capacity = shutil.disk_usage(ancestor)
    return {"path": str(root), "available_bytes": capacity.free, "total_bytes": capacity.total,
            "managed_bytes": sum((root / name).stat().st_size for name in registry.load_registry()
                                 if (root / name).is_file() and (root / name).resolve().is_relative_to(root.resolve())),
            "partials": partials}


def cleanup_selected(names: list[dict]) -> dict:
    from omm.install_state import cleanup_guard
    removed = []
    for selected in names:
        name = selected["filename"]
        if Path(name).name != name or not name.endswith(".part") or config.MODELS_DIR.is_symlink():
            raise ValueError("정리 대상이 올바르지 않아요.")
        path = config.MODELS_DIR / name
        with cleanup_guard(name[:-5]):
            if path.is_symlink() or not path.is_file():
                raise ValueError("정리 대상이 바뀌었어요. 다시 확인해 주세요.")
            stat = path.stat()
            if stat.st_size != selected["size_bytes"] or stat.st_mtime_ns != selected["mtime_ns"]:
                raise ValueError("다운로드 파일이 변경됐어요. 다시 확인해 주세요.")
            path.unlink()
            removed.append(name)
    return {"removed": removed}
