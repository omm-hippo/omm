"""Model search: local curated/cached candidates + live HuggingFace search,
merged, fuzzy-matched, and grouped by model family for `omm search` and for
"did you mean" suggestions on a failed `omm install`.
"""

from __future__ import annotations

import difflib
import re
from concurrent.futures import ThreadPoolExecutor

from omm import hub, predictor
from omm.featurize import is_mmproj_filename
from omm.providers import modelscope

HF_SEARCH_API = "https://huggingface.co/api/models"

FAMILY_KEYWORDS: list[str] = [
    "TinyLlama",
    "Llama",
    "Mistral",
    "Mixtral",
    "Qwen",
    "Gemma",
    "Phi",
    "DeepSeek",
    "StableLM",
    "Falcon",
    "Yi",
]

# HF is full of spam repos claiming to be "distilled" or "fine-tuned" from
# closed, never-publicly-released weights (Claude/Opus, GPT-4/5, Gemini, ...).
# That's not a real technique - those weights were never downloadable, so the
# claim is fabricated. These repos exist to farm downloads/likes off famous
# names and ship broken or nonstandard GGUFs (fake architecture tags, garbage
# quantizations) that fail to load once a user actually installs them. Filter
# them out before they ever reach a suggestion.
FAKE_PROVENANCE_MARKERS: list[str] = [
    "claude",
    "anthropic",
    "opus",
    "sonnet-4",
    "sonnet4",
    "chatgpt",
    "gpt-4",
    "gpt4",
    "gpt-5",
    "gpt5",
    "gemini",
    "bard",
]


def _claims_fake_provenance(text: str) -> bool:
    lowered = text.lower()
    return any(
        re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in FAKE_PROVENANCE_MARKERS
    )


_SHARD_RE = re.compile(r"-\d{5}-of-\d{5}")
_PREFERRED_QUANT_RE = re.compile(r"Q4_K_M", re.IGNORECASE)


def pick_gguf_file(siblings: list[dict]) -> str | None:
    """Pick one concrete .gguf filename out of a repo's file listing, same
    quant preference as scripts/fetch_candidates.py. Repos almost always
    ship multiple quants, and `omm install org/repo` (no filename) refuses to
    guess - so any name we print for the user to copy-paste needs a concrete
    filename attached or it just fails with "specify one".
    """
    gguf_files = []
    for sibling in siblings:
        if not isinstance(sibling, dict):
            continue
        filename = sibling.get("rfilename")
        if (
            isinstance(filename, str)
            and filename.lower().endswith(".gguf")
            and not _SHARD_RE.search(filename)
            and not is_mmproj_filename(filename)
        ):
            gguf_files.append(filename)
    if not gguf_files:
        return None
    preferred = [f for f in gguf_files if _PREFERRED_QUANT_RE.search(f)]
    return preferred[0] if preferred else gguf_files[0]


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
    prefix = _PROVIDER_PREFIX.get(provider)
    return f"{prefix}:{repo_id}" if prefix else repo_id


def exact_install_ref(candidate: dict) -> str:
    """Like install_ref(), but resolves straight to the specific .gguf file
    already priced into this candidate's speed/memory prediction, instead of
    a bare repo id. `omm recommend` rows are already quant-specific - handing
    install_ref()'s bare repo to `omm install` would trigger its
    AmbiguousModelError quant picker a second time, and that picker could
    land on a different quant than the one just shown to the user.
    """
    name = candidate.get("name")
    if name and name in hub.CURATED_INDEX:
        return name
    repo_id = candidate.get("repo_id") or name or ""
    filename = candidate.get("filename")
    if not repo_id or not filename:
        return install_ref(candidate)
    provider = candidate.get("provider") or "huggingface"
    if provider == "huggingface":
        return f"{repo_id}:{filename}"
    prefix = _PROVIDER_PREFIX.get(provider)
    return f"{prefix}:{repo_id}:{filename}" if prefix else install_ref(candidate)


_PROVIDER_PREFIX = {"modelscope": "ms"}


# Matches a quant token as its own hyphen-delimited path segment (e.g. the
# "-Q4_K_M" in ".../Model-Q4_K_M-GGUF"). Many uploaders publish one repo per
# quant level instead of one repo with several files, so without this,
# quant-in-repo-name uploads (e.g. 9 "roleplaiapp/Model-Q4_0-GGUF",
# "...-Q5_K_M-GGUF", ...) show up as 9 unrelated search results.
_QUANT_TOKEN_RE = re.compile(
    r"-(?:IQ[1-4]_(?:XXS|XS|S|M|NL)|Q[2-8](?:_[01])?(?:_K(?:_[SML])?)?|BF16|FP16|FP32|F16|F32)"
    r"(?=-|$)",
    re.IGNORECASE,
)


def _base_repo_key(repo_id: str) -> str:
    """Normalize a repo id by stripping an embedded quant token, so
    per-quant repos from the same uploader collapse to one dedupe key."""
    return _QUANT_TOKEN_RE.sub("", repo_id).lower()


