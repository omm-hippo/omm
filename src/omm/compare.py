"""Pure, read-only model comparison logic.

This module deliberately does not download, install, run, or remove models.
It combines the existing hardware predictor with declared metadata and optional
validated quality evidence supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping, Sequence

from omm import predictor, recommend_metadata, recommend_status
from omm.hardware import HardwareInfo
from omm.recommend_selection import model_label, quantization_label, variant_warning
from omm.search import exact_install_ref


PURPOSES = ("General", "Coding", "Reasoning", "Writing", "Translation", "Documents")
MIN_MODELS = 2
MAX_MODELS = 5


class CompareInputError(ValueError):
    """The requested comparison cannot be resolved without guessing."""


@dataclass(frozen=True)
class QualityEvidence:
    task: str
    pack_id: str
    pack_version: str
    score: float
    summary: str
    source: str = "Signed OMM quality catalog"
    model_digest: str | None = None


@dataclass(frozen=True)
class CompareRow:
    candidate: dict
    ref: str
    display_name: str
    predicted_tokens_per_second: float
    memory_required_gb: float | None
    memory_estimate_basis: str
    profile: str
    profile_budget_gb: float
    within_profile: bool | None
    meets_speed_floor: bool
    installed: bool
    managed_by_omm: bool
    installed_engines: tuple[str, ...]
    model_type: str
    declared_purpose: str
    declared_purpose_source: str
    quantization: str
    warning: str | None
    measured_quality: QualityEvidence | None

    @property
    def eligible(self) -> bool:
        return self.meets_speed_floor and self.within_profile is not False and self.warning is None


@dataclass(frozen=True)
class ComparisonResult:
    rows: tuple[CompareRow, ...]
    purpose: str | None
    best_fit_ref: str | None
    fastest_ref: str | None
    best_measured_quality_ref: str | None
    best_match_ref: str | None
    quality_complete: bool


def candidate_key(candidate: Mapping[str, object]) -> tuple[str, str, str]:
    provider = str(candidate.get("provider") or "huggingface").casefold()
    repo = str(candidate.get("repo_id") or candidate.get("name") or "").strip().casefold()
    filename = str(candidate.get("filename") or "").strip().casefold()
    return provider, repo, filename


def _normal(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _aliases(candidate: dict) -> set[str]:
    provider, repo, filename = candidate_key(candidate)
    exact = exact_install_ref(candidate)
    values = {
        candidate.get("name"),
        candidate.get("repo_id"),
        candidate.get("filename"),
        exact,
        f"{repo}:{filename}" if repo and filename else None,
        f"hf:{repo}:{filename}" if provider == "huggingface" and repo and filename else None,
        model_label(str(candidate.get("filename") or candidate.get("repo_id") or "")),
    }
    return {str(value).strip().casefold() for value in values if value}


def resolve_candidates(candidates: Iterable[dict], queries: Sequence[str]) -> list[dict]:
    if not MIN_MODELS <= len(queries) <= MAX_MODELS:
        raise CompareInputError(f"compare requires {MIN_MODELS} to {MAX_MODELS} models")
    pool = [candidate for candidate in candidates if isinstance(candidate, dict)]
    resolved: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_query in queries:
        query = raw_query.strip()
        if not query:
            raise CompareInputError("model references must not be empty")
        folded = query.casefold()
        exact_matches = [candidate for candidate in pool if folded in _aliases(candidate)]
        matches = exact_matches
        if not matches:
            normalized = _normal(query)
            matches = [
                candidate
                for candidate in pool
                if normalized and normalized in {_normal(alias) for alias in _aliases(candidate)}
            ]
        unique = {candidate_key(candidate): candidate for candidate in matches}
        if not unique:
            raise CompareInputError(f"model is not in the signed recommendation catalog: {query}")
        if len(unique) > 1:
            choices = ", ".join(sorted(exact_install_ref(candidate) for candidate in unique.values())[:5])
            raise CompareInputError(f"model reference is ambiguous: {query} (matches: {choices})")
        key, candidate = next(iter(unique.items()))
        if key in seen:
            raise CompareInputError(f"the same model was listed more than once: {query}")
        seen.add(key)
        resolved.append(candidate)
    return resolved


def _quality_for(
    candidate: dict,
    purpose: str | None,
    quality_index: Mapping[tuple[str, str, str], Sequence[QualityEvidence]] | None,
) -> QualityEvidence | None:
    if purpose is None or quality_index is None:
        return None
    evidence = [
        item
        for item in quality_index.get(candidate_key(candidate), ())
        if item.task.casefold() == purpose.casefold()
    ]
    if not evidence:
        return None
    # Multiple pack versions are not mixed. The catalog loader orders and
    # validates versions; prefer its first exact-package observation.
    return evidence[0]


def compare_candidates(
    artifact: dict,
    queries: Sequence[str],
    hardware: HardwareInfo,
    *,
    profile: str = predictor.DEFAULT_RECOMMEND_PROFILE,
    purpose: str | None = None,
    installations: Sequence[recommend_status.InstallationStatus] | None = None,
    quality_index: Mapping[tuple[str, str, str], Sequence[QualityEvidence]] | None = None,
) -> ComparisonResult:
    if profile not in predictor.RECOMMEND_PROFILES:
        raise CompareInputError(
            f"profile must be one of: {', '.join(predictor.RECOMMEND_PROFILES)}"
        )
    if purpose is not None:
        purpose = next((value for value in PURPOSES if value.casefold() == purpose.casefold()), None)
        if purpose is None:
            raise CompareInputError(f"purpose must be one of: {', '.join(PURPOSES)}")
    selected = resolve_candidates(artifact.get("candidates", []), queries)
    ranked = predictor.rank_candidates({**artifact, "candidates": selected}, hardware)
    speed_by_key = {candidate_key(candidate): speed for candidate, speed in ranked}
    if installations is None:
        installations = [recommend_status.NOT_INSTALLED] * len(selected)
    if len(installations) != len(selected):
        raise ValueError("installation status count must match selected candidates")
    budget = predictor.profile_memory_cap_gb(hardware, profile)
    rows: list[CompareRow] = []
    for candidate, installation in zip(selected, installations):
        speed = float(speed_by_key[candidate_key(candidate)])
        memory = predictor.estimate_required_memory_gb(candidate)
        labels = recommend_metadata.classify(candidate)
        rows.append(
            CompareRow(
                candidate=candidate,
                ref=exact_install_ref(candidate),
                display_name=model_label(
                    str(candidate.get("filename") or candidate.get("repo_id") or candidate.get("name") or "Unknown")
                ),
                predicted_tokens_per_second=speed,
                memory_required_gb=memory,
                memory_estimate_basis=predictor.memory_estimate_basis(candidate),
                profile=profile,
                profile_budget_gb=budget,
                within_profile=(memory <= budget if memory is not None else None),
                meets_speed_floor=speed >= predictor.MIN_USABLE_TOKENS_PER_SECOND,
                installed=installation.installed,
                managed_by_omm=installation.managed_by_omm,
                installed_engines=installation.engines,
                model_type=labels.model_type,
                declared_purpose=labels.use_case,
                declared_purpose_source=labels.use_case_source,
                quantization=quantization_label(candidate),
                warning=variant_warning(candidate),
                measured_quality=_quality_for(candidate, purpose, quality_index),
            )
        )

    eligible = [row for row in rows if row.eligible]
    best_fit = max(
        eligible,
        key=lambda row: (
            row.memory_required_gb if row.memory_required_gb is not None else -1.0,
            row.predicted_tokens_per_second,
        ),
        default=None,
    )
    fastest = max(eligible, key=lambda row: row.predicted_tokens_per_second, default=None)
    measured = [row for row in eligible if row.measured_quality is not None]
    best_quality = max(measured, key=lambda row: row.measured_quality.score, default=None)
    quality_complete = bool(eligible) and purpose is not None and len(measured) == len(eligible)
    best_match = (
        max(
            measured,
            key=lambda row: (
                row.measured_quality.score,
                row.predicted_tokens_per_second,
            ),
            default=None,
        )
        if quality_complete
        else best_fit
    )
    return ComparisonResult(
        rows=tuple(rows),
        purpose=purpose,
        best_fit_ref=best_fit.ref if best_fit else None,
        fastest_ref=fastest.ref if fastest else None,
        best_measured_quality_ref=best_quality.ref if best_quality else None,
        best_match_ref=best_match.ref if best_match else None,
        quality_complete=quality_complete,
    )
