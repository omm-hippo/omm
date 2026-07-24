"""Shared types for model-hub providers (HuggingFace, ModelScope, ...)."""

from __future__ import annotations


class ModelResolutionError(Exception):
    pass


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
