"""Shared types for model-hub providers (HuggingFace, ModelScope, ...)."""

from __future__ import annotations

import math


class ModelResolutionError(Exception):
    """`fix`, when set, is a copy-pasteable next step for the CLI's
    cause+fix error format (issue #191) - callers that don't have one
    just omit it and get the old single-message behavior."""

    def __init__(self, message: str, *, fix: str | None = None):
        self.fix = fix
        super().__init__(message)


class AmbiguousModelError(ModelResolutionError):
    """Raised when a repo resolves to more than one .gguf file, so the
    caller can offer a quantization-level choice instead of just failing
    (see hub.rank_quant_variants)."""

    def __init__(
        self,
        repo_id: str,
        candidates: list[str],
        param_count_b: float | None = None,
        provider: str = "huggingface",
    ):
        self.repo_id = repo_id
        self.candidates = candidates
        self.param_count_b = param_count_b
        self.provider = provider
        super().__init__(
            f"Repo '{repo_id}' has multiple .gguf files, specify one: "
            f"{repo_id}:<filename>\nOptions: {', '.join(candidates)}"
        )


class AmbiguousProviderError(ModelResolutionError):
    """Raised when a bare `org/repo` (no provider prefix) matches a repo on
    more than one provider, so the caller can ask which one instead of
    silently picking one."""

    def __init__(self, repo_id: str, providers: list[str]):
        self.repo_id = repo_id
        self.providers = providers
        super().__init__(
            f"'{repo_id}' exists on more than one provider: {', '.join(providers)}. "
            f"Specify one, e.g. {providers[0]}:{repo_id}"
        )


def coerce_count(value: object) -> int | None:
    """A whole-number metadata field (downloads, likes, context length), or
    None when the provider sent something that isn't one. `bool` is not a
    count even though Python says it is an int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def first_str(value: object) -> str | None:
    """Providers spell single-valued card fields (base_model, language) as
    either a string or a one-element list - normalize both to a string."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def prune_metadata(fields: dict) -> dict:
    """Drop the keys this provider had no value for, so callers render only
    rows that are actually known instead of a column of "unknown"."""
    return {key: value for key, value in fields.items() if value not in (None, "", [], {})}
