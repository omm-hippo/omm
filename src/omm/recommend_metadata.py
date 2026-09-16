"""Display-only model labels from catalog metadata, never model-name guesses.

These labels describe declared tasks, not measured quality or runtime support.
Missing metadata in old signed catalogs remains unknown; reading it does not
fetch model cards or rewrite the user's cache.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelLabels:
    model_type: str = "Unknown"
    use_case: str = "—"
    type_source: str = "Unknown"
    use_case_source: str = "Unknown"
    features: tuple[str, ...] = ()


# Exact bundled artifacts only: a similar name or another provider is not
# evidence that a model has the same capabilities. Sources are documented in
# docs/recommendation-labels.md.
_CURATED = {
    "tinyllama-1.1b-q4": (
        "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
    ),
    "llama3.1-8b-instruct-q4": (
        "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    ),
    "mistral-7b-instruct-q4": (
        "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
        "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
    ),
}
_VLM_TASKS = {
    "vlm", "vision", "image-text-to-text", "image-to-text",
    "visual-question-answering", "document-question-answering",
}
_EMBEDDING_TASKS = {"embedding", "embeddings", "feature-extraction", "sentence-similarity"}
_TEXT_TASKS = {"llm", "text-generation", "text2text-generation", "conversational"}
# Stable tie-break for multiple task tags; a declared use_case takes priority.
_USE_CASES = {
    "Coding": {"coding", "code", "coder", "code-generation"},
    "Reasoning": {"reasoning", "thinking", "math", "mathematics"},
    "Writing": {"writing", "creative-writing", "roleplay", "role-play"},
    "Translation": {"translation", "translate"},
    "Documents": {
        "documents", "summarization", "summarisation", "long-docs", "long-document",
        "doc-q&a", "document-qa", "question-answering", "document-question-answering",
    },
    "General": {"general", "general-purpose", "general-chat", "chat", "instruct", "conversational"},
}
_TOOL_TASKS = {"tools", "tool-use", "function-calling"}


def _label(value: object) -> str:
    if not isinstance(value, str) or len(value) > 100:
        return ""
    return value.strip().casefold().replace("_", "-").replace(" ", "-").removeprefix("task:")


def catalog_metadata(payload: dict) -> dict:
    """Preserve bounded task metadata already returned by provider searches."""
    result = {}
    if not isinstance(payload, dict):
        return result
    task = payload.get("pipeline_tag")
    if isinstance(task, str) and task.strip() and len(task) <= 100:
        result["pipeline_tag"] = task
    tags = payload.get("tags")
    if isinstance(tags, list):
        clean = [tag for tag in tags[:64] if isinstance(tag, str) and 0 < len(tag) <= 100]
        if clean:
            result["tags"] = clean
    return result


def _curated(candidate: dict) -> bool:
    if candidate.get("provider", "huggingface") != "huggingface":
        return False
    repo_id, filename = candidate.get("repo_id"), candidate.get("filename")
    if repo_id or filename:
        return (repo_id, filename) in _CURATED.values()
    # Static rules resolve these exact short names through hub.CURATED_INDEX.
    name = candidate.get("name")
    return isinstance(name, str) and name in _CURATED


def classify(candidate: dict) -> ModelLabels:
    tags = set()
    for key in ("tags", "capabilities"):
        values = candidate.get(key)
        if isinstance(values, list):
            tags.update(_label(value) for value in values[:64])
    task = _label(candidate.get("pipeline_tag"))
    declared_type = _label(candidate.get("model_type"))
    type_hints = tags | {task, declared_type}
    vision = bool(type_hints & _VLM_TASKS)
    embedding = bool(type_hints & _EMBEDDING_TASKS)
    model_type = "Unknown"
    if vision and embedding:
        pass  # Conflicting specialized types need better metadata.
    elif vision:
        model_type = "VLM"
    elif embedding:
        model_type = "Embedding"
    elif type_hints & _TEXT_TASKS:
        model_type = "LLM"
    elif task in set().union(*_USE_CASES.values()):
        model_type = "LLM"
    origin = candidate.get("metadata_origin")
    if origin not in {"Provider metadata cache", "Provider metadata cache (stale)"}:
        origin = "Catalog metadata"
    type_source = origin if model_type != "Unknown" else "Unknown"

    use_case = "—"
    if not embedding:
        declared_use = _label(candidate.get("use_case"))
        for label, aliases in _USE_CASES.items():
            if declared_use in aliases:
                use_case = label
                break
        if use_case == "—":
            for label, aliases in _USE_CASES.items():
                if (tags | {task}) & aliases:
                    use_case = label
                    break
        if use_case == "—" and (tags | {task}) & _TEXT_TASKS:
            use_case = "General"
    use_case_source = origin if use_case != "—" else "Unknown"

    if not type_hints - {""} and _curated(candidate):
        model_type, type_source = "LLM", "Curated model card"
        if use_case == "—":
            use_case, use_case_source = "General", "Curated model card"

    features = ("Tool use",) if tags & _TOOL_TASKS else ()
    return ModelLabels(model_type, use_case, type_source, use_case_source, features)
