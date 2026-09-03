"""HuggingFace provider: resolves repo file listings and download URLs via
the public HF Hub REST API. Logic moved verbatim from the old hub.py - see
git history for prior behavior if something looks unfamiliar."""

from __future__ import annotations

import math
from urllib.parse import quote

from omm.providers.base import ModelResolutionError, normalize_sha256

HF_API = "https://huggingface.co/api/models/{repo_id}"
HF_DOWNLOAD = "https://huggingface.co/{repo_id}/resolve/main/{filename}"
HF_PATHS_INFO = "https://huggingface.co/api/models/{repo_id}/paths-info/main"


def fetch_repo_files(repo_id: str) -> tuple[list[str], float | None]:
    """List of .gguf filenames plus a repo-level param count fallback, in
    billions - HF parses this straight out of the GGUF header itself
    (response key "gguf.total") whether or not the filename spells it out,
    so it covers names like "ID_Legal_Assistant_Q8_0.gguf" that carry a
    quant tag but no param count."""
    import requests

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

    try:
        payload = resp.json()
        siblings = payload.get("siblings", [])
        filenames = [s["rfilename"] for s in siblings]
        if any(not isinstance(filename, str) for filename in filenames):
            raise TypeError("repository filename is not a string")
        files = [filename for filename in filenames if filename.lower().endswith(".gguf")]
        param_count_b = _parse_gguf_total_params(payload)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ModelResolutionError(f"HF API returned an unexpected response for '{repo_id}': {e}") from e
    return files, param_count_b


def _parse_gguf_total_params(payload: dict) -> float | None:
    gguf = payload.get("gguf", {})
    if not isinstance(gguf, dict):
        return None
    total_params = gguf.get("total")
    if (
        isinstance(total_params, (int, float))
        and not isinstance(total_params, bool)
        and math.isfinite(total_params)
        and total_params > 0
    ):
        return total_params / 1e9
    return None


def fetch_repo_param_count_b(repo_id: str) -> float | None:
    """Best-effort repo-level parameter count (billions), for callers that
    only have a repo id and filename and whose filename doesn't spell out
    the count. Never raises - used to decide whether to flag a search
    result as unviable, not to resolve an install."""
    import requests

    try:
        resp = requests.get(HF_API.format(repo_id=repo_id), timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    try:
        return _parse_gguf_total_params(resp.json())
    except (ValueError, TypeError, AttributeError):
        return None


def download_url(repo_id: str, filename: str) -> str:
    return HF_DOWNLOAD.format(repo_id=repo_id, filename=quote(filename, safe="/"))


def remote_file_sha256(repo_id: str, filename: str) -> str | None:
    """Current LFS sha256 of `filename` in `repo_id`'s main branch, via HF's
    paths-info API. Returns None if the request fails, the file isn't
    listed, or it isn't stored as LFS."""
    import requests

    try:
        resp = requests.post(
            HF_PATHS_INFO.format(repo_id=repo_id),
            json={"paths": [filename]},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    try:
        entries = resp.json()
        if not entries:
            return None
        lfs = entries[0].get("lfs", {})
        return normalize_sha256(lfs.get("oid") or lfs.get("sha256"))
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None


def remote_file_size(repo_id: str, filename: str) -> int | None:
    """Best-effort Hub file size without downloading the GGUF."""
    import requests

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
