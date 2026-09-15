"""Choose a varied shortlist from already hardware-ranked candidates.

These are presentation heuristics, not model-quality or provenance scores.
The input order retains the caller's memory/speed preference within each
family. No candidate outside the caller's viable pool can enter the list.
"""

from __future__ import annotations

import re

_QUANT_SUFFIX = re.compile(
    r"(?i)[-_.](?P<quant>(?:UD[-_.])?"
    r"(?:I?Q[1-8](?:[-_.](?:[0-9]+|XXS|XS|NL|K|S|M|L))*"
    r"|BF16|FP16|F16|FP32|F32))$"
)


def _package_stem(value: str) -> str:
    value = value.rsplit("/", 1)[-1]
    value = re.sub(r"(?:[.]gguf|[-_.]gguf)$", "", value, flags=re.IGNORECASE)
    return re.sub(r"-\d{5}-of-\d{5}$", "", value, flags=re.IGNORECASE)


def quantization_label(candidate: dict) -> str:
    """Report the named package format without guessing it from model size."""
    for field in ("filename", "repo_id", "name"):
        match = _QUANT_SUFFIX.search(_package_stem(str(candidate.get(field) or "")))
        if match:
            return match.group("quant").upper()
    return "Unknown"


def model_label(value: str) -> str:
    """Strip packaging suffixes, retaining model versions and role variants."""
    value = _QUANT_SUFFIX.sub("", _package_stem(value))
    value = re.sub(r"[-_]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def variant_warning(candidate: dict) -> str | None:
    # Uploader names are not model capabilities (e.g. ad/Qwen3-8B).
    coordinates = [candidate.get(key) for key in ("repo_id", "filename")]
    text = " ".join(
        str(value).rsplit("/", 1)[-1]
        for value in coordinates if value
    ).lower() or str(candidate.get("name") or "").lower()
    if any(word in text for word in ("abliterated", "heretic", "nsfw", "uncensored")):
        return (
            "Specialized or uncensored variant. Review its model card and behavior "
            "before installing."
        )
    if re.search(r"(?:^|[^a-z0-9])(?:mtp|ad|dflash\d*|eagle\d*|draft|drafter)(?:$|[^a-z0-9])", text):
        return (
            "Specialized decoding variant. Review its model card and runner "
            "requirements before installing."
        )
    return None


def _identity(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+(?:\.\d+)*|[a-z]+", model_label(value).lower()))


def _model_key(candidate: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # Require both the repository model name and the filename to agree.
    # This merges uploader/quant mirrors without collapsing a fine-tune whose
    # only distinguishing marker is in its repo, or generic model.gguf files.
    repo = str(candidate.get("repo_id") or candidate.get("name") or "")
    filename = str(candidate.get("filename") or repo)
    return _identity(repo or filename), _identity(filename)


def _family(candidate: dict) -> tuple[str, ...]:
    identity = _model_key(candidate)[0]
    if identity[:2] == ("meta", "llama"):
        return ("llama",)
    if identity[:1] == ("codeqwen",):
        return ("qwen",)
    if identity[:2] == ("gpt", "oss"):
        return ("gpt", "oss")
    if identity and identity[0] in {
        "qwen", "llama", "tinyllama", "mistral", "mixtral", "gemma", "phi",
        "deepseek", "lfm", "ornith", "glm", "bonsai",
    }:
        return identity[:1]
    # Unrecognized models are not all one "Other" family.
    return identity


def shortlist(
    ranked: list[tuple[dict, float]], *, limit: int = 10
) -> list[tuple[dict, float]]:
    """Dedupe before truncation, initially admitting two models per family.

    Normal candidates come first. Specialized variants remain available if
    there is room after the normal candidates, with warnings in the UI/JSON.
    Excess family members fill any remaining slots. Stable input order keeps
    the hardware ranking within each pass; this does not assert uploader trust.
    """
    if limit <= 0:
        return []
    result = []
    seen = set()
    for specialized in (False, True):
        family_counts: dict[tuple[str, ...], int] = {}
        preferred = []
        deferred = []
        for candidate, speed in ranked:
            if bool(variant_warning(candidate)) != specialized:
                continue
            key = _model_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            family = _family(candidate)
            count = family_counts.get(family, 0)
            family_counts[family] = count + 1
            (preferred if count < 2 else deferred).append((candidate, speed))
        result.extend((preferred + deferred)[: limit - len(result)])
        if len(result) == limit:
            return result
    return result
