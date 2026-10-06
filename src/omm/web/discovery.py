"""Explicit bounded package lookup, using exact publisher/base-model identity."""
from __future__ import annotations

from urllib.parse import quote
import re

from omm import hub, model_wiki


def discover(model: dict) -> list[dict]:
    import requests
    repository = model["repository"]
    trusted = {repository.casefold()}
    trusted.update(key.casefold() for key, item in model_wiki.repository_matches().items()
                   if item["repository"].casefold() == repository.casefold())
    with requests.Session() as client:
        response = client.get("https://huggingface.co/api/models", params={
            "search": repository.rsplit("/", 1)[-1], "filter": "gguf", "limit": 8, "full": "true"},
            timeout=8, allow_redirects=False)
        response.raise_for_status()
        results = response.json()
        if not isinstance(results, list):
            raise ValueError("모델 검색 응답을 확인할 수 없어요.")
        repos = set()
        for item in results[:8]:
            if not isinstance(item, dict):
                continue
            card = item.get("cardData") or {}
            bases = card.get("base_model", []) if isinstance(card, dict) else []
            bases = [bases] if isinstance(bases, str) else bases
            rid = item.get("id")
            if isinstance(rid, str) and (rid.casefold() in trusted or
                    isinstance(bases, list) and any(isinstance(x, str) and x.casefold() == repository.casefold() for x in bases)):
                repos.add(rid)
        # Pinned reviewed repositories remain discoverable when search omits them.
        repos.update(key for key, item in model_wiki.repository_matches().items() if item["repository"] == repository)
        packages = []
        for rid in sorted(repos)[:8]:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", rid):
                continue
            response = client.get(f"https://huggingface.co/api/models/{quote(rid, safe='/')}",
                                  params={"blobs": "true"}, timeout=8, allow_redirects=False)
            response.raise_for_status()
            data = response.json()
            card = data.get("cardData") or {}
            bases = card.get("base_model", []) if isinstance(card, dict) else []
            bases = [bases] if isinstance(bases, str) else bases
            if rid.casefold() not in trusted and not (isinstance(bases, list) and repository in bases):
                continue
            for file in data.get("siblings", []):
                name = file.get("rfilename", "")
                if not isinstance(name, str) or not name.lower().endswith(".gguf"):
                    continue
                # Split shards cannot be installed as a standalone single-file package.
                if re.search(r"-\d{5}-of-\d{5}\.gguf$", name, re.I):
                    continue
                try:
                    hub.validate_model_filename(name)
                except (ValueError, hub.ModelResolutionError):
                    continue
                size = file.get("size") or (file.get("lfs") or {}).get("size")
                if type(size) is not int or size <= 0:
                    continue
                description = model_wiki.describe({"repo_id": rid}) or {
                    **model, "match": "publisher_declared_base", "packageRepository": rid,
                    "matchSource": {"source": response.url, "repository": repository}, "stale": model_wiki.is_stale(model["reviewedAt"])}
                packages.append({"repo_id": rid, "filename": name, "size_bytes": size,
                                 "provider": "huggingface", "wiki": description})
        return packages[:100]
