"""Purpose filters over existing hardware-eligible candidates, not quality ranks."""
from __future__ import annotations
import re

from omm import model_wiki

PURPOSES = {"korean": "한국어 대화", "coding": "코딩", "documents": "문서 질문"}


def matches(candidate: dict, purpose: str) -> bool:
    wiki = candidate.get("wiki")
    if not wiki:
        return False
    tasks = set(wiki.get("tasks", []))
    if purpose == "coding":
        return "coding" in tasks
    if purpose == "korean":
        return "multilingual" in tasks
    return "text" in wiki.get("inputs", []) and bool(tasks & {"general", "reasoning"})


def reasons(model: dict, purpose: str) -> list[str]:
    wiki = model["wiki"]
    rationale = {"korean": "위키에 다국어 용도로 분류되어 있어요. 한국어 답변 품질은 별도 확인이 필요해요.",
                 "coding": "위키에 코딩 용도로 분류되어 있어요. 실제 코드 정확도는 별도 확인이 필요해요.",
                 "documents": "텍스트 입력과 일반 대화·추론 용도로 분류되어 있어요. 내 문서 기능이 찾은 발췌문으로 질문할 후보예요."}
    return [rationale[purpose], f"예상 메모리 {model['memory_required_gb']:.1f} GB로 현재 선택한 메모리 예산 안에 들어요.",
            wiki["chooseWhen"]]


def wiki_options(purpose: str) -> list[dict]:
    # When the recommendation cache has no exact wiki matches, keep the
    # guide useful without inventing package identity or memory suitability.
    rows = [m for m in model_wiki.catalog()["models"] if matches({"wiki": m}, purpose)]
    def size(model):
        value = re.fullmatch(r"(\d+(?:\.\d+)?)B", model.get("sizeLabel", ""))
        return float(value[1]) if value else float("inf")
    return sorted(rows, key=lambda m: (size(m), m["id"]))[:3]
