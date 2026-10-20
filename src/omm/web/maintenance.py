"""Preview external files before adopting a selected, revalidated group."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from omm import config, scan_import
from omm.atomic import atomic_write_text, locked
from omm.hashutil import sha256_file


def _preview_path() -> Path:
    return config.OMM_HOME / "web-jobs" / "import-preview.json"


def read_import_preview() -> list[dict]:
    path = _preview_path()
    if not path.exists():
        return []
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("외부 모델 검색 기록을 확인해 주세요.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("외부 모델 검색 기록 형식이 올바르지 않아요.")
    return value


def scan() -> dict:
    from omm.web.service import identifier
    groups = scan_import.group_by_hash(scan_import.find_external_models())
    rows = []
    for group in groups[:50]:
        rows.append({"id": identifier(group.sha256), "sha256": group.sha256, "name": group.display_name,
                     "size_bytes": group.size_bytes, "engines": group.engines,
                     "locations": [{**asdict(x), "path": str(x.path)} for x in group.locations]})
    if _preview_path().is_symlink() or _preview_path().parent.is_symlink():
        raise ValueError("검색 기록 경로를 확인해 주세요.")
    atomic_write_text(_preview_path(), json.dumps(rows, ensure_ascii=False) + "\n")
    return {"groups": [{k: v for k, v in row.items() if k != "locations"} for row in rows],
            "count": len(groups), "shown": len(rows)}


def adopt(selected: dict) -> dict:
    # Only a server-generated preview, never a client-supplied filesystem path.
    fresh = next((x for x in read_import_preview() if x["id"] == selected["id"]), None)
    if fresh != selected:
        raise ValueError("외부 모델 검색 결과가 바뀌었어요. 다시 선택해 주세요.")
    locations = []
    for item in selected["locations"]:
        path = Path(item["path"])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size_bytes"] or sha256_file(path) != selected["sha256"]:
            raise ValueError("외부 파일이 바뀌었어요. 다시 검색해 주세요.")
        locations.append(scan_import.ExternalGguf(**{**item, "path": path}))
    result = scan_import.adopt_group(scan_import.ModelGroup(selected["sha256"], locations))
    from omm import registry
    if not (config.MODELS_DIR / result.filename).is_file() or result.filename not in registry.load_registry():
        raise ValueError("가져온 파일의 저장 결과를 확인하지 못했어요.")
    with locked(_preview_path()):
        remaining = [x for x in read_import_preview() if x["id"] != selected["id"]]
        atomic_write_text(_preview_path(), json.dumps(remaining, ensure_ascii=False) + "\n")
    return {"filename": result.filename, "bytes_saved": result.bytes_saved, "warnings": result.link_warnings}