def dedupe_by_base_repo(candidates: list[dict]) -> list[dict]:
    """Collapse candidates that are really the same base model - either the
    exact same repo, or per-quant sibling repos from the same uploader
    (quant baked into the repo name rather than the filename) - down to one
    representative each, first-seen order preserved."""
    seen: set[str] = set()
    deduped: list[dict] = []
    for c in candidates:
        repo_id = c.get("repo_id")
        key = _base_repo_key(repo_id) if repo_id else _label(c).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


def guess_family(text: str) -> str:
    for family in FAMILY_KEYWORDS:
        if re.search(rf"\b{re.escape(family)}\b", text, re.IGNORECASE):
            return family
    return "Other"


def _label(candidate: dict) -> str:
    return candidate.get("name") or candidate.get("repo_id") or ""


def _curated_as_candidates() -> list[dict]:
    return [
        {
            "name": name,
            "repo_id": repo_id,
            "filename": filename,
            "description": "Curated default",
        }
        for name, (repo_id, filename) in hub.CURATED_INDEX.items()
    ]


def local_candidate_pool(
    model_url: str | None,
    manifest_url: str | None = None,
    public_key: str | None = None,
) -> list[dict]:
    pool = _curated_as_candidates()
    artifact = predictor.load_model(model_url, manifest_url, public_key)
    if artifact:
        pool.extend(artifact.get("candidates", []))

    seen: set[tuple[str | None, str | None]] = set()
    deduped: list[dict] = []
    for candidate in pool:
        key = (candidate.get("repo_id"), candidate.get("filename"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def search_huggingface(query: str, limit: int = 20, timeout: float = 3.0) -> list[dict]:
    import requests

    try:
        resp = requests.get(
            HF_SEARCH_API,
            params={"search": query, "filter": "gguf", "limit": limit, "full": "true"},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return []

    results = []
    if not isinstance(payload, list):
        return []
    for item in payload:
        if not isinstance(item, dict):
            continue
        repo_id = item.get("id") or item.get("modelId")
        if not isinstance(repo_id, str) or not repo_id:
            continue
        if _claims_fake_provenance(repo_id):
            continue
        filename = pick_gguf_file(item.get("siblings", []))
        if filename is None:
            continue
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


MS_SEARCH_API = "https://modelscope.cn/openapi/v1/models"


def search_modelscope(query: str, limit: int = 20, timeout: float = 3.0) -> list[dict]:
    import requests

    if limit <= 0:
        return []

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

    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    models = data.get("models", [])
    if not isinstance(models, list):
        return []
    gguf_tagged = [
        model
        for model in models
        if isinstance(model, dict)
        and isinstance(model.get("tags"), list)
        and "library:gguf" in model["tags"]
    ]

    candidates = [
        item
        for item in gguf_tagged[: min(limit, 15)]
        if isinstance(item.get("id"), str)
        and item["id"]
        and not _claims_fake_provenance(item["id"])
    ]
    if not candidates:
        return []

    def _resolve(item: dict) -> dict | None:
        try:
            repo_id = item["id"]
            files, _ = modelscope.fetch_repo_files(repo_id, timeout=timeout)
            filename = pick_gguf_file([{"rfilename": f} for f in files])
            if filename is None:
                return None
            downloads = item.get("downloads", 0)
            downloads_text = (
                f"{downloads:,}"
                if isinstance(downloads, (int, float)) and not isinstance(downloads, bool)
                else "0"
            )
            return {
                "name": repo_id,
                "repo_id": repo_id,
                "filename": filename,
                "description": f"{downloads_text} downloads on ModelScope",
                "provider": "modelscope",
            }
        except Exception:  # noqa: BLE001 - a single malformed repo shouldn't kill the search
            return None

    # Each repo needs its own file-listing call - ModelScope's list API has
    # no HF-style full=true that inlines siblings - but the calls are
    # independent, so fan them out instead of paying N sequential round
    # trips (this alone made `omm search` take ~15s).
    with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
        resolved = executor.map(_resolve, candidates)
        return [r for r in resolved if r is not None]


def match_candidates(pool: list[dict], query: str) -> list[dict]:
    q = query.lower()
    substring = [
        c
        for c in pool
        if q in _label(c).lower() or q in (c.get("repo_id") or "").lower()
    ]
    if substring:
        return substring

    labels = [_label(c) for c in pool]
    close = difflib.get_close_matches(query, labels, n=10, cutoff=0.4)
    by_label: dict[str, dict] = {}
    for candidate in pool:
        by_label.setdefault(_label(candidate), candidate)
    return [by_label[label] for label in close if label in by_label]


def suggest_similar(query: str, pool: list[dict], limit: int = 3) -> list[dict]:
    labels = [_label(c) for c in pool]
    close = difflib.get_close_matches(query, labels, n=limit, cutoff=0.3)

    by_label: dict[str, dict] = {}
    for candidate in pool:
        by_label.setdefault(_label(candidate), candidate)
    return [by_label[label] for label in close if label in by_label]


def group_by_family(candidates: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        text = f"{_label(c)} {c.get('repo_id') or ''}"
        family = guess_family(text)
        groups.setdefault(family, []).append(c)
    return groups
