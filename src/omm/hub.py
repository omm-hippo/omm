"""Resolve a model name into a downloadable URL + filename.

Accepts these forms for `omm install <model_name>`:
  1. A curated short name (see CURATED_INDEX below), e.g. "tinyllama-1.1b-q4"
  2. A direct https:// URL to a .gguf file
  3. An explicit provider ref: "hf:org/repo:file.gguf", "ms:org/repo:file.gguf"
  4. A provider web-page URL pasted straight out of a browser, e.g.
     "https://huggingface.co/org/repo", ".../tree/main", ".../blob/main/x.gguf",
     "hf.co/org/repo" or "https://modelscope.cn/models/org/repo" - normalized
     into form 3 by `parse_model_ref` before anything else looks at it.
  5. A bare "org/repo" (no filename) - tried against every known provider;
     resolves automatically if only one provider has it. A bare
     "org/repo:filename" (filename already known, no prefix) always
     resolves against HuggingFace with zero network calls, matching
     pre-multi-provider behavior - use an explicit "ms:" prefix to install
     a fully-specified ModelScope file.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
import re
import struct
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

from omm.featurize import (
    is_mmproj_filename,
    is_shard_filename,
    parse_param_count_billions,
    parse_quant_bits,
)
from omm.gguf import read_gguf_metadata_bytes
from omm.providers.base import (
    AmbiguousModelError,
    AmbiguousProviderError,
    ModelResolutionError,
    NoGgufFilesError,
)

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

_PROVIDER_LABELS: dict[str, str] = {
    "huggingface": "HuggingFace",
    "modelscope": "ModelScope",
}


@dataclass
class ResolvedModel:
    url: str
    filename: str
    repo_id: str | None  # None when installed from a direct URL (no known repo)
    provider: str | None = None  # None when the source provider is unknown
    expected_sha256: str | None = None
    expected_size_bytes: int | None = None
    source_metadata_checked: bool = False
    # Set when resolution fell back to a single provider because another
    # provider could not be checked (outage) rather than confirmed absent -
    # the CLI surfaces this so the fallback isn't silent. None otherwise.
    note: str | None = None


@dataclass
class QuantVariant:
    filename: str
    quant_bits: float | None
    required_gb: float | None  # None when quant/param count couldn't be parsed
    fits: bool | None  # None when required_gb couldn't be estimated


_RAM_OVERHEAD_FACTOR = 1.2  # context/runtime slack on top of raw weight size
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def validate_provider(provider: str) -> str:
    if not isinstance(provider, str) or provider not in _PROVIDER_MODULES:
        raise ModelResolutionError(f"unsupported model provider: {provider!r}")
    return provider


def validate_repo_id(repo_id: str) -> str:
    """Validate the provider repository id before it reaches URL or link paths.

    Both supported providers use exactly ``owner/repository``. Keeping that
    contract here prevents a provider response or copied command from smuggling
    ``..`` or Windows separators into LM Studio's directory layout.
    """
    if (
        not isinstance(repo_id, str)
        or len(repo_id) > 300
        or "\\" in repo_id
        or any(ord(character) < 32 for character in repo_id)
    ):
        raise ModelResolutionError("model repository id contains unsafe path characters")
    parts = repo_id.split("/")
    if len(parts) != 2 or any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) is None
        for part in parts
    ):
        raise ModelResolutionError(
            "model repository id must use the safe 'owner/repository' form"
        )
    return repo_id


def validate_model_filename(filename: str) -> str:
    """Allow a relative provider path but never a path that escapes the hub."""
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 500
        or unicodedata.normalize("NFC", filename) != filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise ModelResolutionError("model filename contains unsafe path characters")
    path = PurePosixPath(filename)
    if (
        path.is_absolute()
        or filename != path.as_posix()
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
        or any(
            part.endswith((" ", "."))
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
            for part in path.parts
        )
        or not filename.lower().endswith(".gguf")
    ):
        raise ModelResolutionError(
            "model filename must be a safe relative path ending in .gguf"
        )
    return filename


def model_filename_identity(filename: str) -> str:
    """Portable identity for registry paths on case-insensitive filesystems."""
    return validate_model_filename(filename).casefold()


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
    return _PROVIDER_MODULES[validate_provider(provider)].download_url(repo_id, filename)


def fetch_repo_files(provider: str, repo_id: str) -> tuple[list[str], float | None]:
    return _PROVIDER_MODULES[validate_provider(provider)].fetch_repo_files(repo_id)


def fetch_repo_metadata(provider: str, repo_id: str) -> dict:
    """Best-effort repo-level facts (author, downloads, likes, license, ...)
    for a model that is not installed. Providers return {} rather than
    raising, so callers can render whatever came back."""
    return _PROVIDER_MODULES[validate_provider(provider)].fetch_repo_metadata(repo_id)


def remote_file_size(provider: str, repo_id: str, filename: str) -> int | None:
    return _PROVIDER_MODULES[validate_provider(provider)].remote_file_size(repo_id, filename)


def remote_file_sha256(provider: str, repo_id: str, filename: str) -> str | None:
    return _PROVIDER_MODULES[validate_provider(provider)].remote_file_sha256(repo_id, filename)


# A prefix may be as large as 64 MiB. Keeping 128 of them could retain 8 GiB
# in a long-running contribute process; a small cache still covers the normal
# reconsider-the-same-candidate path without competing with model memory.
@lru_cache(maxsize=4)
def _remote_gguf_prefix_cached(
    provider: str,
    repo_id: str,
    filename: str,
    max_prefix_bytes: int,
) -> bytes | None:
    """Read a bounded GGUF header prefix without downloading tensor data.

    Providers are permitted to ignore Range.  ``stream=True`` plus the
    explicit byte limit makes that case safe: the response is closed as soon
    as the bounded prefix has been read rather than buffering the whole model.
    """
    import requests

    from omm import auth

    url = download_url(provider, repo_id, filename)
    headers = {"Range": f"bytes=0-{max_prefix_bytes - 1}", **auth.headers_for_url(url)}
    with requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=(10, 30),
    ) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            remaining = max_prefix_bytes - len(data)
            data.extend(chunk[:remaining])
            if len(data) >= max_prefix_bytes:
                break

    return bytes(data) or None


def _remote_gguf_prefix(provider: str, repo_id: str, filename: str, max_prefix_bytes: int) -> bytes | None:
    """Cache only successes: lru_cache never memoizes a raised exception, so a
    one-off timeout or 5xx is retried on the next call instead of sticking for
    the life of the cache slot.
    """
    import requests

    try:
        return _remote_gguf_prefix_cached(provider, repo_id, filename, max_prefix_bytes)
    except requests.RequestException:
        return None


def remote_gguf_metadata(
    provider: str,
    repo_id: str,
    filename: str,
    wanted_keys: set[str],
    *,
    max_prefix_bytes: int = 16 * 1024**2,
) -> dict[str, object] | None:
    """Best-effort typed metadata from a remote GGUF's bounded header.

    The result is cached for the process lifetime because contribute may
    defer and reconsider the same candidate several times.
    """
    provider = validate_provider(provider)
    repo_id = validate_repo_id(repo_id)
    filename = validate_model_filename(filename)
    if not wanted_keys or max_prefix_bytes < 24 or max_prefix_bytes > 64 * 1024**2:
        return None
    prefix = _remote_gguf_prefix(provider, repo_id, filename, max_prefix_bytes)
    if prefix is None:
        return None
    try:
        metadata = read_gguf_metadata_bytes(prefix, wanted_keys)
    except (struct.error, KeyError, TypeError, ValueError):
        return None
    return metadata or None


def fetch_repo_param_count_b(provider: str, repo_id: str) -> float | None:
    return _PROVIDER_MODULES[validate_provider(provider)].fetch_repo_param_count_b(repo_id)


def _resolve_repo_ref(provider: str, repo_id: str, filename: str | None) -> ResolvedModel:
    """Shared org/repo[:filename] resolution logic for a single provider -
    filename given -> just build the URL; filename omitted -> list the
    repo's .gguf files and either pick the lone candidate or raise
    AmbiguousModelError."""
    module = _PROVIDER_MODULES[validate_provider(provider)]
    repo_id = validate_repo_id(repo_id)
    if filename is not None:
        if not filename.lower().endswith(".gguf"):
            filename = f"{filename}.gguf"
        filename = validate_model_filename(filename)
        if is_shard_filename(filename):
            raise ModelResolutionError(
                f"'{filename}' is one part of a split (multi-part) GGUF - "
                "installing a single part does not give a usable model.",
                fix="Split GGUF models are not supported yet; pick a single-file quant from the repo.",
            )
        url = module.download_url(repo_id, filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id, provider=provider)

    candidates, param_count_b = module.fetch_repo_files(repo_id)
    if not candidates:
        raise _no_gguf_error(provider, repo_id)
    model_candidates: list[str] = []
    seen_candidates: set[str] = set()
    had_shards = False
    unsafe_names = 0
    for candidate in candidates:
        try:
            validated = validate_model_filename(candidate)
        except ModelResolutionError:
            # 이 후보를 못 쓴다는 뜻이지 이 레포를 해석할 수 없다는 뜻이 아니다.
            unsafe_names += 1
            continue
        identity = validated.casefold()
        if is_mmproj_filename(validated) or is_shard_filename(validated) or identity in seen_candidates:
            if is_shard_filename(validated):
                had_shards = True
            continue
        seen_candidates.add(identity)
        model_candidates.append(validated)
    if not model_candidates:
        if had_shards:
            raise ModelResolutionError(
                f"{provider} repo '{repo_id}' only contains split (multi-part) GGUF "
                "files, which omm cannot install yet.",
                fix="Choose a repo that ships single-file quants.",
            )
        if unsafe_names:
            raise ModelResolutionError(
                f"{provider} repo '{repo_id}' has no installable .gguf file: "
                f"{unsafe_names} file name(s) were rejected as unsafe.",
                fix="Install a specific file instead: omm install <owner>/<repo>:<file>.gguf",
            )
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
    "hf.co": "huggingface",
    "modelscope.cn": "modelscope",
}

_PREFIXES = {
    "hf": "huggingface",
    "huggingface": "huggingface",
    "ms": "modelscope",
    "modelscope": "modelscope",
}

# The short prefix each provider's canonical ref is written with.
_REF_PREFIXES = {"huggingface": "hf", "modelscope": "ms"}

# HuggingFace namespaces things that are not models under a leading path
# segment - a dataset URL must not be read as the model "datasets/squad".
_HF_NON_MODEL_ROOTS = {
    "datasets",
    "spaces",
    "collections",
    "organizations",
    "settings",
    "docs",
    "blog",
    "papers",
}
# ".../<marker>/<revision>/<path...>" - where a provider's web UI puts a file.
_HF_FILE_MARKERS = {"blob", "resolve", "raw"}


@dataclass(frozen=True)
class ModelRef:
    """A model reference normalized to omm's canonical ref form.

    `text` is what the rest of the resolver works on: either an untouched
    passthrough (curated id, `org/repo`, direct URL) or a provider web-page
    URL rewritten as `hf:owner/repo[:file.gguf]` / `ms:owner/repo[:file.gguf]`.
    """

    text: str
    provider: str | None = None
    repo_id: str | None = None
    filename: str | None = None

    @property
    def search_text(self) -> str:
        """What to type into a keyword search for this reference - the repo
        name, so `omm search <pasted url>` searches for the model rather than
        for the literal URL."""
        return self.repo_id.split("/")[-1] if self.repo_id else self.text


def _split_hub_page_url(text: str) -> tuple[str, str, str | None] | None:
    """`(provider, repo_id, filename|None)` for a provider web-page URL, else
    None so the caller passes the text through untouched."""
    lowered = text.lower()
    if lowered.startswith(("https://", "http://")):
        url = text
    elif any(
        lowered.startswith(f"{host}/") or lowered.startswith(f"www.{host}/")
        for host in _URL_HOST_PROVIDER
    ):
        url = f"https://{text}"
    else:
        return None

    parsed = urlparse(url)
    provider = _URL_HOST_PROVIDER.get((parsed.hostname or "").lower().removeprefix("www."))
    if provider is None:
        return None
    # A `#sha256=` fragment means the user meant this as a pinned direct
    # download, digest and all - leave that to resolve_model's URL branch
    # instead of silently dropping the pin on the way to a repo ref.
    if "sha256" in parse_qs(parsed.fragment):
        return None

    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if provider == "huggingface":
        return _split_huggingface_path(segments)
    return _split_modelscope_path(segments)


def _split_huggingface_path(segments: list[str]) -> tuple[str, str, str | None] | None:
    if segments[:2] == ["api", "models"]:
        segments = segments[2:]  # someone pasted the REST URL rather than the page
    if len(segments) < 2 or segments[0] in _HF_NON_MODEL_ROOTS:
        return None
    rest = segments[2:]
    filename = None
    if len(rest) >= 3 and rest[0] in _HF_FILE_MARKERS:
        filename = "/".join(rest[2:])
    return "huggingface", f"{segments[0]}/{segments[1]}", filename


def _split_modelscope_path(segments: list[str]) -> tuple[str, str, str | None] | None:
    if segments[:1] == ["api"]:
        return None  # /api/v1/models/... is already a direct download URL
    if segments[:1] == ["models"]:
        segments = segments[1:]
    if len(segments) < 2:
        return None
    rest = segments[2:]
    filename = None
    if len(rest) >= 3 and rest[0] == "resolve":
        filename = "/".join(rest[2:])
    elif len(rest) >= 4 and rest[:2] == ["file", "view"]:
        filename = "/".join(rest[3:])
    return "modelscope", f"{segments[0]}/{segments[1]}", filename


def parse_model_ref(text: str) -> ModelRef:
    """Normalize whatever the user typed into one canonical model reference.

    Every command that takes a model name goes through here (via
    `resolve_model`), so a URL copied out of a HuggingFace or ModelScope page
    works for `install`, `fit`, `info` and `search` alike instead of each
    command growing its own URL special case (issue #340).
    """
    if not isinstance(text, str):
        raise ModelResolutionError("model reference must be text")
    candidate = text.strip()
    if not candidate:
        raise ModelResolutionError("model reference is empty")

    split = _split_hub_page_url(candidate)
    if split is None:
        return ModelRef(text=candidate)
    provider, repo_id, filename = split
    repo_id = validate_repo_id(repo_id)
    prefix = _REF_PREFIXES[provider]
    # A page URL can point at any file in the repo (README.md, config.json).
    # Only a .gguf names the model to install; anything else still identifies
    # the repo, which is the more useful answer than a hard failure.
    if filename and filename.lower().endswith(".gguf"):
        filename = validate_model_filename(filename)
        return ModelRef(f"{prefix}:{repo_id}:{filename}", provider, repo_id, filename)
    return ModelRef(f"{prefix}:{repo_id}", provider, repo_id, None)


def gguf_quantization_refs(provider: str, repo_id: str, limit: int = 3) -> list[str]:
    """Installable refs for GGUF re-uploads of `repo_id`, best-effort.

    Only HuggingFace publishes a model tree, so every other provider answers
    with an empty list rather than a failure."""
    fetch = getattr(_PROVIDER_MODULES.get(provider), "fetch_gguf_quantizations", None)
    if fetch is None:
        return []
    refs: list[str] = []
    for candidate in fetch(repo_id, limit) or []:
        try:
            refs.append(validate_repo_id(candidate))
        except ModelResolutionError:
            continue
    return refs[:limit]


def _no_gguf_error(provider: str, repo_id: str) -> NoGgufFilesError:
    return NoGgufFilesError(
        repo_id,
        provider,
        provider_label=_PROVIDER_LABELS.get(provider, provider),
        suggestions=gguf_quantization_refs(provider, repo_id),
    )


def resolve_model(model_name: str) -> ResolvedModel:
    # One normalizer, applied before any of the branches below look at the
    # text, so a pasted provider URL behaves exactly like the ref it names.
    model_name = parse_model_ref(model_name).text

    if model_name in CURATED_INDEX:
        repo_id, filename = CURATED_INDEX[model_name]
        repo_id = validate_repo_id(repo_id)
        filename = validate_model_filename(filename)
        url = huggingface.download_url(repo_id, filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id, provider="huggingface")

    if model_name.startswith("http://"):
        raise ModelResolutionError("direct model URLs must use HTTPS")
    if model_name.startswith("https://"):
        parsed = urlparse(model_name)
        host = parsed.hostname or ""
        provider = _URL_HOST_PROVIDER.get(host.removeprefix("www."))
        query_filename = parse_qs(parsed.query).get("FilePath", [None])[0]
        filename = validate_model_filename(
            query_filename or parsed.path.rsplit("/", 1)[-1]
        )
        digest_values = parse_qs(parsed.fragment).get("sha256", [])
        if len(digest_values) != 1 or re.fullmatch(r"[0-9a-fA-F]{64}", digest_values[0]) is None:
            raise ModelResolutionError(
                "direct model URLs require a #sha256=<64-hex-digest> fragment",
                fix=(
                    "A host outside HuggingFace/ModelScope publishes no digest omm can "
                    "check, so pin the file yourself: append '#sha256=<digest>' to the "
                    "URL. A HuggingFace or ModelScope page URL needs no digest - paste "
                    "that instead."
                ),
            )
        return ResolvedModel(
            url=parsed._replace(fragment="").geturl(),
            filename=filename,
            repo_id=None,
            provider=provider,
            expected_sha256=digest_values[0].lower(),
        )

    if ":" in model_name:
        prefix, rest = model_name.split(":", 1)
        provider = _PREFIXES.get(prefix.lower())
        if provider is not None:
            if ":" in rest:
                repo_id, filename = rest.split(":", 1)
            else:
                repo_id, filename = rest, None
            return _resolve_repo_ref(provider, repo_id, filename)
        # A valid prefix-less ref is 'owner/repository[:file]', so the text before
        # the first ':' always contains '/'. Without one it can only have been meant
        # as a provider prefix - say so instead of re-splitting the whole string and
        # blaming the repo id.
        if "/" not in prefix:
            raise ModelResolutionError(
                f"unknown provider prefix '{prefix}' in '{model_name}'.",
                fix="Use 'hf:' or 'ms:', or drop the prefix: hf:org/repo:file.gguf",
            )

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

        def _check_provider(provider: str) -> tuple[str, bool, ModelResolutionError | None]:
            try:
                candidates, _ = _PROVIDER_MODULES[provider].fetch_repo_files(repo_id)
            except ModelResolutionError as e:
                return provider, False, e
            return provider, bool(candidates), None

        # Each provider needs its own repo-listing call, and the calls are
        # independent - fan them out instead of paying a sequential round
        # trip per provider (same class of fix as search.py's ModelScope
        # search parallelization).
        with ThreadPoolExecutor(max_workers=len(_PROVIDER_MODULES)) as executor:
            results = list(executor.map(_check_provider, _PROVIDER_MODULES))

        matches = [provider for provider, found, _ in results if found]
        # The provider answered, the repo is there, it just holds no GGUF -
        # a safetensors-only base model. Saying "not found" here sends people
        # hunting for a typo that does not exist (issue #340).
        no_gguf = [provider for provider, found, error in results if not found and error is None]
        # A `kind` of None means the stub/legacy caller didn't classify the
        # failure - treat it as "not found" rather than "unavailable" so old
        # tests/providers keep the pre-existing not-found behavior.
        unavailable = [
            (provider, error)
            for provider, found, error in results
            if not found and error is not None and error.kind == "unavailable"
        ]

        if len(matches) > 1:
            raise AmbiguousProviderError(repo_id, matches)
        if len(matches) == 1:
            resolved = _resolve_repo_ref(matches[0], repo_id, None)
            other_unavailable = [(p, e) for p, e in unavailable if p != matches[0]]
            if other_unavailable:
                provider, error = other_unavailable[0]
                resolved.note = (
                    f"{_PROVIDER_LABELS.get(provider, provider)} could not be checked "
                    f"({error}); using {_PROVIDER_LABELS.get(matches[0], matches[0])}."
                )
            return resolved
        if not matches and no_gguf:
            raise _no_gguf_error(no_gguf[0], repo_id)
        if not matches and unavailable:
            failed_names = ", ".join(_PROVIDER_LABELS.get(p, p) for p, _ in unavailable)
            _, first_error = unavailable[0]
            raise ModelResolutionError(
                f"Could not check {failed_names} for '{repo_id}': {first_error}",
                fix=f"Retry, or name the provider explicitly: hf:{repo_id} or ms:{repo_id}",
            )
        raise ModelResolutionError(
            f"'{repo_id}' was not found on HuggingFace or ModelScope.",
            fix="Check the spelling, or search with `omm search <query>`.",
        )

    raise ModelResolutionError(
        f"Unknown model '{model_name}'.",
        fix=(
            f"Use a curated name ({', '.join(CURATED_INDEX)}), an "
            "'org/repo:file.gguf' ref (optionally prefixed 'hf:' or 'ms:'), "
            "or an HTTPS direct URL ending in #sha256=<64-hex-digest>."
        ),
    )
