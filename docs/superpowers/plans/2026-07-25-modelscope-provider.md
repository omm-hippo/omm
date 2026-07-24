# ModelScope Provider Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `omm search`/`install`/`recommend`/`contribute` and the recommendation-model training pipeline resolve, download, and rank models from ModelScope in addition to HuggingFace.

**Architecture:** Extract the existing HuggingFace-only logic in `hub.py` into a `src/omm/providers/` package (`huggingface.py` + a new `modelscope.py`, both implementing the same 4-function interface: `fetch_repo_files`, `download_url`, `remote_file_size`, `remote_file_sha256`). `hub.py` becomes a thin router that dispatches by a `provider` string threaded through `ResolvedModel`, candidate dicts, the registry, and telemetry. Every existing HF-only call site in `cli.py` (5 of them) gets updated to pass the provider through instead of assuming HF.

**Tech Stack:** Python 3.14, `requests`, `typer`, `pytest`, `monkeypatch`-based test doubles (no `responses`/`requests_mock` library in this repo - follow existing local fake-response-class pattern).

## Global Constraints

- Every existing test must keep passing unmodified unless the plan explicitly says to edit it. Run `python -m pytest` after each task.
- No provider prefix is required for HF (100% backward compatible: bare `org/repo`, `org/repo:file.gguf`, curated names, direct URLs all behave exactly as before).
- CivitAI is explicitly out of scope (see `docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md` - CivitAI downloads 401 without an API key, confirmed live).
- All new provider API calls use `timeout=15` like the existing HF calls, and never raise on network failure except through `ModelResolutionError` (matching existing HF error-handling shape).
- ModelScope API field names (`Path`, `Size`, `Sha256` in file listings; `id`, `tags`, `downloads` in search) are taken verbatim from live `curl` responses recorded in the spec - do not guess alternate casings.

---

### Task 1: `providers` package skeleton with shared exception types

**Files:**
- Create: `src/omm/providers/__init__.py`
- Create: `src/omm/providers/base.py`
- Modify: `src/omm/hub.py:1-57` (replace local `ModelResolutionError`/`AmbiguousModelError` definitions with a re-export from `providers.base`, add `AmbiguousProviderError`)
- Test: `tests/test_hub.py`

**Interfaces:**
- Produces: `omm.providers.base.ModelResolutionError`, `omm.providers.base.AmbiguousModelError(repo_id: str, candidates: list[str], param_count_b: float | None = None, provider: str = "huggingface")`, `omm.providers.base.AmbiguousProviderError(repo_id: str, providers: list[str])`. `omm.hub` re-exports all three under the same names (so every existing `from omm.hub import AmbiguousModelError, ModelResolutionError` in `cli.py`/tests keeps working unchanged).

- [ ] **Step 1: Create `src/omm/providers/__init__.py`**

```python
"""Model-hub providers (HuggingFace, ModelScope, ...). Each provider module
implements fetch_repo_files/download_url/remote_file_size/remote_file_sha256
with the same signatures - see providers/base.py for the shared exceptions
and hub.py for the dispatch layer that picks a module by provider name."""
```

- [ ] **Step 2: Create `src/omm/providers/base.py`**

```python
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
```

- [ ] **Step 3: Update `src/omm/hub.py` to import the shared exceptions**

Replace lines 1-57 of `src/omm/hub.py` (the module docstring through the end of the old `AmbiguousModelError` class) with:

```python
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
```

(`ModelResolutionError`/`AmbiguousModelError`/`AmbiguousProviderError` are now imported, not defined here - do not re-declare them. `quote` and `urlparse` are both used later in this file, in Tasks 2 and 4.)

- [ ] **Step 4: Run the existing hub/cli tests to confirm nothing broke yet**

Run: `python -m pytest tests/test_hub.py tests/test_hub_quant_variants.py tests/test_hub_remote_sha256.py tests/test_cli_install_quant_picker.py -v`
Expected: All still FAIL or ERROR at this point only if later parts of `hub.py` (still referencing old code from Task 2/4, not yet written) are broken - if you get `ImportError`/`AttributeError` about `_fetch_repo_gguf_info` or similar missing names, that's expected since Task 2 hasn't moved that code yet. Do not worry about full green here; this step exists to catch typos in Steps 2-3. Skip to Task 2 immediately.

- [ ] **Step 5: Commit**

```bash
git add src/omm/providers/__init__.py src/omm/providers/base.py src/omm/hub.py
git commit -m "refactor: extract shared provider exception types into providers/base.py"
```

---

### Task 2: `providers/huggingface.py` - move existing HF logic, `hub.py` becomes a router

**Files:**
- Create: `src/omm/providers/huggingface.py`
- Modify: `src/omm/hub.py` (replace the body from `class AmbiguousModelError` removal point onward - i.e. everything after the `CURATED_INDEX` dict you just wrote in Task 1 - with the router shown below)
- Test: `tests/test_hub.py`, `tests/test_hub_quant_variants.py`, `tests/test_hub_remote_sha256.py` (must pass unmodified)

