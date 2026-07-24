"""Resolve a model name into a downloadable URL + filename.

Accepts these forms for `omm install <model_name>`:
  1. A curated short name (see CURATED_INDEX below), e.g. "tinyllama-1.1b-q4"
  2. A direct https:// URL to a .gguf file
  3. An explicit provider ref: "hf:org/repo:file.gguf", "ms:org/repo:file.gguf"
  4. A bare "org/repo[:filename]" - tried against every known provider;
     resolves automatically if only one provider has it
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlparse

import requests

from omm.featurize import is_mmproj_filename, parse_param_count_billions, parse_quant_bits
from omm.providers.base import AmbiguousModelError, AmbiguousProviderError, ModelResolutionError

HF_API = "https://huggingface.co/api/models/{repo_id}"
HF_DOWNLOAD = "https://huggingface.co/{repo_id}/resolve/main/{filename}"
HF_PATHS_INFO = "https://huggingface.co/api/models/{repo_id}/paths-info/main"

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


@dataclass
class ResolvedModel:
    url: str
    filename: str
    repo_id: str | None  # None when installed from a direct URL (no HF repo)


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
    quality first, so the CLI can default the picker's cursor there.

    `param_count_b` is a repo-level fallback (from HF's own parsed GGUF
    metadata) for filenames that don't spell out a param count, e.g.
    "ID_Legal_Assistant_Q8_0.gguf" - the quant is still parseable per file,
    but nothing in the name says "8B", so without a fallback every variant
    of a repo like that shows "fit unknown" regardless of quant.
    """
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
    """Fastest filename per quant_bits tier, using only the (repo_id,
    filename) pairs the caller already resolved a predicted speed for -
    every other variant (didn't fit, speed unresolvable) is left out of
    consideration entirely rather than guessed at.

    Ties keep whichever filename appears first in `variants` (already
    sorted fits-desc/quant_bits-desc by `rank_quant_variants`), via the
    strict `>` below.
    """
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


def _fetch_repo_gguf_info(repo_id: str) -> tuple[list[str], float | None]:
    """List of .gguf filenames plus a repo-level param count fallback, in
    billions - HF parses this straight out of the GGUF header itself
    (response key "gguf.total") whether or not the filename spells it out,
    so it covers names like "ID_Legal_Assistant_Q8_0.gguf" that carry a
    quant tag but no param count."""
    try:
        resp = requests.get(HF_API.format(repo_id=repo_id), timeout=15)
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            raise ModelResolutionError(
                f"HF repo '{repo_id}' is private or gated - requires an access token."
            ) from e
        if status == 404:
            raise ModelResolutionError(f"HF repo '{repo_id}' not found.") from e
        raise ModelResolutionError(f"HF API request failed for '{repo_id}' ({status}).") from e
    except requests.RequestException as e:
        raise ModelResolutionError(f"Could not reach Hugging Face for '{repo_id}': {e}") from e

    payload = resp.json()
    siblings = payload.get("siblings", [])
    files = [s["rfilename"] for s in siblings if s["rfilename"].endswith(".gguf")]
    param_count_b = _parse_gguf_total_params(payload)
    return files, param_count_b


def _parse_gguf_total_params(payload: dict) -> float | None:
    total_params = payload.get("gguf", {}).get("total")
    return total_params / 1e9 if total_params else None


def fetch_repo_param_count_b(repo_id: str) -> float | None:
    """Best-effort repo-level parameter count (billions), for callers that
    only have a repo id and filename - not the full listing `resolve_model`
    fetches - and whose filename doesn't spell out the count (e.g. repos
    branded like "DeepSeek-V4-Flash" instead of "...-70B"). Unlike
    `_fetch_repo_gguf_info`, this never raises: it's used to decide whether
    to flag a search result as unviable, not to resolve an install, so a
    failed lookup should just leave that decision unmade."""
    try:
        resp = requests.get(HF_API.format(repo_id=repo_id), timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    try:
        return _parse_gguf_total_params(resp.json())
    except ValueError:
        return None


def remote_file_sha256(repo_id: str, filename: str) -> str | None:
    """Current LFS sha256 of `filename` in `repo_id`'s main branch, via HF's
    paths-info API - no need to download the file to check it. Returns None
    if the request fails, the file isn't listed, or it isn't stored as LFS
    (gguf files always are in practice, so this covers "can't verify")."""
    try:
        resp = requests.post(
            HF_PATHS_INFO.format(repo_id=repo_id),
            json={"paths": [filename]},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    entries = resp.json()
    if not entries:
        return None
    return entries[0].get("lfs", {}).get("sha256")


def remote_file_size(repo_id: str, filename: str) -> int | None:
    """Best-effort Hub file size without downloading the GGUF."""
    url = HF_DOWNLOAD.format(repo_id=repo_id, filename=quote(filename, safe="/"))
    try:
        response = requests.head(url, timeout=15, allow_redirects=False)
        response.raise_for_status()
    except requests.RequestException:
        return None
    raw_size = response.headers.get("X-Linked-Size")
    if raw_size is None and response.status_code == 200:
        raw_size = response.headers.get("Content-Length")
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def resolve_model(model_name: str) -> ResolvedModel:
    if model_name in CURATED_INDEX:
        repo_id, filename = CURATED_INDEX[model_name]
        url = HF_DOWNLOAD.format(repo_id=repo_id, filename=filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id)

    if model_name.startswith("http://") or model_name.startswith("https://"):
        filename = model_name.rsplit("/", 1)[-1].split("?", 1)[0]
        return ResolvedModel(url=model_name, filename=filename, repo_id=None)

    if "/" in model_name:
        if ":" in model_name:
            repo_id, filename = model_name.split(":", 1)
            if not filename.lower().endswith(".gguf"):
                filename = f"{filename}.gguf"
        else:
            repo_id, filename = model_name, None
            candidates, param_count_b = _fetch_repo_gguf_info(repo_id)
            if not candidates:
                raise ModelResolutionError(f"No .gguf files found in HF repo '{repo_id}'.")
            # mmproj files aren't standalone models - excluded here so a repo
            # that ships one alongside the real model doesn't get the mmproj
            # auto-selected (single-candidate shortcut) or offered in the
            # quant picker (ambiguous case) as if it were a quant choice.
            model_candidates = [c for c in candidates if not is_mmproj_filename(c)]
            if not model_candidates:
                raise ModelResolutionError(
                    f"HF repo '{repo_id}' only contains a multimodal projector "
                    "(mmproj) file, not a standalone model GGUF - nothing to install."
                )
            if len(model_candidates) > 1:
                raise AmbiguousModelError(repo_id, model_candidates, param_count_b)
            filename = model_candidates[0]
        url = HF_DOWNLOAD.format(repo_id=repo_id, filename=filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id)

    raise ModelResolutionError(
        f"Unknown model '{model_name}'. Use a curated name "
        f"({', '.join(CURATED_INDEX)}), an 'org/repo:file.gguf' ref, or a direct URL."
    )
