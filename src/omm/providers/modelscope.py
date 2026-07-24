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