**Interfaces:**
- Consumes: `omm.providers.base.{ModelResolutionError, AmbiguousModelError, AmbiguousProviderError}` (Task 1).
- Produces: `omm.providers.huggingface.fetch_repo_files(repo_id) -> tuple[list[str], float | None]`, `.download_url(repo_id, filename) -> str`, `.remote_file_size(repo_id, filename) -> int | None`, `.remote_file_sha256(repo_id, filename) -> str | None`, `.fetch_repo_param_count_b(repo_id) -> float | None`. `omm.hub.resolve_model`, `.rank_quant_variants`, `.best_filenames_by_tier`, `.remote_file_sha256`, `.remote_file_size`, `.fetch_repo_param_count_b`, `.ResolvedModel`, `.QuantVariant` all keep their exact current signatures used by `cli.py` (Task 5 changes some of these call sites, but the public names must still exist since `cli.py`'s import block references them until Task 5 runs).

- [ ] **Step 1: Create `src/omm/providers/huggingface.py`**

```python
"""HuggingFace provider: resolves repo file listings and download URLs via
the public HF Hub REST API. Logic moved verbatim from the old hub.py - see
git history for prior behavior if something looks unfamiliar."""

from __future__ import annotations

from urllib.parse import quote

import requests

from omm.providers.base import ModelResolutionError

HF_API = "https://huggingface.co/api/models/{repo_id}"
HF_DOWNLOAD = "https://huggingface.co/{repo_id}/resolve/main/{filename}"
HF_PATHS_INFO = "https://huggingface.co/api/models/{repo_id}/paths-info/main"


def fetch_repo_files(repo_id: str) -> tuple[list[str], float | None]:
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
    only have a repo id and filename and whose filename doesn't spell out
    the count. Never raises - used to decide whether to flag a search
    result as unviable, not to resolve an install."""
    try:
        resp = requests.get(HF_API.format(repo_id=repo_id), timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    try:
        return _parse_gguf_total_params(resp.json())
    except ValueError:
        return None


def download_url(repo_id: str, filename: str) -> str:
    return HF_DOWNLOAD.format(repo_id=repo_id, filename=filename)


def remote_file_sha256(repo_id: str, filename: str) -> str | None:
    """Current LFS sha256 of `filename` in `repo_id`'s main branch, via HF's
    paths-info API. Returns None if the request fails, the file isn't
    listed, or it isn't stored as LFS."""
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
```

- [ ] **Step 2: Rewrite the rest of `src/omm/hub.py` as a thin router**

Append this to `src/omm/hub.py`, right after the `CURATED_INDEX` block from Task 1 (this replaces everything that used to follow it - the old `ModelResolutionError`/`AmbiguousModelError` classes are already gone from Task 1, and the old `_fetch_repo_gguf_info`/`_parse_gguf_total_params`/`fetch_repo_param_count_b`/`remote_file_sha256`/`remote_file_size` function bodies are now superseded by the provider dispatch below - delete their old definitions from this file entirely, they live in `providers/huggingface.py` now):

```python
from omm.providers import huggingface

_PROVIDER_MODULES: dict[str, object] = {"huggingface": huggingface}


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
            return _resolve_repo_ref(matches[0], repo_id, filename)
        raise ModelResolutionError(
            f"'{repo_id}' was not found on HuggingFace or ModelScope."
        )

    raise ModelResolutionError(
        f"Unknown model '{model_name}'. Use a curated name "
        f"({', '.join(CURATED_INDEX)}), an 'org/repo:file.gguf' ref (optionally "
        "prefixed 'hf:' or 'ms:'), or a direct URL."
    )
```

Note on `_resolve_repo_ref`'s "filename given" branch: the *old* `resolve_model` did **not** append `.gguf` to a bare-`org/repo:filename` ref before this refactor in one place (curated/URL branches) but *did* in the explicit-filename branch (`if not filename.lower().endswith(".gguf"): filename = f"{filename}.gguf"` - this was the original behavior at old `hub.py:234-235`, preserved verbatim above).

- [ ] **Step 2: Run existing hub/quant-picker tests**

Run: `python -m pytest tests/test_hub.py tests/test_hub_quant_variants.py tests/test_hub_remote_sha256.py tests/test_cli_install_quant_picker.py tests/test_cli_install_suggestions.py tests/test_search.py -v`
Expected: `test_hub_remote_sha256.py` will FAIL because it monkeypatches `hub.requests` directly (`monkeypatch.setattr(hub.requests, "post", ...)`) but `requests` is no longer imported in `hub.py` - this is expected and fixed in Step 3.

- [ ] **Step 3: Update `tests/test_hub_remote_sha256.py` to patch the new location**

Open `tests/test_hub_remote_sha256.py`. Every `monkeypatch.setattr(hub.requests, "post", ...)` (or similar direct `hub.requests`/`hub.HF_PATHS_INFO` reference) must become `monkeypatch.setattr(huggingface.requests, "post", ...)` with `from omm.providers import huggingface` added to the imports, and any `hub.remote_file_sha256(repo_id, filename)` call in the test body becomes `hub.remote_file_sha256("huggingface", repo_id, filename)` (the function is now provider-dispatched, first positional arg is the provider name). Apply the same mechanical edit throughout the file - read it first with the Read tool to see every call site before editing, since the exact monkeypatch targets depend on the file's current structure.

- [ ] **Step 4: Run the full test file list again**

Run: `python -m pytest tests/test_hub.py tests/test_hub_quant_variants.py tests/test_hub_remote_sha256.py tests/test_cli_install_quant_picker.py tests/test_cli_install_suggestions.py tests/test_search.py -v`
Expected: PASS. If `test_cli_install_quant_picker.py` or `test_cli_install_suggestions.py` fail on an `AmbiguousModelError(repo_id, candidates)` call missing `provider`, that's fine - the default `provider="huggingface"` from Task 1 should already cover it; if it still fails, read the failure and fix the test call site to match (do not change the production default).

- [ ] **Step 5: Commit**

```bash
git add src/omm/providers/huggingface.py src/omm/hub.py tests/test_hub_remote_sha256.py
git commit -m "refactor: move HF-specific hub.py logic into providers/huggingface.py, make hub.py a provider router"
```

---

### Task 3: `providers/modelscope.py`

**Files:**
- Create: `src/omm/providers/modelscope.py`
- Test: Create `tests/test_provider_modelscope.py`

**Interfaces:**
- Consumes: `omm.providers.base.ModelResolutionError` (Task 1).
- Produces: `omm.providers.modelscope.fetch_repo_files(repo_id) -> tuple[list[str], float | None]`, `.download_url`, `.remote_file_size`, `.remote_file_sha256`, `.fetch_repo_param_count_b` - same 5-function shape as `providers/huggingface.py` (Task 2), consumed by `hub.py`'s `_PROVIDER_MODULES` dict (wired up in Task 4).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_modelscope.py`:

```python
"""Unit tests for the ModelScope provider module. Field names (Path/Size/
Sha256) match live API responses recorded in
docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md - do not
"fix" the casing without re-verifying against the real API."""

from __future__ import annotations

import pytest

from omm.providers import modelscope
from omm.providers.base import ModelResolutionError


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)

    def json(self):
        return self._payload


_FILES_PAYLOAD = {
    "Code": 200,
    "Data": {
        "Files": [
            {"Name": "README.md", "Path": "README.md", "Size": 100, "Sha256": "readme-hash"},
            {
                "Name": "model-q4_k_m.gguf",
                "Path": "model-q4_k_m.gguf",
                "Size": 491400032,
                "Sha256": "abc123",
            },
            {
                "Name": "model-q8_0.gguf",
                "Path": "model-q8_0.gguf",
                "Size": 900000000,
                "Sha256": "def456",
            },
        ]
    },
}


def test_fetch_repo_files_filters_to_gguf_only(monkeypatch):
    monkeypatch.setattr(
        modelscope.requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    files, param_count_b = modelscope.fetch_repo_files("org/repo")
    assert files == ["model-q4_k_m.gguf", "model-q8_0.gguf"]
    assert param_count_b is None


def test_fetch_repo_files_404_raises_model_resolution_error(monkeypatch):
    monkeypatch.setattr(modelscope.requests, "get", lambda *a, **k: _FakeResponse(404, {}))
    with pytest.raises(ModelResolutionError):
        modelscope.fetch_repo_files("org/does-not-exist")


def test_download_url_builds_expected_query_string():
    url = modelscope.download_url("org/repo", "model-q4_k_m.gguf")
    assert url == (
        "https://modelscope.cn/api/v1/models/org/repo/repo"
        "?Revision=master&FilePath=model-q4_k_m.gguf"
    )


def test_remote_file_size_finds_matching_file(monkeypatch):
    monkeypatch.setattr(
        modelscope.requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    assert modelscope.remote_file_size("org/repo", "model-q4_k_m.gguf") == 491400032


def test_remote_file_size_returns_none_for_missing_file(monkeypatch):
    monkeypatch.setattr(
        modelscope.requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    assert modelscope.remote_file_size("org/repo", "does-not-exist.gguf") is None


def test_remote_file_sha256_finds_matching_file(monkeypatch):
    monkeypatch.setattr(
        modelscope.requests, "get", lambda *a, **k: _FakeResponse(200, _FILES_PAYLOAD)
    )
    assert modelscope.remote_file_sha256("org/repo", "model-q8_0.gguf") == "def456"


def test_fetch_repo_param_count_b_is_always_none():
    # ModelScope's file-listing API doesn't expose a parsed GGUF header
    # total-params field like HF's does - always None, never guessed.
    assert modelscope.fetch_repo_param_count_b("org/repo") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_modelscope.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omm.providers.modelscope'`

- [ ] **Step 3: Write `src/omm/providers/modelscope.py`**

```python
"""ModelScope provider: resolves repo file listings and download URLs via
the public ModelScope Hub API (https://modelscope.cn). No auth needed for
public repos - confirmed with live curl requests (see
docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md).

ModelScope's download endpoint honors Range requests but returns HTTP 200
instead of 206 for a partial response (confirmed live) - see
downloader.py's _probe_range_support for the corresponding fix."""

from __future__ import annotations

from urllib.parse import quote_plus

import requests

from omm.providers.base import ModelResolutionError

MS_REPO_FILES = "https://modelscope.cn/api/v1/models/{repo_id}/repo/files"
MS_DOWNLOAD = "https://modelscope.cn/api/v1/models/{repo_id}/repo"


def _list_repo_files(repo_id: str) -> list[dict]:
    try:
        resp = requests.get(
            MS_REPO_FILES.format(repo_id=repo_id),
            params={"Revision": "master", "Recursive": "True"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            raise ModelResolutionError(
                f"ModelScope repo '{repo_id}' is private or gated - requires an access token."
            ) from e
        if status == 404:
            raise ModelResolutionError(f"ModelScope repo '{repo_id}' not found.") from e
        raise ModelResolutionError(
            f"ModelScope API request failed for '{repo_id}' ({status})."
        ) from e
    except requests.RequestException as e:
        raise ModelResolutionError(f"Could not reach ModelScope for '{repo_id}': {e}") from e

    payload = resp.json()
    return payload.get("Data", {}).get("Files", [])


def fetch_repo_files(repo_id: str) -> tuple[list[str], float | None]:
    files = [
        f["Path"]
        for f in _list_repo_files(repo_id)
        if str(f.get("Path", "")).lower().endswith(".gguf")
    ]
    return files, None


def fetch_repo_param_count_b(repo_id: str) -> float | None:
    """ModelScope's file-listing API doesn't expose a parsed GGUF header
    total-params field like HF's does - always None, filename-based
    parsing is the only source for ModelScope repos."""
    return None


def download_url(repo_id: str, filename: str) -> str:
    return (
        f"{MS_DOWNLOAD.format(repo_id=repo_id)}"
        f"?Revision=master&FilePath={quote_plus(filename)}"
    )


def remote_file_size(repo_id: str, filename: str) -> int | None:
    for f in _list_repo_files(repo_id):
        if f.get("Path") == filename:
            size = f.get("Size")
            return int(size) if size else None
    return None


def remote_file_sha256(repo_id: str, filename: str) -> str | None:
    for f in _list_repo_files(repo_id):
        if f.get("Path") == filename:
            sha = f.get("Sha256")
            return sha.lower() if sha else None
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_modelscope.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/providers/modelscope.py tests/test_provider_modelscope.py
git commit -m "feat: add ModelScope provider module (file listing, download URL, size, sha256)"
```

---

### Task 4: Wire ModelScope into `hub.py`'s dispatch tables and `resolve_model`

**Files:**
- Modify: `src/omm/hub.py` (the `_PROVIDER_MODULES`, `_URL_HOST_PROVIDER`, `_PREFIXES` dicts from Task 2)
- Test: Create `tests/test_hub_multi_provider.py`

**Interfaces:**
- Consumes: `omm.providers.modelscope` (Task 3), `omm.hub._resolve_repo_ref`/`resolve_model` (Task 2).
- Produces: `resolve_model("ms:org/repo:file.gguf")`, `resolve_model("ms:org/repo")`, bare `resolve_model("org/repo")` that checks both providers, and `AmbiguousProviderError` when both HF and ModelScope have the same `org/repo`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_multi_provider.py`:

```python
"""Tests for hub.resolve_model's provider dispatch across HuggingFace and
ModelScope: explicit prefixes, and bare org/repo refs that must query both
providers and disambiguate."""

from __future__ import annotations

import pytest

from omm import hub
from omm.providers import huggingface, modelscope
from omm.providers.base import AmbiguousProviderError, ModelResolutionError


def _stub_fetch_repo_files(monkeypatch, module, files_by_repo: dict[str, list[str]]):
    def fake(repo_id):
        if repo_id not in files_by_repo:
            raise ModelResolutionError(f"not found: {repo_id}")
        return files_by_repo[repo_id], None

    monkeypatch.setattr(module, "fetch_repo_files", fake)


def test_explicit_ms_prefix_with_filename_resolves_without_network(monkeypatch):
    resolved = hub.resolve_model("ms:org/repo:model-q4_k_m.gguf")
    assert resolved.provider == "modelscope"
    assert resolved.repo_id == "org/repo"
    assert resolved.filename == "model-q4_k_m.gguf"
    assert resolved.url == (
        "https://modelscope.cn/api/v1/models/org/repo/repo"
        "?Revision=master&FilePath=model-q4_k_m.gguf"
    )


def test_explicit_hf_prefix_still_works(monkeypatch):
    resolved = hub.resolve_model("hf:org/repo:model.gguf")
    assert resolved.provider == "huggingface"
    assert resolved.url == "https://huggingface.co/org/repo/resolve/main/model.gguf"


def test_bare_repo_resolves_to_sole_matching_provider(monkeypatch):
    _stub_fetch_repo_files(monkeypatch, huggingface, {})
    _stub_fetch_repo_files(monkeypatch, modelscope, {"org/only-on-ms": ["model.gguf"]})
    resolved = hub.resolve_model("org/only-on-ms")
    assert resolved.provider == "modelscope"
    assert resolved.filename == "model.gguf"


def test_bare_repo_on_both_providers_raises_ambiguous_provider_error(monkeypatch):
    _stub_fetch_repo_files(monkeypatch, huggingface, {"org/repo": ["model.gguf"]})
    _stub_fetch_repo_files(monkeypatch, modelscope, {"org/repo": ["model.gguf"]})
    with pytest.raises(AmbiguousProviderError) as exc_info:
        hub.resolve_model("org/repo")
    assert set(exc_info.value.providers) == {"huggingface", "modelscope"}


def test_bare_repo_on_neither_provider_raises_model_resolution_error(monkeypatch):
    _stub_fetch_repo_files(monkeypatch, huggingface, {})
    _stub_fetch_repo_files(monkeypatch, modelscope, {})
    with pytest.raises(ModelResolutionError):
        hub.resolve_model("org/nowhere")


def test_url_from_known_modelscope_host_is_tagged(monkeypatch):
    resolved = hub.resolve_model("https://modelscope.cn/api/v1/models/org/repo/repo?FilePath=x.gguf")
    assert resolved.provider is None  # host tagging only covers huggingface.co for now
```

Note the last test documents current behavior (ModelScope host auto-tagging is intentionally *not* added in this task - `_URL_HOST_PROVIDER` still only has `huggingface.co`, matching the existing direct-URL install path which never needed a ModelScope entry since ModelScope URLs weren't reachable before this feature). If Step 3 below adds `modelscope.cn` to `_URL_HOST_PROVIDER`, update this test's assertion to `== "modelscope"` instead - do whichever you implement, but keep the test and the implementation in agreement.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hub_multi_provider.py -v`
Expected: FAIL - `hub.resolve_model("ms:org/repo:model-q4_k_m.gguf")` raises `ModelResolutionError` because `_PROVIDER_MODULES`/`_PREFIXES` don't know `"ms"`/`"modelscope"` yet.

- [ ] **Step 3: Wire ModelScope into `hub.py`'s dispatch dicts**

In `src/omm/hub.py`, change:

```python
from omm.providers import huggingface

_PROVIDER_MODULES: dict[str, object] = {"huggingface": huggingface}
```

to:

```python
from omm.providers import huggingface, modelscope

_PROVIDER_MODULES: dict[str, object] = {
    "huggingface": huggingface,
    "modelscope": modelscope,
}
```

and change:

```python
_URL_HOST_PROVIDER = {
    "huggingface.co": "huggingface",
}

_PREFIXES = {
    "hf": "huggingface",
    "huggingface": "huggingface",
}
```

to:

```python
_URL_HOST_PROVIDER = {
    "huggingface.co": "huggingface",
}

_PREFIXES = {
    "hf": "huggingface",
    "huggingface": "huggingface",
    "ms": "modelscope",
    "modelscope": "modelscope",
}
```

(Leave `_URL_HOST_PROVIDER` without a `modelscope.cn` entry, matching the test written in Step 1 - direct-URL provider tagging for ModelScope is deferred since no code path currently needs it and the design doc doesn't require it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hub_multi_provider.py tests/test_hub.py tests/test_hub_quant_variants.py tests/test_hub_remote_sha256.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/hub.py tests/test_hub_multi_provider.py
git commit -m "feat: dispatch resolve_model across HuggingFace and ModelScope providers"
```

---

### Task 5: `downloader.py` - accept ModelScope's 200-with-Range as parallel-download-eligible

**Files:**
- Modify: `src/omm/downloader.py:75-121`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_probe_range_support(url)` and `_download_range_worker(...)` now also accept a `200` response whose `Content-Length` exactly matches the requested byte range, not just `206`. Public `download_file()` signature is unchanged.

- [ ] **Step 1: Write the failing test**

Read `tests/test_downloader.py` first to see its existing fake-response/monkeypatch style, then add these two tests to it (match the file's existing import style and fake-response class if one already exists there; if it doesn't, add a minimal local one as shown):

```python
def test_probe_range_support_accepts_200_with_matching_content_length(monkeypatch):
    """ModelScope's download endpoint honors Range but replies 200, not 206
    - confirmed live (see docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md).
    A single byte requested and exactly one byte returned, with a Content-Range
    header proving the server sliced correctly, must count as Range support."""

    class _FakeResp:
        status_code = 200
        headers = {"Content-Range": "bytes 0-0/491400032", "Content-Length": "1"}

        def close(self):
            pass

    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: _FakeResp())
    total, supports_ranges = downloader._probe_range_support("https://example.com/f.gguf")
    assert total == 491400032
    assert supports_ranges is True


def test_probe_range_support_rejects_200_with_full_content_length(monkeypatch):
    """A server that ignores the Range header and returns the whole file
    with status 200 must NOT be treated as Range-capable, or a "parallel"
    download would just refetch the entire file once per thread."""

    class _FakeResp:
        status_code = 200
        headers = {"Content-Length": "491400032"}

        def close(self):
            pass

    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: _FakeResp())
    total, supports_ranges = downloader._probe_range_support("https://example.com/f.gguf")
    assert supports_ranges is False
```

Add `from omm import downloader` (or whatever the file's existing import already is - check first) to the test file's imports if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_downloader.py -k "probe_range_support" -v`
Expected: FAIL - `test_probe_range_support_accepts_200_with_matching_content_length` fails because `_probe_range_support` currently only checks `resp.status_code == 206`.

- [ ] **Step 3: Fix `_probe_range_support`**

In `src/omm/downloader.py`, replace:

```python
def _probe_range_support(url: str) -> tuple[int, bool]:
    """Probe with a 1-byte Range request. Returns (total_size, supports_ranges).
    A 206 with a parseable `Content-Range` means the server (and by
    extension its CDN) honors Range requests, so a full download can be
    safely split across threads."""
    try:
        resp = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=30)
    except requests.RequestException:
        return 0, False
    resp.close()
    if resp.status_code == 206:
        content_range = resp.headers.get("Content-Range", "")
        try:
            total = int(content_range.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return 0, False
        return total, total > 0
    return 0, False
```

with:

```python
def _probe_range_support(url: str) -> tuple[int, bool]:
    """Probe with a 1-byte Range request. Returns (total_size, supports_ranges).
    A 206 with a parseable `Content-Range` means the server (and by
    extension its CDN) honors Range requests, so a full download can be
    safely split across threads. Some servers (confirmed: ModelScope's
    download endpoint) honor the Range header - returning exactly the
    requested byte(s) with a correct Content-Range - but reply with status
    200 instead of the RFC-correct 206; a 200 only counts as Range support
    when Content-Length matches the single byte we asked for, so a server
    that ignores Range and dumps the whole file with status 200 isn't
    mistaken for one that sliced it."""
    try:
        resp = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=30)
    except requests.RequestException:
        return 0, False
    resp.close()
    content_range = resp.headers.get("Content-Range", "")
    honored = resp.status_code == 206 or (
        resp.status_code == 200 and resp.headers.get("Content-Length") == "1"
    )
    if honored and content_range:
        try:
            total = int(content_range.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return 0, False
        return total, total > 0
    return 0, False
```

- [ ] **Step 4: Fix `_download_range_worker` to accept the same 200-with-Range case**

In `src/omm/downloader.py`, replace:

```python
    try:
        resp = requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=30)
        if resp.status_code != 206:
            raise DownloadError(f"Expected 206 for a range request, got {resp.status_code}")
```

with:

```python
    try:
        resp = requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=30)
        expected_len = end - start + 1
        honored = resp.status_code == 206 or (
            resp.status_code == 200
            and resp.headers.get("Content-Length") == str(expected_len)
        )
        if not honored:
            raise DownloadError(
                f"Expected a Range response for bytes={start}-{end}, got "
                f"status {resp.status_code} with Content-Length "
                f"{resp.headers.get('Content-Length')}"
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_downloader.py -v`
Expected: PASS (all tests, including the two new ones)

- [ ] **Step 6: Commit**

```bash
git add src/omm/downloader.py tests/test_downloader.py
git commit -m "fix: accept 200-status Range responses (ModelScope) as parallel-download-eligible"
```

---

### Task 6: `AmbiguousProviderError` handling + provider-aware quant re-pick in `cli.py install()`

**Files:**
- Modify: `src/omm/cli.py:64-76` (import block), `:960-982` (`_pick_quant_variant`), `:1323-1353` (`install()`)
- Test: `tests/test_cli_install_quant_picker.py`, create `tests/test_cli_install_ambiguous_provider.py`

**Interfaces:**
- Consumes: `omm.hub.AmbiguousProviderError` (Task 1), `omm.hub.remote_file_size(provider, repo_id, filename)` (Task 2/4 dispatch signature).
- Produces: `install()` now handles `AmbiguousProviderError` by prompting the user to pick a provider, then re-resolving with an explicit prefix.

- [ ] **Step 1: Update the `cli.py` import block**

Change:

```python
from omm.hub import (
    HF_DOWNLOAD,
    AmbiguousModelError,
    ModelResolutionError,
    QuantVariant,
    ResolvedModel,
    best_filenames_by_tier,
    fetch_repo_param_count_b,
    rank_quant_variants,
    remote_file_size,
    remote_file_sha256,
    resolve_model,
)
```

to:

```python
from omm.hub import (
    AmbiguousModelError,
    AmbiguousProviderError,
    ModelResolutionError,
    QuantVariant,
    ResolvedModel,
    best_filenames_by_tier,
    download_url,
    fetch_repo_param_count_b,
    rank_quant_variants,
    remote_file_size,
    remote_file_sha256,
    resolve_model,
)
```

(`HF_DOWNLOAD` is dropped - both of its call sites are replaced with the provider-dispatched `download_url` in Task 8. `download_url` is added now so it's available for Task 8's edits, avoiding a second import-block churn.)

- [ ] **Step 2: Write the failing test for `AmbiguousProviderError` handling**

Create `tests/test_cli_install_ambiguous_provider.py`:

```python
"""Tests that `omm install org/repo` prompts for a provider when the repo
exists on more than one, instead of crashing or silently picking one."""

from __future__ import annotations

import pytest
import questionary

from omm import cli
from omm.hub import AmbiguousProviderError, ResolvedModel


def test_install_prompts_for_provider_on_ambiguous_match(monkeypatch, isolated_omm_home):
    calls = []

    def fake_resolve_model(name):
        if name == "org/repo":
            raise AmbiguousProviderError("org/repo", ["huggingface", "modelscope"])
        calls.append(name)
        return ResolvedModel(
            url="https://modelscope.cn/api/v1/models/org/repo/repo?FilePath=x.gguf",
            filename="x.gguf",
            repo_id="org/repo",
            provider="modelscope",
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(cli, "_resolve_ref", lambda name: name)
    monkeypatch.setattr(
        cli, "_ask_select", lambda prompt: "modelscope"
    )
    monkeypatch.setattr(
        cli,
        "_install_impl",
        lambda resolved, **kwargs: cli.InstallOutcome(
            resolved.filename, resolved.repo_id, {}, None, None, False, sha256="x"
        ),
    )

    cli.install("org/repo", skip_unfit=False, upload=None)

    assert calls == ["modelscope:org/repo"]
```

Read `src/omm/cli.py`'s `_ask_select` helper and `InstallOutcome` dataclass definition first (grep for `def _ask_select` and `class InstallOutcome`) to confirm this test's mock shapes match the real signatures before running it - adjust the monkeypatch targets if the real names differ.

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_cli_install_ambiguous_provider.py -v`
Expected: FAIL - `install()` doesn't catch `AmbiguousProviderError` yet, so it propagates uncaught (or `AmbiguousProviderError` isn't importable from `cli`'s current `omm.hub` import list).

- [ ] **Step 4: Handle `AmbiguousProviderError` in `install()`**

In `src/omm/cli.py`, change:

```python
    model_name = _resolve_ref(model_name)
    try:
        resolved = resolve_model(model_name)
    except AmbiguousModelError as e:
        chosen = _pick_quant_variant(e)
        if chosen is None:
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(f"{e.repo_id}:{chosen}", skip_unfit=skip_unfit, upload=upload)
        return
    except ModelResolutionError as e:
        err_console.print(f"[red]{e}[/red]")
        _print_install_suggestions(model_name)
        raise typer.Exit(1) from e
```

to:

```python
    model_name = _resolve_ref(model_name)
    try:
        resolved = resolve_model(model_name)
    except AmbiguousModelError as e:
        chosen = _pick_quant_variant(e)
        if chosen is None:
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(f"{e.provider}:{e.repo_id}:{chosen}", skip_unfit=skip_unfit, upload=upload)
        return
    except AmbiguousProviderError as e:
        choices = [
            questionary.Choice(title=provider, value=provider) for provider in e.providers
        ]
        chosen_provider = _ask_select(
            questionary.select(f"'{e.repo_id}' found on multiple providers, pick one:", choices=choices)
        )
        if chosen_provider is None:
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(f"{chosen_provider}:{e.repo_id}", skip_unfit=skip_unfit, upload=upload)
        return
    except ModelResolutionError as e:
        err_console.print(f"[red]{e}[/red]")
        _print_install_suggestions(model_name)
        raise typer.Exit(1) from e
```

- [ ] **Step 5: Make `_pick_quant_variant` provider-aware**

In `src/omm/cli.py`, change:

```python
        size_bytes = remote_file_size(error.repo_id, variant.filename)
```

to:

```python
        size_bytes = remote_file_size(error.provider, error.repo_id, variant.filename)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_install_ambiguous_provider.py tests/test_cli_install_quant_picker.py tests/test_cli_install_suggestions.py -v`
Expected: PASS. If `test_cli_install_quant_picker.py` has a test asserting the exact recursive-call string `f"{e.repo_id}:{chosen}"` (without a provider prefix), update that assertion to expect the `huggingface:` prefix now included (the default `provider="huggingface"` from Task 1's `AmbiguousModelError` makes this deterministic for existing HF-only test fixtures).

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_install_ambiguous_provider.py tests/test_cli_install_quant_picker.py
git commit -m "feat: prompt for a provider when omm install matches more than one hub"
```

---

### Task 7: `_update_one()` and `_install_impl()` become provider-aware (registry + `omm upgrade`)

**Files:**
- Modify: `src/omm/cli.py:1137-1230` (`_install_impl`, the `registry.upsert_entry` call), `:1508-1568` (`_update_one`), `:1455-1506` (`info()`)
- Test: `tests/test_install_impl.py`, `tests/test_cli_upgrade.py`, `tests/test_cli_info.py`

**Interfaces:**
- Consumes: `omm.hub.download_url(provider, repo_id, filename)`, `omm.hub.remote_file_sha256(provider, repo_id, filename)` (Task 2/4).
- Produces: registry entries gain a `"provider"` key; `omm upgrade` and `omm info` read it via `entry.get("provider") or "huggingface"`.

- [ ] **Step 1: Add `provider` to the registry write in `_install_impl`**

In `src/omm/cli.py`, change:

```python
    registry.upsert_entry(
        filename,
        sha256=sha256,
        version=sha256[:7],
        source=url,
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        repo_id=repo_id,
        linked=linked,
    )
```

to:

```python
    registry.upsert_entry(
        filename,
        sha256=sha256,
        version=sha256[:7],
        source=url,
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        repo_id=repo_id,
        provider=resolved.provider or "huggingface",
        linked=linked,
    )
```

- [ ] **Step 2: Run `test_install_impl.py` to see the new field's effect**

Run: `python -m pytest tests/test_install_impl.py -v`
Expected: PASS (the existing tests build `ResolvedModel(url=..., filename=..., repo_id="org/repo")` without a `provider` kwarg, which defaults to `None` per Task 2's dataclass default - `resolved.provider or "huggingface"` then writes `"huggingface"`, and no existing assertion checks for the *absence* of a `provider` key, so nothing should break). If any assertion does an exact-dict-equality check on the registry entry, update it to include `"provider": "huggingface"`.

- [ ] **Step 3: Make `_update_one` provider-aware**

In `src/omm/cli.py`, change:

```python
    dest = MODELS_DIR / filename
    repo_id = entry.get("repo_id")
    old_sha256 = entry.get("sha256")

    if repo_id:
        remote_sha256 = remote_file_sha256(repo_id, filename)
        if remote_sha256 is None:
            err_console.print(
                f"[yellow]{filename}: could not check for updates "
                "(no repo/LFS info), skipped.[/yellow]"
            )
            return "skipped"
        if remote_sha256 == old_sha256:
            return "up_to_date"

        url = HF_DOWNLOAD.format(repo_id=repo_id, filename=filename)
        try:
            download_file(url, dest)
```

to:

```python
    dest = MODELS_DIR / filename
    repo_id = entry.get("repo_id")
    provider = entry.get("provider") or "huggingface"
    old_sha256 = entry.get("sha256")

    if repo_id:
        remote_sha256 = remote_file_sha256(provider, repo_id, filename)
        if remote_sha256 is None:
            err_console.print(
                f"[yellow]{filename}: could not check for updates "
                "(no repo/LFS info), skipped.[/yellow]"
            )
            return "skipped"
        if remote_sha256 == old_sha256:
            return "up_to_date"

        url = download_url(provider, repo_id, filename)
        try:
            download_file(url, dest)
```

- [ ] **Step 4: Preserve `provider` across `omm upgrade`'s registry rewrite**

Still in `_update_one`, change:

```python
    ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
    linked = _link_model(dest, repo_id, ollama_tag)
    registry.upsert_entry(
        filename,
        sha256=new_sha256,
        version=new_sha256[:7],
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        linked=linked,
    )
    return "updated"
```

to:

```python
    ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
    linked = _link_model(dest, repo_id, ollama_tag)
    registry.upsert_entry(
        filename,
        sha256=new_sha256,
        version=new_sha256[:7],
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        provider=provider,
        linked=linked,
    )
    return "updated"
```

- [ ] **Step 5: Run the upgrade tests**

Run: `python -m pytest tests/test_cli_upgrade.py -v`
Expected: PASS. If a test monkeypatches `cli.remote_file_sha256` with a 2-argument lambda (`lambda repo_id, filename: ...`), update it to a 3-argument lambda (`lambda provider, repo_id, filename: ...`) to match the new dispatch signature - this is a required test edit, not a production bug.

- [ ] **Step 6: Show the provider in `omm info`**

In `src/omm/cli.py`'s `info()`, change:

```python
    table.add_row("Repo", entry.get("repo_id") or "(direct URL install)")
```

to:

```python
    repo_label = entry.get("repo_id") or "(direct URL install)"
    provider = entry.get("provider")
    if entry.get("repo_id") and provider and provider != "huggingface":
        repo_label = f"{repo_label} [{provider}]"
    table.add_row("Repo", repo_label)
```

and in the same function's `--json` branch, change:

```python
        console.print_json(
            data={
                "filename": filename,
                "repo_id": entry.get("repo_id"),
```

to:

```python
        console.print_json(
            data={
                "filename": filename,
                "repo_id": entry.get("repo_id"),
                "provider": entry.get("provider") or ("huggingface" if entry.get("repo_id") else None),
```

- [ ] **Step 7: Run the info tests**

Run: `python -m pytest tests/test_cli_info.py -v`
Expected: PASS. If a test does exact-dict-equality on the `--json` output, add the new `"provider"` key to its expected dict.

- [ ] **Step 8: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py tests/test_cli_upgrade.py tests/test_cli_info.py
git commit -m "feat: thread provider through install registry, omm upgrade, and omm info"
```

---

### Task 8: `_run_contribution_loop` - drop the hardcoded `HF_DOWNLOAD.format(...)`

**Files:**
- Modify: `src/omm/cli.py:2716-2745` (`_run_contribution_loop`)
- Test: `tests/test_contribute_loop.py`

**Interfaces:**
- Consumes: `omm.hub.download_url(provider, repo_id, filename)` (Task 2/4, already imported in Task 6 Step 1).
- Produces: candidates whose dict has `"provider"` (Task 10 will start populating this in `published/candidates.json`) now download from the right provider instead of always being forced through `HF_DOWNLOAD`.

- [ ] **Step 1: Update the failing assertion (if any) in `test_contribute_loop.py`**

Read `tests/test_contribute_loop.py`'s `_candidate(...)` helper first. If it doesn't already accept a `provider` kwarg, add one with a default of `"huggingface"`:

```python
def _candidate(repo_id="org/repo", filename="model.gguf", name="model", provider="huggingface"):
    return {"repo_id": repo_id, "filename": filename, "name": name, "provider": provider}
```

Then add a new test to the same file:

```python
def test_run_contribution_loop_builds_url_via_provider_dispatch(monkeypatch):
    seen_urls = []

    def fake_install_impl(resolved, **kwargs):
        seen_urls.append(resolved.url)
        return cli.InstallOutcome(
            resolved.filename, resolved.repo_id, {}, None, 5.0, True, sha256="x"
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {})
    monkeypatch.setattr(cli, "_lookup_entry", lambda filename, reg: (None, None))
    monkeypatch.setattr(
        cli.benchmark_history, "record_benchmarked", lambda *a, **k: None
    )

    candidate = _candidate(repo_id="org/repo", filename="model.gguf", provider="modelscope")
    queue = _FakeQueue([candidate])
    stop_event = threading.Event()
    stop_event.set()  # loop exits after the first candidate

    cli._run_contribution_loop(queue, stop_event, refetch=lambda: (None, False))

    assert seen_urls == [
        "https://modelscope.cn/api/v1/models/org/repo/repo?Revision=master&FilePath=model.gguf"
    ]
```

Check the file's existing `_FakeQueue` class and `threading`/`cli` imports first (per the research summary, `test_contribute_loop.py` already has a `_FakeQueue` with `next_candidate`/`mark_seen` - reuse it, adjust the constructor call to match its real signature if different from `_FakeQueue([candidate])` above).

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_contribute_loop.py -k provider_dispatch -v`
Expected: FAIL - `_run_contribution_loop` still calls `HF_DOWNLOAD.format(...)` unconditionally, so `seen_urls` would contain an `huggingface.co` URL instead.

- [ ] **Step 3: Fix `_run_contribution_loop`**

In `src/omm/cli.py`, change:

```python
        resolved = ResolvedModel(
            url=HF_DOWNLOAD.format(repo_id=candidate["repo_id"], filename=candidate["filename"]),
            filename=candidate["filename"],
            repo_id=candidate["repo_id"],
        )
```

to:

```python
        candidate_provider = candidate.get("provider") or "huggingface"
        resolved = ResolvedModel(
            url=download_url(candidate_provider, candidate["repo_id"], candidate["filename"]),
            filename=candidate["filename"],
            repo_id=candidate["repo_id"],
            provider=candidate_provider,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_contribute_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_contribute_loop.py
git commit -m "fix: omm contribute downloads candidates via provider dispatch instead of always HuggingFace"
```

---

### Task 9: `contribute.py` - provider-aware `ref()` with legacy-format compatibility

**Files:**
- Modify: `src/omm/contribute.py:25-26`
- Test: `tests/test_contribute_selection.py`

**Interfaces:**
- Produces: `contribute.ref(candidate)` now returns `f"{provider}:{repo_id}:{filename}"`; a new `contribute.matches_history(candidate, history_refs)` helper treats a legacy bare `f"{repo_id}:{filename}"` entry in `history_refs` as equivalent to today's HF-provider ref, so previously-benchmarked HF models don't reappear as "new" once every ref gains a provider prefix.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contribute_selection.py` (read the file first to match its existing `_candidate`/fixture helpers):

```python
def test_ref_includes_provider_prefix():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf", "provider": "modelscope"}
    assert contribute.ref(candidate) == "modelscope:org/repo:model.gguf"


def test_ref_defaults_to_huggingface_when_provider_missing():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf"}
    assert contribute.ref(candidate) == "huggingface:org/repo:model.gguf"


def test_matches_history_accepts_legacy_unprefixed_hf_ref():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf", "provider": "huggingface"}
    legacy_history = {"org/repo:model.gguf"}
    assert contribute.matches_history(candidate, legacy_history) is True


def test_matches_history_rejects_legacy_ref_for_non_hf_provider():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf", "provider": "modelscope"}
    legacy_history = {"org/repo:model.gguf"}
    assert contribute.matches_history(candidate, legacy_history) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_contribute_selection.py -k "ref_includes or ref_defaults or matches_history" -v`
Expected: FAIL - `ref()` still returns `f"{repo_id}:{filename}"` with no provider prefix, and `matches_history` doesn't exist yet.

- [ ] **Step 3: Update `contribute.py`**

In `src/omm/contribute.py`, change:

```python
def ref(candidate: dict) -> str:
    return f"{candidate['repo_id']}:{candidate['filename']}"
```

to:

```python
def ref(candidate: dict) -> str:
    provider = candidate.get("provider") or "huggingface"
    return f"{provider}:{candidate['repo_id']}:{candidate['filename']}"


def matches_history(candidate: dict, history_refs: set[str]) -> bool:
    """True if `candidate` is already in `history_refs`, accepting both the
    current provider-prefixed ref format and the legacy bare
    "repo_id:filename" format that pre-dates provider support (always
    HuggingFace, since it was the only provider back then)."""
    if ref(candidate) in history_refs:
        return True
    provider = candidate.get("provider") or "huggingface"
    if provider != "huggingface":
        return False
    legacy_ref = f"{candidate['repo_id']}:{candidate['filename']}"
    return legacy_ref in history_refs
```

- [ ] **Step 4: Use `matches_history` everywhere `ref(...) not in self.history_refs` / `ref(candidate) not in history_refs` currently appears**

In `src/omm/contribute.py`, within `_next_unseen`, change:

```python
def _next_unseen(
    pool: list[tuple[dict, float]], history_refs: set[str], cursor: int
) -> tuple[dict | None, int]:
    """Scan `pool` starting at `cursor`, wrapping at most once, for a
    candidate not in `history_refs`."""
    n = len(pool)
    if n == 0:
        return None, cursor
    for step in range(n):
        idx = (cursor + step) % n
        candidate, _ = pool[idx]
        if ref(candidate) not in history_refs:
            return candidate, idx + 1
    return None, cursor
```

to:

```python
def _next_unseen(
    pool: list[tuple[dict, float]], history_refs: set[str], cursor: int
) -> tuple[dict | None, int]:
    """Scan `pool` starting at `cursor`, wrapping at most once, for a
    candidate not in `history_refs`."""
    n = len(pool)
    if n == 0:
        return None, cursor
    for step in range(n):
        idx = (cursor + step) % n
        candidate, _ = pool[idx]
        if not matches_history(candidate, history_refs):
            return candidate, idx + 1
    return None, cursor
```

And within `ContributionQueue._rebuild`, change:

```python
        self._phase_a_queue = [c for c, s in viable if ref(c) not in self.history_refs]
```

to:

```python
        self._phase_a_queue = [c for c, s in viable if not matches_history(c, self.history_refs)]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_contribute_selection.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/contribute.py tests/test_contribute_selection.py
git commit -m "feat: provider-prefixed contribute refs with legacy HF-ref compatibility"
```

---

### Task 10: `telemetry` payload gains `model_provider`

**Files:**
- Modify: `src/omm/cli.py` (`_report_telemetry`, exact line range: grep `def _report_telemetry` first - the research in this plan's prep quoted the payload keys but not line numbers; read the function before editing)
- Test: `tests/test_install_impl.py`

**Interfaces:**
- Produces: every telemetry event dict built by `_report_telemetry` now includes `"model_provider"` alongside the existing `"model_repo_id"`.

- [ ] **Step 1: Extend `_resolved()` and add a failing test to `test_install_impl.py`**

`tests/test_install_impl.py`'s helper at the top of the file currently reads:

```python
def _resolved(filename="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"):
    return ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo")
```

Change it to accept an optional `provider` (defaulting to `None`, matching `ResolvedModel`'s own default from Task 2, so every existing call site like `_resolved()` and `_resolved(filename=...)` keeps behaving exactly as before):

```python
def _resolved(filename="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf", provider=None):
    return ResolvedModel(
        url="https://example.com/x.gguf", filename=filename, repo_id="org/repo", provider=provider
    )
```

Then add this test, copying the exact monkeypatch scaffolding `test_auto_upload_skips_confirm_prompt_and_sends_telemetry` (a few lines above it in the same file) already uses:

```python
def test_install_impl_telemetry_includes_model_provider(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    captured = {}
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: captured.update(event) or True
    )

    cli._install_impl(_resolved(provider="modelscope"), auto_upload=True)

    assert captured["model_provider"] == "modelscope"


def test_install_impl_telemetry_defaults_provider_to_huggingface(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    captured = {}
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: captured.update(event) or True
    )

    cli._install_impl(_resolved(), auto_upload=True)

    assert captured["model_provider"] == "huggingface"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_install_impl.py -k model_provider -v`
Expected: FAIL - `KeyError: 'model_provider'`

- [ ] **Step 3: Add `model_provider` to the telemetry event**

Find `_report_telemetry` in `src/omm/cli.py` (grep `def _report_telemetry`). It's called from `_install_impl` as `_report_telemetry(filename, repo_id, tokens_per_sec, ...)` and `_report_telemetry(filename, repo_id, tokens_per_sec)` - both call sites only pass `repo_id`, not `provider`. Add a `provider: str | None = None` parameter to `_report_telemetry`'s signature, and set `event["model_provider"] = provider or "huggingface"` alongside the existing `event["model_repo_id"] = repo_id` line (read the function body first to find the exact line - the research already quoted the full key list: `model_installed`, `model_repo_id`, `model_size_bytes`, ... - add the new key immediately after `model_repo_id`).

Then update both call sites inside `_install_impl`:

```python
            telemetry_sent = _report_telemetry(
                filename,
                repo_id,
                tokens_per_sec,
                sample_count=sample_count,
                speed_min=speed_min,
                speed_max=speed_max,
                quality=quality_summary,
                model_metadata=model_metadata,
                runtime=runtime,
                engine_version=engine_version,
                model_filename=filename,
                model_digest=sha256,
            )
```

add `provider=resolved.provider,` to this call's kwargs, and:

```python
        telemetry_sent = _report_telemetry(filename, repo_id, tokens_per_sec)
```

becomes:

```python
        telemetry_sent = _report_telemetry(filename, repo_id, tokens_per_sec, provider=resolved.provider)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_install_impl.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py
git commit -m "feat: include model_provider in benchmark telemetry payloads"
```

---

### Task 11: `search.py` - `search_modelscope()` and provider tagging

**Files:**
- Modify: `src/omm/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `omm.providers.modelscope.fetch_repo_files` (Task 3).
- Produces: `search.search_modelscope(query: str, limit: int = 20, timeout: float = 3.0) -> list[dict]` with the same dict shape as `search_huggingface` plus `"provider": "modelscope"`; `search_huggingface`'s results gain `"provider": "huggingface"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py` (match the file's existing `_Resp` fake-response class and `monkeypatch.setattr(search_mod.requests, "get", ...)` pattern):

```python
_MS_SEARCH_PAYLOAD = {
    "success": True,
    "data": {
        "models": [
            {
                "id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
                "downloads": 50622,
                "tags": ["library:gguf", "task:text-generation"],
            },
            {
                "id": "some-org/not-gguf-model",
                "downloads": 10,
                "tags": ["task:text-generation"],
            },
        ]
    },
}


def test_search_modelscope_filters_to_gguf_tagged_repos_and_picks_a_file(monkeypatch):
    monkeypatch.setattr(
        search_mod.requests, "get", lambda *a, **k: _Resp(_MS_SEARCH_PAYLOAD)
    )
    monkeypatch.setattr(
        search_mod.modelscope,
        "fetch_repo_files",
        lambda repo_id: (["qwen2.5-0.5b-instruct-q4_k_m.gguf"], None),
    )
    results = search_mod.search_modelscope("qwen2.5")
    assert len(results) == 1
    assert results[0]["repo_id"] == "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
    assert results[0]["filename"] == "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    assert results[0]["provider"] == "modelscope"


def test_search_modelscope_skips_fake_provenance_repos(monkeypatch):
    payload = {
        "success": True,
        "data": {
            "models": [
                {"id": "someone/claude-4-opus-gguf", "downloads": 1, "tags": ["library:gguf"]}
            ]
        },
    }
    monkeypatch.setattr(search_mod.requests, "get", lambda *a, **k: _Resp(payload))
    results = search_mod.search_modelscope("claude")
    assert results == []


def test_search_huggingface_results_are_tagged_huggingface(monkeypatch):
    payload = [
        {
            "id": "org/repo",
            "siblings": [{"rfilename": "model.Q4_K_M.gguf"}],
        }
    ]
    monkeypatch.setattr(search_mod.requests, "get", lambda *a, **k: _Resp(payload))
    results = search_mod.search_huggingface("query")
    assert results[0]["provider"] == "huggingface"
```

Check what `_Resp` in the existing file wraps (a `.json()`-returning object per the research summary) and use it exactly as-is; if the existing `_Resp` doesn't accept a payload constructor argument, adapt these tests to match its real interface (e.g. a module-level fixture instead) rather than inventing a new class.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -k "modelscope or huggingface_results_are_tagged" -v`
Expected: FAIL - `AttributeError: module 'omm.search' has no attribute 'search_modelscope'`, and the HF tag test fails because `"provider"` isn't in the result dict yet.

- [ ] **Step 3: Add `provider` tagging to `search_huggingface`**

In `src/omm/search.py`, change:

```python
        results.append(
            {
                "name": repo_id,
                "repo_id": repo_id,
                "filename": filename,
                "description": "HuggingFace",
            }
        )
    return results
```

to:

```python
        results.append(
            {
                "name": repo_id,
                "repo_id": repo_id,
                "filename": filename,
                "description": "HuggingFace",
                "provider": "huggingface",
            }
        )
    return results
```

- [ ] **Step 4: Add `search_modelscope`**

In `src/omm/search.py`, add the import `from omm.providers import modelscope` near the top (alongside the existing `from omm import hub, predictor`), then add this function after `search_huggingface`:

```python
MS_SEARCH_API = "https://modelscope.cn/openapi/v1/models"


def search_modelscope(query: str, limit: int = 20, timeout: float = 3.0) -> list[dict]:
    try:
        resp = requests.get(
            MS_SEARCH_API,
            params={"search": query, "page_size": limit},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return []

    models = payload.get("data", {}).get("models", [])
    gguf_tagged = [m for m in models if "library:gguf" in m.get("tags", [])]

    results = []
    for item in gguf_tagged[:15]:
        repo_id = item.get("id")
        if not repo_id or _claims_fake_provenance(repo_id):
            continue
        try:
            files, _ = modelscope.fetch_repo_files(repo_id)
        except Exception:  # noqa: BLE001 - a single bad repo shouldn't kill the search
            continue
        filename = pick_gguf_file([{"rfilename": f} for f in files])
        if filename is None:
            continue
        results.append(
            {
                "name": repo_id,
                "repo_id": repo_id,
                "filename": filename,
                "description": f"{item.get('downloads', 0):,} downloads on ModelScope",
                "provider": "modelscope",
            }
        )
    return results
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/search.py tests/test_search.py
git commit -m "feat: add search_modelscope, tag search_huggingface results with provider"
```

---

### Task 12: `catalog.install_ref()` provider prefixing + `cli.py search()` merges ModelScope

**Files:**
- Modify: `src/omm/search.py` (`install_ref`), `src/omm/cli.py` (`search()` command)
- Test: `tests/test_search.py`, `tests/test_cli_search.py`

**Interfaces:**
- Consumes: `search.search_modelscope` (Task 11).
- Produces: `search.install_ref(candidate)` returns `ms:org/repo` for ModelScope candidates; `omm search <query>` includes ModelScope results in its output.

- [ ] **Step 1: Write the failing test for `install_ref`**

Add to `tests/test_search.py`:

```python
def test_install_ref_prefixes_modelscope_candidates():
    candidate = {"name": "org/repo", "repo_id": "org/repo", "provider": "modelscope"}
    assert search_mod.install_ref(candidate) == "ms:org/repo"


def test_install_ref_leaves_huggingface_candidates_unprefixed():
    candidate = {"name": "org/repo", "repo_id": "org/repo", "provider": "huggingface"}
    assert search_mod.install_ref(candidate) == "org/repo"


def test_install_ref_leaves_curated_names_unprefixed():
    candidate = {"name": "tinyllama-1.1b-q4", "repo_id": "TheBloke/x", "provider": "huggingface"}
    assert search_mod.install_ref(candidate) == "tinyllama-1.1b-q4"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -k install_ref -v`
Expected: FAIL - `install_ref` never adds a `ms:` prefix.

- [ ] **Step 3: Update `install_ref`**

In `src/omm/search.py`, change:

```python
def install_ref(candidate: dict) -> str:
    """The string a user can actually pass to `omm install`, as opposed to
    the human-readable label. A curated short name only resolves if it's a
    literal CURATED_INDEX key. Everything else is left as a bare 'org/repo'
    - repos almost always ship several quants under one name, and `omm
    install org/repo` already walks the user through picking one (see
    hub.AmbiguousModelError), so there's no need to hardcode a filename here
    and print what looks like a distinct result per quant.
    """
    name = candidate.get("name")
    if name and name in hub.CURATED_INDEX:
        return name
    return candidate.get("repo_id") or name or ""
```

to:

```python
def install_ref(candidate: dict) -> str:
    """The string a user can actually pass to `omm install`, as opposed to
    the human-readable label. A curated short name only resolves if it's a
    literal CURATED_INDEX key. Everything else is left as a bare 'org/repo'
    (or 'ms:org/repo' for a non-HuggingFace provider) - repos almost always
    ship several quants under one name, and `omm install org/repo` already
    walks the user through picking one (see hub.AmbiguousModelError), so
    there's no need to hardcode a filename here and print what looks like a
    distinct result per quant.
    """
    name = candidate.get("name")
    if name and name in hub.CURATED_INDEX:
        return name
    repo_id = candidate.get("repo_id") or name or ""
    provider = candidate.get("provider") or "huggingface"
    if provider == "huggingface" or not repo_id:
        return repo_id
    return f"{_PROVIDER_PREFIX[provider]}:{repo_id}"


_PROVIDER_PREFIX = {"modelscope": "ms"}
```

(Define `_PROVIDER_PREFIX` right after `install_ref` rather than before it, matching where it's used - Python doesn't care about definition order here since it's resolved at call time, not import time... actually it does need to exist by the time `install_ref` is *called*, and since it's a module-level constant defined below the function, that's fine at import time too since the function body only looks it up when invoked. Keep it directly below `install_ref` as shown.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for `cli.py search()` merging ModelScope**

`tests/test_cli_search.py` drives the command through `runner.invoke(cli.app, ["search", ...])` (a module-level `runner = CliRunner()`), not by calling `cli.search(...)` directly. Add this test to the file, following `test_search_prints_numbered_refs_and_records_session`'s exact style:

```python
def test_search_command_includes_modelscope_results(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(cli.search_mod, "local_candidate_pool", lambda model_url: [])
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(
        cli.search_mod,
        "search_modelscope",
        lambda query, **kwargs: [
            {
                "name": "org/repo",
                "repo_id": "org/repo",
                "filename": "model.gguf",
                "description": "1,000 downloads on ModelScope",
                "provider": "modelscope",
            }
        ],
    )
    recorded = []
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: recorded.append(refs))

    result = runner.invoke(cli.app, ["search", "repo"])

    assert result.exit_code == 0, result.stdout
    assert "[1] ms:org/repo" in result.stdout
    assert recorded == [["ms:org/repo"]]
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `python -m pytest tests/test_cli_search.py -k modelscope -v`
Expected: FAIL - `cli.search_mod.search_modelscope` isn't called by `search()` yet, or the attribute doesn't exist (fixed in Task 11, but not yet wired into the `search()` command).

- [ ] **Step 7: Wire `search_modelscope` into the `search()` command**

In `src/omm/cli.py`, change:

```python
    local_repo_ids = {c.get("repo_id") for c in local_matches if c.get("repo_id")}
    hf_matches = [
        c
        for c in search_mod.search_huggingface(query)
        if c.get("repo_id") not in local_repo_ids
    ]

    combined = search_mod.dedupe_by_base_repo(local_matches + hf_matches)
```

to:

```python
    local_repo_ids = {c.get("repo_id") for c in local_matches if c.get("repo_id")}
    hf_matches = [
        c
        for c in search_mod.search_huggingface(query)
        if c.get("repo_id") not in local_repo_ids
    ]
    ms_matches = [
        c
        for c in search_mod.search_modelscope(query)
        if c.get("repo_id") not in local_repo_ids
    ]

    combined = search_mod.dedupe_by_base_repo(local_matches + hf_matches + ms_matches)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_search.py tests/test_search.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/omm/search.py src/omm/cli.py tests/test_search.py tests/test_cli_search.py
git commit -m "feat: merge ModelScope results into omm search, prefix non-HF install refs"
```

---

### Task 13: `recommend()` uses `install_ref()` instead of a hardcoded HF-shaped ref

**Files:**
- Modify: `src/omm/cli.py:766-829` (`recommend()`)
- Test: create `tests/test_cli_recommend.py`

**Interfaces:**
- Consumes: `search_mod.install_ref` (Task 12).
- Produces: `recommend()`'s picker now installs via `install(search_mod.install_ref(c))` instead of `install(f"{c['repo_id']}:{c['filename']}")`, so a ModelScope candidate in the trained model's candidate pool installs correctly instead of being misinterpreted as an HF ref.

This is also a pre-existing bug fix independent of ModelScope: today, `recommend()` and `search()` build install refs two different ways (`recommend()` inlines `f"{repo_id}:{filename}"`, `search()` calls `search_mod.install_ref(c)`), and only `search_mod.install_ref` knows about curated-name shortcuts. Unifying on `install_ref` fixes that divergence too.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_recommend.py` (there is currently no test file covering `recommend()`'s ranking/install-ref logic - `test_cli_recommend_escape.py` only tests an unrelated keybinding helper per this plan's research). The bug lives in how `recommend()` builds the `value=` of each `questionary.Choice` it offers, so the test must capture the `choices` argument passed to `questionary.select` rather than asserting on `install()`'s argument (which is one layer removed from where the fix applies):

```python
"""Tests for `recommend()`'s candidate ranking and install-ref building."""

from __future__ import annotations

from omm import cli


def test_recommend_builds_choice_values_via_install_ref(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}
    captured_choices = []

    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    def fake_select(prompt_text, choices):
        captured_choices.extend(choices)
        return _DummySelect()

    class _DummySelect:
        pass

    monkeypatch.setattr(cli.questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda select_obj: None)  # cancel path, avoids install()

    try:
        cli.recommend()
    except cli.typer.Exit:
        pass  # typer.Exit(0) on the cancel path is expected - not a real SystemExit
              # when recommend() is called directly instead of via CliRunner

    assert captured_choices[0].value == "ms:org/repo"
```

Note `except cli.typer.Exit`, not `except SystemExit` - `typer.Exit` only becomes a process exit when raised through Click's own dispatch (e.g. via `CliRunner.invoke`); called directly as `cli.recommend()`, it's just a plain exception - confirmed against the existing pattern in `tests/test_cli_install_confirm.py:115`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_cli_recommend.py -v`
Expected: FAIL - `captured_choices[0].value == "org/repo:model.gguf"`, not `"ms:org/repo"`.

- [ ] **Step 3: Fix `recommend()`**

In `src/omm/cli.py`, change:

```python
        refs = [f"{c['repo_id']}:{c['filename']}" for c, speed in viable]
        session_cache.record_seen(refs)
        choices = [
            questionary.Choice(
                title=f"{c['name']} (~{speed:.0f} tok/s predicted) - {c.get('description', '')}",
                value=ref,
            )
            for (c, speed), ref in zip(viable, refs)
        ]
```

to:

```python
        refs = [search_mod.install_ref(c) for c, speed in viable]
        session_cache.record_seen(refs)
        choices = [
            questionary.Choice(
                title=f"{c['name']} (~{speed:.0f} tok/s predicted) - {c.get('description', '')}",
                value=ref,
            )
            for (c, speed), ref in zip(viable, refs)
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_recommend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_recommend.py
git commit -m "fix: recommend() builds install refs via install_ref (fixes ModelScope, curated names)"
```

---

### Task 14: `scripts/fetch_hf_candidates.py` → `scripts/fetch_candidates.py`, add ModelScope candidates

**Files:**
- Create: `scripts/fetch_candidates.py` (renamed from `scripts/fetch_hf_candidates.py`, with additions)
- Delete: `scripts/fetch_hf_candidates.py`
- Modify: `.github/workflows/train.yml:38`, `src/omm/search.py:67` (comment), `tests/test_search.py:168` (comment)
- Test: create `tests/test_fetch_candidates.py`

**Interfaces:**
- Produces: `published/candidates.json` entries now carry `"provider": "huggingface"` or `"provider": "modelscope"`; dedupe key changes from `repo_id` to `(provider, repo_id)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fetch_candidates.py`:

```python
"""Tests for scripts/fetch_candidates.py's ModelScope fetch + cross-provider
dedupe logic. Doesn't test fetch_trending_candidates (HF) since that's
unchanged from before this feature and already implicitly covered by
test_search.py's HF-fetching tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_candidates  # noqa: E402


def test_fetch_modelscope_candidates_tags_provider(monkeypatch):
    monkeypatch.setattr(
        fetch_candidates.search_mod,
        "search_modelscope",
        lambda query, **kwargs: [
            {
                "name": "org/repo",
                "repo_id": "org/repo",
                "filename": "model.gguf",
                "description": "ModelScope",
                "provider": "modelscope",
            }
        ],
    )
    candidates = fetch_candidates.fetch_modelscope_candidates()
    assert candidates == [
        {
            "name": "org/repo",
            "repo_id": "org/repo",
            "filename": "model.gguf",
            "description": "ModelScope",
            "provider": "modelscope",
        }
    ]


def test_main_dedupes_by_provider_and_repo_id(monkeypatch, tmp_path):
    hf_candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "a.gguf",
        "description": "HF",
        "provider": "huggingface",
    }
    ms_candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "b.gguf",
        "description": "MS",
        "provider": "modelscope",
    }
    monkeypatch.setattr(fetch_candidates, "curated_candidates", lambda: [])
    monkeypatch.setattr(fetch_candidates, "fetch_trending_candidates", lambda: [hf_candidate])
    monkeypatch.setattr(
        fetch_candidates, "fetch_modelscope_candidates", lambda: [ms_candidate]
    )
    output_path = tmp_path / "candidates.json"
    monkeypatch.setattr(fetch_candidates, "OUTPUT_PATH", output_path)

    fetch_candidates.main()

    import json

    written = json.loads(output_path.read_text())
    # Same repo_id, different provider - both must survive the dedupe.
    assert len(written) == 2
    assert {c["provider"] for c in written} == {"huggingface", "modelscope"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fetch_candidates.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'fetch_candidates'`

- [ ] **Step 3: Create `scripts/fetch_candidates.py`**

```python
"""CI-only script: pull a fresh pool of candidate GGUF models from
HuggingFace and ModelScope so `omm recommend` reflects newly published
models without an omm release. Output feeds into scripts/train_model.py's
artifact."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omm import search as search_mod  # noqa: E402
from omm.featurize import parse_param_count_billions  # noqa: E402
from omm.hub import CURATED_INDEX  # noqa: E402
from omm.linker import sanitize_ollama_tag  # noqa: E402
from omm.search import _claims_fake_provenance, pick_gguf_file  # noqa: E402

HF_SEARCH_URL = "https://huggingface.co/api/models"
CANDIDATE_LIMIT = 30
MODELSCOPE_QUERIES = ["gguf", "instruct gguf", "chat gguf"]
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "published" / "candidates.json"


def fetch_trending_candidates() -> list[dict]:
    resp = requests.get(
        HF_SEARCH_URL,
        params={
            "filter": "gguf",
            "pipeline_tag": "text-generation",
            "sort": "downloads",
            "direction": -1,
            "limit": CANDIDATE_LIMIT,
            "full": "true",
        },
        timeout=30,
    )
    resp.raise_for_status()

    candidates = []
    for model in resp.json():
        if _claims_fake_provenance(model["id"]):
            continue
        filename = pick_gguf_file(model.get("siblings", []))
        if filename is None:
            continue
        # Skip repos whose param count we can't parse from id/filename -
        # they'd otherwise fall back to 0 and get mis-ranked as tiny/fast.
        if parse_param_count_billions(f"{model['id']} {filename}") is None:
            continue
        candidates.append(
            {
                "name": sanitize_ollama_tag(model["id"]),
                "repo_id": model["id"],
                "filename": filename,
                "description": f"{model.get('downloads', 0):,} downloads on HuggingFace",
                "provider": "huggingface",
            }
        )
    return candidates


def fetch_modelscope_candidates() -> list[dict]:
    """Same idea as fetch_trending_candidates but for ModelScope - queries
    a small fixed set of GGUF-flavored search terms since ModelScope's
    search API (unlike HF's) has no "sort by downloads with a gguf filter
    and get everything in one call" shape; results across queries are
    deduped by the caller (main())."""
    candidates: list[dict] = []
    for query in MODELSCOPE_QUERIES:
        candidates.extend(search_mod.search_modelscope(query, limit=CANDIDATE_LIMIT))
    return candidates


def curated_candidates() -> list[dict]:
    return [
        {
            "name": name,
            "repo_id": repo_id,
            "filename": filename,
            "description": "Curated default",
            "provider": "huggingface",
        }
        for name, (repo_id, filename) in CURATED_INDEX.items()
    ]


def main() -> None:
    try:
        trending = fetch_trending_candidates()
    except requests.RequestException as e:
        print(f"Warning: HF fetch failed ({e}), using curated candidates only.")
        trending = []

    try:
        modelscope_candidates = fetch_modelscope_candidates()
    except requests.RequestException as e:
        print(f"Warning: ModelScope fetch failed ({e}), skipping.")
        modelscope_candidates = []

    seen_keys: set[tuple[str, str]] = set()
    candidates = []
    for c in curated_candidates() + trending + modelscope_candidates:
        key = (c.get("provider") or "huggingface", c["repo_id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(c)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(candidates, indent=2))
    print(
        f"Wrote {OUTPUT_PATH} ({len(candidates)} candidates, {len(trending)} from HF trending, "
        f"{len(modelscope_candidates)} from ModelScope)"
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Delete the old script**

```bash
git rm scripts/fetch_hf_candidates.py
```

- [ ] **Step 5: Update the CI workflow reference**

In `.github/workflows/train.yml`, change:

```yaml
        run: python scripts/fetch_hf_candidates.py
```

to:

```yaml
        run: python scripts/fetch_candidates.py
```

- [ ] **Step 6: Update the two stale comment references**

In `src/omm/search.py`, change the comment:

```python
    quant preference as scripts/fetch_hf_candidates.py. Repos almost always
```

to:

```python
    quant preference as scripts/fetch_candidates.py. Repos almost always
```

In `tests/test_search.py`, change the comment:

```python
    # (e.g. from scripts/fetch_hf_candidates.py), never a valid
```

to:

```python
    # (e.g. from scripts/fetch_candidates.py), never a valid
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_fetch_candidates.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add scripts/fetch_candidates.py .github/workflows/train.yml src/omm/search.py tests/test_search.py tests/test_fetch_candidates.py
git commit -m "feat: rename fetch_hf_candidates.py to fetch_candidates.py, add ModelScope candidate fetching"
```

---

### Task 15: Full-suite verification pass

**Files:** None (verification only).

**Interfaces:** None.

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -q`
Expected: All tests PASS. If anything still references `hub.HF_DOWNLOAD`, `cli.HF_DOWNLOAD`, or a 2-argument `remote_file_size`/`remote_file_sha256` call outside the files this plan touched, grep for it (`grep -rn "HF_DOWNLOAD" src tests scripts`) and fix the remaining call site the same way Task 8's fix was applied.

- [ ] **Step 2: Run the linter/formatter if the project has one configured**

Run: `grep -n "\[tool.ruff\]\|\[tool.black\]\|\[tool.flake8\]" pyproject.toml setup.cfg 2>/dev/null` to check what's configured, then run whichever tool is present (e.g. `ruff check src tests scripts` and/or `ruff format --check src tests scripts`) and fix any reported issues.

- [ ] **Step 3: Manually verify the new install path end-to-end against the real ModelScope API (not mocked)**

Run: `python -c "
import sys
sys.path.insert(0, 'src')
from omm.hub import resolve_model
resolved = resolve_model('ms:Qwen/Qwen2.5-0.5B-Instruct-GGUF:qwen2.5-0.5b-instruct-q4_k_m.gguf')
print(resolved)
"`
Expected: Prints a `ResolvedModel` with `provider='modelscope'` and a `url` starting with `https://modelscope.cn/api/v1/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/repo?Revision=master&FilePath=`. This is a live network call - if it fails, verify the ModelScope API hasn't changed shape since the spec was written (re-run the `curl` commands from the spec doc to compare).

- [ ] **Step 4: Manually verify a real (small) parallel download picks up the Range fix**

Run: `python -c "
import sys
sys.path.insert(0, 'src')
from pathlib import Path
from omm.downloader import download_file
download_file(
    'https://modelscope.cn/api/v1/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/repo?Revision=master&FilePath=qwen2.5-0.5b-instruct-q2_k.gguf',
    Path('/tmp/ms-test-download.gguf'),
)
print(Path('/tmp/ms-test-download.gguf').stat().st_size)
"`
Expected: Downloads successfully (progress bar renders, ~415MB file per the earlier `curl` size check) and prints a matching byte count. Delete `/tmp/ms-test-download.gguf` afterward. If it silently falls back to single-stream every time, re-check Task 5's `_probe_range_support` fix against a fresh `curl -H "Range: bytes=0-0" -D -` of this exact URL to confirm the response shape hasn't drifted from what the fix expects.

- [ ] **Step 5: No commit for this task** (verification only, nothing to stage).
