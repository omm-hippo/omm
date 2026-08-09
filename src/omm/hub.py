"""Resolve a model name into a downloadable URL + filename.

Accepts these forms for `omm install <model_name>`:
  1. A curated short name (see CURATED_INDEX below), e.g. "tinyllama-1.1b-q4"
  2. A direct https:// URL to a .gguf file
  3. An explicit provider ref: "hf:org/repo:file.gguf", "ms:org/repo:file.gguf"
  4. A bare "org/repo" (no filename) - tried against every known provider;
     resolves automatically if only one provider has it. A bare
     "org/repo:filename" (filename already known, no prefix) always
     resolves against HuggingFace with zero network calls, matching
     pre-multi-provider behavior - use an explicit "ms:" prefix to install
     a fully-specified ModelScope file.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from omm.featurize import is_mmproj_filename, parse_param_count_billions, parse_quant_bits
from omm.providers.base import AmbiguousModelError, AmbiguousProviderError, ModelResolutionError

# Small curated index of popular GGUF models. Not exhaustive - `omm search`
# and `omm recommend` pull from a larger hosted candidate list instead.
CURATED_INDEX: dict[str, tuple[str, str]] = {
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

from omm.providers import huggingface, modelscope

_PROVIDER_MODULES: dict[str, object] = {
    "huggingface": huggingface,
    "modelscope": modelscope,
}


@dataclass
class ResolvedModel:
    url: str
    filename: str
    repo_id: str | None  # None when installed from a direct URL (no known repo)
    provider: str | None = None  # None when the source provider is unknown


@dataclass
class QuantVariant:
    filename: str
    quant_bits: float | None
    required_gb: float | None  # None when quant/param count couldn't be parsed
    fits: bool | None  # None when required_gb couldn't be estimated


_RAM_OVERHEAD_FACTOR = 1.2  # context/runtime slack on top of raw weight size


def rank_quant_variants(
    candidates: list[str], available_gb: float, param_count_b: float | None = None
) -> list[QuantVariant]:
    """Rank a repo's .gguf files by hardware fit, best-fitting-and-highest-
    quality first, so the CLI can default the picker's cursor there."""
    variants = []
    for filename in candidates:
        quant_bits = parse_quant_bits(filename)
        param_b = parse_param_count_billions(filename) or param_count_b
        if quant_bits is not None and param_b is not None:
            required_gb = param_b * quant_bits / 8 * _RAM_OVERHEAD_FACTOR
            fits = required_gb <= available_gb
        else:
            required_gb = None
            fits = None
        variants.append(QuantVariant(filename, quant_bits, required_gb, fits))

    variants.sort(key=lambda v: (v.fits is not True, -(v.quant_bits or 0)))
    return variants


def best_filenames_by_tier(
    variants: list[QuantVariant], predicted_speed: dict[str, float]
) -> set[str]:
    """Fastest filename per quant_bits tier, using only the filenames the
    caller already resolved a predicted speed for."""
    best_for_tier: dict[float, tuple[str, float]] = {}
    for variant in variants:
        if variant.quant_bits is None:
            continue
        speed = predicted_speed.get(variant.filename)
        if speed is None:
            continue
        current = best_for_tier.get(variant.quant_bits)
        if current is None or speed > current[1]:
            best_for_tier[variant.quant_bits] = (variant.filename, speed)
    return {filename for filename, _ in best_for_tier.values()}


def download_url(provider: str, repo_id: str, filename: str) -> str:
    return _PROVIDER_MODULES[provider].download_url(repo_id, filename)


def fetch_repo_files(provider: str, repo_id: str) -> tuple[list[str], float | None]:
    return _PROVIDER_MODULES[provider].fetch_repo_files(repo_id)


def remote_file_size(provider: str, repo_id: str, filename: str) -> int | None:
    return _PROVIDER_MODULES[provider].remote_file_size(repo_id, filename)


def remote_file_sha256(provider: str, repo_id: str, filename: str) -> str | None:
    return _PROVIDER_MODULES[provider].remote_file_sha256(repo_id, filename)


