"""Bounded, signed measured-quality evidence for exact model packages."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re

from omm import catalog, compare, config


MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_EVALUATIONS = 4096
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")


class QualityCatalogError(ValueError):
    pass


def _short_text(value: object, label: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise QualityCatalogError(f"quality {label} is missing or invalid")
    return value.strip()


def build_index(document: object) -> dict[tuple[str, str, str], list[compare.QualityEvidence]]:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise QualityCatalogError("unsupported quality catalog schema")
    _short_text(document.get("generated_at"), "generated_at", 50)
    evaluations = document.get("evaluations")
    if not isinstance(evaluations, list) or len(evaluations) > MAX_EVALUATIONS:
        raise QualityCatalogError("quality evaluations must be a bounded list")
    result: dict[tuple[str, str, str], list[compare.QualityEvidence]] = {}
    seen = set()
    for item in evaluations:
        if not isinstance(item, dict):
            raise QualityCatalogError("quality evaluation rows must be objects")
        provider = _short_text(item.get("provider"), "provider", 32).casefold()
        if provider not in {"huggingface", "modelscope"}:
            raise QualityCatalogError("unsupported quality provider")
        repo = _short_text(item.get("repo_id"), "repo_id", 200)
        if not _REPO_RE.fullmatch(repo):
            raise QualityCatalogError("quality repo_id is invalid")
        filename = _short_text(item.get("filename"), "filename", 300)
        if "/" in filename or "\\" in filename:
            raise QualityCatalogError("quality filename must be a basename")
        digest = _short_text(item.get("model_digest"), "model_digest", 64).casefold()
        if not _DIGEST_RE.fullmatch(digest):
            raise QualityCatalogError("quality model_digest must be sha256")
        task = _short_text(item.get("task"), "task", 32)
        if task not in compare.PURPOSES:
            raise QualityCatalogError("unsupported measured quality task")
        pack_id = _short_text(item.get("pack_id"), "pack_id", 100)
        pack_version = _short_text(item.get("pack_version"), "pack_version", 32)
        summary = _short_text(item.get("summary"), "summary", 200)
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise QualityCatalogError("quality score must be from 0 to 1")
        if item.get("source") != "official":
            raise QualityCatalogError("only official quality rows may enter the signed catalog")
        key = (provider, repo.casefold(), filename.casefold())
        duplicate = (*key, task.casefold())
        if duplicate in seen:
            raise QualityCatalogError("duplicate package/task quality row")
        seen.add(duplicate)
        result.setdefault(key, []).append(
            compare.QualityEvidence(
                task,
                pack_id,
                pack_version,
                float(score),
                summary,
                source="Signed OMM quality catalog",
                model_digest=digest,
            )
        )
    return result


def load_signed(content: bytes, manifest: object, public_key: str) -> dict[tuple[str, str, str], list[compare.QualityEvidence]]:
    if len(content) > MAX_ARTIFACT_BYTES:
        raise QualityCatalogError("quality catalog is too large")
    try:
        catalog.verify_signed_artifact(content, manifest, public_key)
        document = json.loads(content)
    except (catalog.CatalogVerificationError, UnicodeError, ValueError, TypeError) as error:
        raise QualityCatalogError(f"quality catalog verification failed: {error}") from error
    return build_index(document)


def load_cached(
    public_key: str | None,
    *,
    artifact_path: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[tuple[str, str, str], list[compare.QualityEvidence]]:
    """Fail closed to no measured evidence when cache or trust is unavailable."""

    if not isinstance(public_key, str) or not public_key:
        return {}
    artifact = artifact_path or config.QUALITY_CATALOG_PATH
    manifest_file = manifest_path or config.QUALITY_MANIFEST_PATH
    try:
        content = artifact.read_bytes()
        if len(content) > MAX_ARTIFACT_BYTES:
            return {}
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        return load_signed(content, manifest, public_key)
    except (OSError, QualityCatalogError, ValueError):
        return {}
