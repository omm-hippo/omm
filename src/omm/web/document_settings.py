"""Remembered document-question choices, kept apart from documents and index.

The generation model is a replaceable resource: changing it never touches the
registered documents or their search index (lexical and semantic), and a model that fails on its
first question is rolled back to the last model that produced an answer.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from filelock import FileLock

from omm import config
from omm.atomic import atomic_write_text
from omm.documents import CHUNKING_VERSION, RETRIEVAL_METHOD, DocumentStore

ENGINE_NAMES = {"ollama": "Ollama", "lmstudio": "LM Studio"}
KEPT = ("등록한 문서와 검색 색인은 그대로 사용해요. 단어 색인과 의미 색인(임베딩 모델)은 답변 모델과 별개라 다시 만들 필요가 없어요.")


def _path():
    root = config.OMM_HOME / "documents"
    if root.is_symlink() or config.OMM_HOME.is_symlink():
        raise ValueError("문서 저장 폴더의 연결 경로를 확인해 주세요.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "question-settings.json"
    if path.is_symlink():
        raise ValueError("문서 질문 설정 파일의 연결 경로를 확인해 주세요.")
    return path


def _choice(value):
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(k), str) for k in ("model_id", "filename", "engine")) or value["engine"] not in ENGINE_NAMES:
        return None
    return {k: value[k] for k in ("model_id", "filename", "engine")}


def _read(path):
    try:
        if path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    ids = value.get("document_ids")
    return {"selected": _choice(value.get("selected")), "confirmed": _choice(value.get("confirmed")),
            "document_ids": [x for x in ids if isinstance(x, str)][:20] if isinstance(ids, list) else [],
            "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None}


def _write(path, value):
    value = {**value, "updated_at": datetime.now(timezone.utc).isoformat()}
    atomic_write_text(path, json.dumps(value, ensure_ascii=False) + "\n")
    return value


def _lock(path):
    return FileLock(str(path) + ".lock", timeout=10)


def load() -> dict:
    path = _path()
    with _lock(path):
        value = _read(path)
    return {"selected": value.get("selected"), "confirmed": value.get("confirmed"),
            "document_ids": value.get("document_ids", []), "updated_at": value.get("updated_at"),
            "index": {"retrieval": RETRIEVAL_METHOD, "chunking": CHUNKING_VERSION,
                      "embedding_model": DocumentStore().embedding_settings()["model"]}}


def describe_change(old, new) -> str | None:
    """Plain explanation of what changes and what is kept for a new model."""
    if not old or not new or (old["filename"], old["engine"]) == (new["filename"], new["engine"]):
        return None
    changed = []
    if old["filename"] != new["filename"]:
        changed.append(f"답변 모델: {old['filename']} → {new['filename']}")
    if old["engine"] != new["engine"]:
        changed.append(f"실행 앱: {ENGINE_NAMES[old['engine']]} → {ENGINE_NAMES[new['engine']]}")
    tail = (" 새 모델은 첫 질문에서 실제로 로딩해 확인해요. 실패하면 이전 모델 선택으로 되돌리고 문서·모델 파일은 건드리지 않아요."
            " 답변 품질과 속도는 모델마다 달라서 새 모델이 항상 낫다고 보장하지 않아요.")
    return "바뀌는 것: " + ", ".join(changed) + ". " + KEPT + tail


def select(model_id: str, filename: str, engine: str, document_ids: list[str]) -> dict:
    path = _path()
    new = {"model_id": model_id, "filename": filename, "engine": engine}
    with _lock(path):
        value = _read(path)
        old = value.get("selected")
        saved = _write(path, {"selected": new, "confirmed": value.get("confirmed"), "document_ids": list(document_ids)})
    return {"settings": {**load()}, "change": describe_change(old, new), "saved_at": saved["updated_at"]}


def select_documents(document_ids: list[str]) -> dict:
    path = _path()
    with _lock(path):
        value = _read(path)
        _write(path, {"selected": value.get("selected"), "confirmed": value.get("confirmed"), "document_ids": list(document_ids)})
    return {"settings": load(), "change": None}


def confirm(filename: str, engine: str) -> None:
    """Record the choice that just produced a model answer as the safe fallback."""
    path = _path()
    with _lock(path):
        value = _read(path)
        selected = value.get("selected")
        if selected and (selected["filename"], selected["engine"]) == (filename, engine):
            _write(path, {**value, "confirmed": selected})


def rollback_after_failure(filename: str, engine: str) -> str | None:
    """Undo a new, never-confirmed model choice; documents and files stay as they are."""
    path = _path()
    with _lock(path):
        value = _read(path)
        selected, confirmed = value.get("selected"), value.get("confirmed")
        if not selected or (selected["filename"], selected["engine"]) != (filename, engine):
            return None
        if not confirmed or (confirmed["filename"], confirmed["engine"]) == (filename, engine):
            return None
        _write(path, {**value, "selected": confirmed})
    return (f"이전에 답변했던 모델({confirmed['filename']}, {ENGINE_NAMES[confirmed['engine']]})로 선택을 되돌렸어요. "
            "등록한 문서·검색 색인·모델 파일은 그대로예요.")