def fetch_repo_param_count_b(provider: str, repo_id: str) -> float | None:
    return _PROVIDER_MODULES[provider].fetch_repo_param_count_b(repo_id)


def _resolve_repo_ref(provider: str, repo_id: str, filename: str | None) -> ResolvedModel:
    """Shared org/repo[:filename] resolution logic for a single provider -
    filename given -> just build the URL; filename omitted -> list the
    repo's .gguf files and either pick the lone candidate or raise
    AmbiguousModelError."""
    module = _PROVIDER_MODULES[provider]
    if filename is not None:
        if not filename.lower().endswith(".gguf"):
            filename = f"{filename}.gguf"
        url = module.download_url(repo_id, filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id, provider=provider)

    candidates, param_count_b = module.fetch_repo_files(repo_id)
    if not candidates:
        raise ModelResolutionError(f"No .gguf files found in {provider} repo '{repo_id}'.")
    model_candidates = [c for c in candidates if not is_mmproj_filename(c)]
    if not model_candidates:
        raise ModelResolutionError(
            f"{provider} repo '{repo_id}' only contains a multimodal projector "
            "(mmproj) file, not a standalone model GGUF - nothing to install."
        )
    if len(model_candidates) > 1:
        raise AmbiguousModelError(repo_id, model_candidates, param_count_b, provider=provider)
    filename = model_candidates[0]
    url = module.download_url(repo_id, filename)
    return ResolvedModel(url=url, filename=filename, repo_id=repo_id, provider=provider)


_URL_HOST_PROVIDER = {
    "huggingface.co": "huggingface",
}

_PREFIXES = {
    "hf": "huggingface",
    "huggingface": "huggingface",
    "ms": "modelscope",
    "modelscope": "modelscope",
}


def resolve_model(model_name: str) -> ResolvedModel:
    if model_name in CURATED_INDEX:
        repo_id, filename = CURATED_INDEX[model_name]
        url = huggingface.download_url(repo_id, filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id, provider="huggingface")

    if model_name.startswith("http://") or model_name.startswith("https://"):
        filename = model_name.rsplit("/", 1)[-1].split("?", 1)[0]
        host = urlparse(model_name).hostname or ""
        provider = _URL_HOST_PROVIDER.get(host.removeprefix("www."))
        return ResolvedModel(url=model_name, filename=filename, repo_id=None, provider=provider)

    if ":" in model_name:
        prefix, rest = model_name.split(":", 1)
        provider = _PREFIXES.get(prefix.lower())
        if provider is not None:
            if ":" in rest:
                repo_id, filename = rest.split(":", 1)
            else:
                repo_id, filename = rest, None
            return _resolve_repo_ref(provider, repo_id, filename)

    if "/" in model_name:
        if ":" in model_name:
            repo_id, filename = model_name.split(":", 1)
        else:
            repo_id, filename = model_name, None
        if filename is not None:
            # No provider prefix but the filename is already known - preserve
            # the pre-multi-provider behavior exactly (zero network calls,
            # HuggingFace's specific error messages via _resolve_repo_ref).
            # Installing a fully-specified ModelScope file without
            # disambiguation needs an explicit "ms:org/repo:file.gguf" prefix.
            return _resolve_repo_ref("huggingface", repo_id, filename)

        matches: list[str] = []
        for provider in _PROVIDER_MODULES:
            try:
                candidates, _ = _PROVIDER_MODULES[provider].fetch_repo_files(repo_id)
            except ModelResolutionError:
                continue
            if candidates:
                matches.append(provider)
        if len(matches) > 1:
            raise AmbiguousProviderError(repo_id, matches)
        if len(matches) == 1:
            return _resolve_repo_ref(matches[0], repo_id, None)
        raise ModelResolutionError(
            f"'{repo_id}' was not found on HuggingFace or ModelScope."
        )

    raise ModelResolutionError(
        f"Unknown model '{model_name}'. Use a curated name "
        f"({', '.join(CURATED_INDEX)}), an 'org/repo:file.gguf' ref (optionally "
        "prefixed 'hf:' or 'ms:'), or a direct URL."
    )
