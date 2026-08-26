"""CI-only script: pull a fresh pool of candidate GGUF models from
HuggingFace and ModelScope so `omm recommend` reflects newly published
models without an omm release. Output feeds into scripts/train_model.py's
artifact."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omm import search as search_mod  # noqa: E402
from omm.atomic import atomic_write_text, locked  # noqa: E402
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

    payload = resp.json()
    if not isinstance(payload, list):
        raise ValueError("HuggingFace search response must be a list")

    candidates = []
    for model in payload:
        if not isinstance(model, dict):
            continue
        repo_id = model.get("id")
        siblings = model.get("siblings", [])
        if not isinstance(repo_id, str) or not repo_id or not isinstance(siblings, list):
            continue
        if _claims_fake_provenance(repo_id):
            continue
        filename = pick_gguf_file(siblings)
        if filename is None:
            continue
        # Skip repos whose param count we can't parse from id/filename -
        # they'd otherwise fall back to 0 and get mis-ranked as tiny/fast.
        if parse_param_count_billions(f"{repo_id} {filename}") is None:
            continue
        downloads = model.get("downloads", 0)
        if (
            isinstance(downloads, bool)
            or not isinstance(downloads, int)
            or downloads < 0
        ):
            downloads = 0
        candidates.append(
            {
                "name": sanitize_ollama_tag(repo_id),
                "repo_id": repo_id,
                "filename": filename,
                "description": f"{downloads:,} downloads on HuggingFace",
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
    # HF and ModelScope are independent network sources - fetch concurrently.
    with ThreadPoolExecutor(max_workers=2) as executor:
        trending_future = executor.submit(fetch_trending_candidates)
        modelscope_future = executor.submit(fetch_modelscope_candidates)

        try:
            trending = trending_future.result()
        except (requests.RequestException, TypeError, ValueError) as e:
            print(f"Warning: HF fetch failed ({e}), using curated candidates only.")
            trending = []

        try:
            modelscope_candidates = modelscope_future.result()
        except (requests.RequestException, TypeError, ValueError) as e:
            print(f"Warning: ModelScope fetch failed ({e}), skipping.")
            modelscope_candidates = []

    seen_keys: set[tuple[str, str]] = set()
    candidates = []
    for c in curated_candidates() + trending + modelscope_candidates:
        if not isinstance(c, dict):
            print("Warning: skipping a malformed non-object candidate.")
            continue
        repo_id = c.get("repo_id")
        filename = c.get("filename")
        name = c.get("name")
        description = c.get("description")
        provider = c.get("provider") or "huggingface"
        if (
            not isinstance(repo_id, str)
            or not repo_id
            or not isinstance(filename, str)
            or not filename
            or not isinstance(name, str)
            or not name
            or (description is not None and not isinstance(description, str))
            or provider not in {"huggingface", "modelscope"}
        ):
            print("Warning: skipping a candidate with invalid coordinates or provider.")
            continue
        key = (provider, repo_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(c)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with locked(OUTPUT_PATH):
        atomic_write_text(OUTPUT_PATH, json.dumps(candidates, indent=2) + "\n")
    print(
        f"Wrote {OUTPUT_PATH} ({len(candidates)} candidates, {len(trending)} from HF trending, "
        f"{len(modelscope_candidates)} from ModelScope)"
    )


if __name__ == "__main__":
    main()
