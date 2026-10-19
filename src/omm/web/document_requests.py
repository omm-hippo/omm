"""Validate browser-selected documents and exact managed-model identifiers."""
from __future__ import annotations
import re
from omm.documents import MAX_UPLOAD


def validate(path, body, service):
    if not isinstance(body, dict) or body.get("confirmed") is not True:
        raise ValueError("문서 작업을 확인해 주세요.")
    common = {"confirmed", "request_id"}
    if path == "/api/documents":
        if set(body) - common - {"name", "content", "replace"}:
            raise ValueError("지원하지 않는 문서 등록 항목이에요.")
        content, name = body.get("content"), body.get("name")
        if not isinstance(content, str) or not content or len(content) > ((MAX_UPLOAD + 2) // 3) * 4:
            raise ValueError("문서는 6 MiB까지 등록할 수 있어요.")
        if not isinstance(name, str) or not 1 <= len(name) <= 180:
            raise ValueError("파일 이름을 확인해 주세요.")
        if type(body.get("replace", False)) is not bool:
            raise ValueError("문서 교체 여부를 확인해 주세요.")
        return {"operation": "document_import", "filename": name, "content": content, "replace": body.get("replace", False)}
    if path == "/api/documents/delete":
        if set(body) != common | {"document_id"} or not isinstance(body["document_id"], str):
            raise ValueError("삭제할 문서를 다시 선택해 주세요.")
        return {"operation": "document_delete", "filename": "document", "document_id": body["document_id"]}
    if set(body) != common | {"question", "document_ids", "model_id", "engine"}:
        raise ValueError("질문과 문서, 모델, 실행 앱을 선택해 주세요.")
    question, ids = body["question"], body["document_ids"]
    if not isinstance(question, str) or not 2 <= len(question.strip()) <= 600:
        raise ValueError("질문은 2~600자로 입력해 주세요.")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(x, str) or len(x) > 40 for x in ids):
        raise ValueError("질문할 문서를 1~20개 선택해 주세요.")
    if body["engine"] not in {"ollama", "lmstudio"} or not isinstance(body["model_id"], str):
        raise ValueError("연결된 Ollama 또는 LM Studio 모델을 선택해 주세요.")
    selected = next((row for row in service.models() if row["id"] == body["model_id"]), None)
    if not selected or not selected["exists"] or body["engine"] not in selected["engines"]:
        raise ValueError("내 모델에서 실행 앱 연결을 확인해 주세요.")
    return {"operation": "document_question", "filename": selected["filename"], "engine": body["engine"],
            "question": question.strip(), "document_ids": ids}
