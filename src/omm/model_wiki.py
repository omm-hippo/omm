"""Pinned checkpoint descriptions shared with the existing OMM website."""
from __future__ import annotations

from functools import lru_cache
from datetime import date, datetime, timezone
import json
from pathlib import Path


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(Path(__file__).with_name("model-wiki.json").read_text(encoding="utf-8"))


def localize(model: dict, locale="ko") -> dict:
    locale = locale if locale in {"ko", "en"} else "ko"
    result = dict(model)
    for field in ("summary", "chooseWhen", "runtimeNote"):
        result[field] = model[field][locale]
    for field in ("strengths", "cautions"):
        result[field] = [{**claim, "text": claim["text"][locale]} for claim in model[field]]
    return result


def is_stale(reviewed: str) -> bool:
    try:
        return (datetime.now(timezone.utc).date() - date.fromisoformat(reviewed)).days > 30
    except (TypeError, ValueError):
        return True


def catalog(locale="ko") -> dict:
    source = load()
    return {**source, "locale": locale, "stale": is_stale(source["reviewedAt"]),
            "models": [localize(model, locale) for model in source["models"]]}


def describe(candidate: dict, locale="ko") -> dict | None:
    repository = candidate.get("repo_id")
    if not isinstance(repository, str):
        return None
    links = json.loads(Path(__file__).with_name("model-wiki-matches.json").read_text(encoding="utf-8"))["matches"]
    base = next((value for key, value in links.items() if key.casefold() == repository.casefold()), None)
    target = base["repository"] if base else repository
    matches = [model for model in load()["models"] if model["repository"].casefold() == target.casefold()]
    if len(matches) != 1:
        return None
    source = load()
    return {**localize(matches[0], locale), "contentVersion": source["contentVersion"],
            "match": "declared_base_repository" if base else "exact_repository",
            "packageRepository": repository, "matchSource": base,
            "stale": is_stale(matches[0]["reviewedAt"])}
