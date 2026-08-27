"""ModelScope provider: resolves repo file listings and download URLs via
the public ModelScope Hub API (https://modelscope.cn). No auth needed for
public repos - confirmed with live curl requests (see
docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md).

ModelScope's download endpoint honors Range requests but returns HTTP 200
instead of 206 for a partial response (confirmed live) - see
downloader.py's _probe_range_support for the corresponding fix."""

from __future__ import annotations

from functools import lru_cache
import re
from urllib.parse import quote_plus

from omm.providers.base import ModelResolutionError

MS_REPO_FILES = "https://modelscope.cn/api/v1/models/{repo_id}/repo/files"
MS_DOWNLOAD = "https://modelscope.cn/api/v1/models/{repo_id}/repo"


@lru_cache(maxsize=128)
def _list_repo_files(repo_id: str, timeout: float = 15) -> list[dict]:
    """Cached for the process lifetime: fetch_repo_files, remote_file_size,
    and remote_file_sha256 all hit this same listing for the same repo_id
    (e.g. once per quant variant when the install prompt is ranking them),
    and the response doesn't change within a single command invocation."""
    import requests

    try:
        resp = requests.get(
            MS_REPO_FILES.format(repo_id=repo_id),
            params={"Revision": "master", "Recursive": "True"},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
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
    except ValueError as e:
        # requests.exceptions.JSONDecodeError subclasses both RequestException
        # and ValueError - this branch must come first or a non-JSON body
        # gets the misleading "could not reach" message instead of this one.
        raise ModelResolutionError(
            f"ModelScope API response was not valid JSON for '{repo_id}': {e}"
        ) from e
    except requests.RequestException as e:
        raise ModelResolutionError(f"Could not reach ModelScope for '{repo_id}': {e}") from e

    if not isinstance(payload, dict):
        raise ModelResolutionError(
            f"ModelScope API returned an unexpected response for '{repo_id}'."
        )
    data = payload.get("Data")
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ModelResolutionError(
            f"ModelScope API returned invalid Data for '{repo_id}'."
        )
    files = data.get("Files", [])
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ModelResolutionError(
            f"ModelScope API returned an invalid file list for '{repo_id}'."
        )
    return files


def fetch_repo_files(repo_id: str, timeout: float = 15) -> tuple[list[str], float | None]:
    files = [
        f["Path"]
        for f in _list_repo_files(repo_id, timeout=timeout)
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
    """Best-effort file size lookup via repo listing. Returns None on any
    failure (404, network error, gated repo, etc.) - never raises. Matches
    the HuggingFace provider's contract for best-effort metadata."""
    try:
        # Must match fetch_repo_files' call shape (repo_id, timeout=15)
        # exactly, or the lru_cache key differs and this misses the cache
        # it exists to hit.
        files = _list_repo_files(repo_id, timeout=15)
    except ModelResolutionError:
        return None
    for f in files:
        if f.get("Path") == filename:
            size = f.get("Size")
            try:
                parsed_size = int(size)
            except (TypeError, ValueError):
                return None
            return parsed_size if parsed_size > 0 else None
    return None


def remote_file_sha256(repo_id: str, filename: str) -> str | None:
    """Best-effort SHA256 lookup via repo listing. Returns None on any
    failure (404, network error, gated repo, etc.) - never raises. Matches
    the HuggingFace provider's contract for best-effort metadata."""
    try:
        # Must match fetch_repo_files' call shape (repo_id, timeout=15)
        # exactly, or the lru_cache key differs and this misses the cache
        # it exists to hit.
        files = _list_repo_files(repo_id, timeout=15)
    except ModelResolutionError:
        return None
    for f in files:
        if f.get("Path") == filename:
            sha = f.get("Sha256")
            if not isinstance(sha, str):
                return None
            digest = sha.removeprefix("sha256:").lower()
            return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None
    return None
