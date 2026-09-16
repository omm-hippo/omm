"""Bounded provider facts for old catalogs, stored apart from signed bytes.

Only explicit data refreshes contact providers. Ordinary recommendations read
this cache without extra provider requests. Facts match provider/repo/file;
they describe declared tasks and file sizes, never measured runtime behavior.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

from omm import config, predictor
from omm.atomic import atomic_write_text, locked
from omm.featurize import positive_finite_number
from omm.httpjson import read_bounded_json_response
from omm.recommend_metadata import catalog_metadata
from omm.recommend_selection import shortlist

MAX_BYTES = 2 * 1024 * 1024
TTL_SECONDS = 24 * 60 * 60
MAX_REPOS = 32


def _path():
    return config.OMM_HOME / "recommend-provider-facts.json"


def _key(candidate: dict) -> str | None:
    provider = candidate.get("provider") or "huggingface"
    repo = candidate.get("repo_id")
    if provider not in {"huggingface", "modelscope"} or not isinstance(repo, str):
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        return None
    return f"{provider}:{repo}"


def load() -> dict:
    try:
        path = _path()
        if path.stat().st_size > MAX_BYTES:
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
        records = document.get("repos") if document.get("version") == 1 else None
        if not isinstance(records, dict) or len(records) > 256:
            return {}
        now = time.time()
        return {
            key: record for key, record in records.items()
            if isinstance(record, dict)
            and (stamp := positive_finite_number(record.get("fetched_at"))) is not None
            and 0 <= now - stamp
        }
    except (OSError, ValueError, AttributeError):
        return {}


def apply(artifact: dict, records: dict | None = None) -> dict:
    records = load() if records is None else records
    candidates = []
    for original in artifact.get("candidates", []):
        candidate = dict(original)
        record = records.get(_key(candidate), {})
        filenames = record.get("files", {})
        filename = candidate.get("filename")
        # Do not transfer a repository's facts to a different/missing file.
        if isinstance(filenames, dict) and filename in filenames:
            candidate.update(catalog_metadata(record.get("metadata", {})))
            stamp = positive_finite_number(record.get("fetched_at"))
            candidate["metadata_origin"] = (
                "Provider metadata cache (stale)"
                if stamp is not None and time.time() - stamp > TTL_SECONDS
                else "Provider metadata cache"
            )
            size = positive_finite_number(filenames[filename])
            if size is not None:
                candidate["size_bytes"] = size
        candidates.append(candidate)
    return {**artifact, "candidates": candidates}


def _get(url: str, deadline: float, *, params: dict | None = None) -> dict:
    import requests

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("provider metadata refresh budget reached")
    response = requests.get(url, params=params, timeout=min(8, remaining), stream=True, allow_redirects=False)
    try:
        response.raise_for_status()
    except requests.RequestException:
        response.close()
        raise
    payload, _ = read_bounded_json_response(response, maximum=MAX_BYTES, label="recommendation provider facts")
    if not isinstance(payload, dict):
        raise ValueError("provider facts must be an object")
    return payload


def fetch(provider: str, repo: str, filenames: set[str], deadline: float) -> dict:
    encoded = "/".join(quote(part, safe="") for part in repo.split("/"))
    if provider == "huggingface":
        payload = _get(f"https://huggingface.co/api/models/{encoded}", deadline, params={"blobs": "true"})
        if payload.get("id") != repo:
            raise ValueError("Hugging Face returned a different repository")
        metadata = catalog_metadata(payload)
        siblings = payload.get("siblings", [])
        if not isinstance(siblings, list):
            raise ValueError("invalid file listing")
        files = {item["rfilename"]: item.get("size") for item in siblings
                 if isinstance(item, dict) and item.get("rfilename") in filenames}
    else:
        payload = _get(f"https://modelscope.cn/api/v1/models/{encoded}", deadline)
        data = payload.get("Data")
        if not isinstance(data, dict) or payload.get("Success") is False:
            raise ValueError("invalid ModelScope metadata")
        # These are provider task declarations, not model-name inference.
        tasks = data.get("Tasks", [])
        task_tags = [task.get("Name") for task in tasks[:64] if isinstance(task, dict)] if isinstance(tasks, list) else []
        tags = data.get("Tags", [])
        metadata = catalog_metadata({"tags": (tags[:64] if isinstance(tags, list) else []) + task_tags})
        listing = _get(f"https://modelscope.cn/api/v1/models/{encoded}/repo/files", deadline,
                       params={"Revision": "master", "Recursive": "True"})
        listed = listing.get("Data")
        if not isinstance(listed, dict) or not isinstance(listed.get("Files"), list):
            raise ValueError("invalid ModelScope file listing")
        files = {item["Path"]: item.get("Size") for item in listed["Files"]
                 if isinstance(item, dict) and item.get("Path") in filenames}
    return {"metadata": metadata, "files": files, "fetched_at": time.time()}


def _preferred(artifact: dict, hw) -> list[dict]:
    ranked = [(c, s) for c, s in predictor.rank_candidates(artifact, hw) if s >= predictor.MIN_USABLE_TOKENS_PER_SECOND]
    ordered = []
    # Cover the tightest budget first, then fill the larger profiles.
    for profile in ("minimal", "balanced", "dedicated"):
        pool = predictor.filter_by_profile(ranked, hw, profile)
        pool.sort(key=lambda pair: predictor.estimate_required_memory_gb(pair[0]) or 0, reverse=True)
        ordered.extend(c for c, _ in shortlist(pool))
    return ordered


def refresh(artifact: dict, hw, *, max_repos: int = MAX_REPOS, seconds: float = 60) -> dict:
    """At most 32 repos / 64 metadata GETs, sequential, without retries.

    Re-ranking after each lookup allows replacements for newly oversized
    packages to receive facts too. A rate limit stops the whole refresh.
    """
    import requests

    records = load()
    fetched = {}
    attempted = set()
    deadline = time.monotonic() + seconds
    error = None
    while len(attempted) < min(MAX_REPOS, max_repos) and time.monotonic() < deadline:
        pending = [c for c in _preferred(apply(artifact, records), hw)
                   if (key := _key(c)) is not None and key not in attempted
                   and (key not in records or time.time() - records[key]["fetched_at"] > TTL_SECONDS
                        or not isinstance(records[key].get("files"), dict)
                        or c.get("filename") not in records[key]["files"])]
        if not pending:
            break
        key = _key(pending[0])
        attempted.add(key)
        provider, repo = key.split(":", 1)
        filenames = {c["filename"] for c in artifact["candidates"] if _key(c) == key}
        try:
            record = fetch(provider, repo, filenames, deadline)
            fetched[key] = records[key] = record
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            error = f"provider HTTP {status}"
            if status in {402, 429}:
                break
        except (requests.RequestException, ValueError, TimeoutError) as exc:
            error = type(exc).__name__
            break
    if fetched:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked(path):
            merged = {**load(), **fetched}
            merged = dict(sorted(merged.items(), key=lambda item: item[1]["fetched_at"], reverse=True)[:256])
            atomic_write_text(path, json.dumps({"version": 1, "repos": merged}) + "\n")
    return {"fetched_repos": len(fetched), "attempted_repos": len(attempted), "error": error}
