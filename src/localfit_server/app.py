"""FastAPI application for self-hosted, privacy-minimized benchmark data."""

from __future__ import annotations

import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from localfit_server.db import BenchmarkStore


class BenchmarkEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=300)

    ram_gb: float = Field(gt=0, le=4096)
    vram_gb: float | None = Field(default=None, ge=0, le=4096)
    unified_memory: bool
    gpu_tflops: float | None = Field(default=None, ge=0, le=100_000)
    model_installed: str = Field(min_length=1, max_length=300)
    model_repo_id: str | None = Field(default=None, max_length=300)
    model_provider: Literal["huggingface", "modelscope"] | None = None
    model_size_bytes: int | None = Field(default=None, gt=0, le=10**15)
    model_filename: str | None = Field(default=None, max_length=300)
    model_digest: str | None = Field(default=None, max_length=64)
    parameter_count_b: float | None = Field(default=None, gt=0, le=10_000)
    active_parameter_count_b: float | None = Field(default=None, gt=0, le=10_000)
    quant_bits: float | None = Field(default=None, ge=0.5, le=32)
    engine_version: str | None = Field(default=None, min_length=1, max_length=100)
    client_version: str | None = Field(default=None, min_length=1, max_length=100)
    engine: Literal["llama.cpp", "lmstudio", "ollama", "jan", "gpt4all"]
    benchmark_version: int = Field(ge=1, le=8)
    recorded_at: datetime
    tokens_per_sec: float | None = Field(default=None, gt=0, le=10_000)
    sample_count: int | None = Field(default=None, ge=1, le=10)
    tokens_per_sec_min: float | None = Field(default=None, gt=0, le=10_000)
    tokens_per_sec_max: float | None = Field(default=None, gt=0, le=10_000)
    runtime_profile: str | None = Field(default=None, max_length=50)
    context_length: int | None = Field(default=None, ge=128, le=10_000_000)
    gpu_offload_percent: int | None = Field(default=None, ge=0, le=100)
    cpu_threads: int | None = Field(default=None, ge=1, le=4096)
    num_batch: int | None = Field(default=None, ge=1, le=1_000_000)
    cpu_model: str | None = Field(default=None, min_length=1, max_length=256)
    cpu_arch: str | None = Field(default=None, min_length=1, max_length=64)
    cpu_physical_cores: int | None = Field(default=None, ge=1, le=1024)
    cpu_logical_cores: int | None = Field(default=None, ge=1, le=1024)
    cpu_score: float | None = Field(default=None, ge=0, le=99_999)
    cpu_tier: float | None = Field(default=None, ge=0, le=10)
    gpu_score: float | None = Field(default=None, ge=0, le=99_999)
    gpu_tier: float | None = Field(default=None, ge=0, le=10)
    quality_pack_id: str | None = Field(default=None, max_length=100)
    quality_pack_version: str | None = Field(default=None, max_length=20)
    quality_correct: int | None = Field(default=None, ge=0, le=100)
    quality_total: int | None = Field(default=None, ge=1, le=100)
    quality_accuracy: float | None = Field(default=None, ge=0, le=1)
    outcome: Literal[
        "success", "model_unfit", "transient_error", "performance_unfit"
    ] | None = None
    failure_reason: Literal[
        "out_of_memory",
        "model_load_failed",
        "unsupported_runtime",
        "generation_timeout",
        "ollama_unavailable",
        "connection_error",
        "no_timing_metrics",
        "unknown",
        "confirmed_generation_timeout",
    ] | None = None
    confirmation_attempts: int | None = Field(default=None, ge=1, le=2)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)

    @field_validator("model_installed", "model_repo_id", "model_filename")
    @classmethod
    def reject_paths_and_controls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 32 for character in value) or "\\" in value:
            raise ValueError("control characters and local paths are not allowed")
        if value.startswith("/") or ":/" in value:
            raise ValueError("local paths are not allowed")
        return value

    @field_validator("model_digest")
    @classmethod
    def normalize_model_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("model_digest must be a SHA-256 hex digest")
        return normalized

    @model_validator(mode="after")
    def validate_sample_summary(self) -> "BenchmarkEvent":
        bounds = (self.tokens_per_sec_min, self.tokens_per_sec_max)
        if (bounds[0] is None) != (bounds[1] is None):
            raise ValueError("sample minimum and maximum must be supplied together")
        if self.tokens_per_sec is None and (
            self.sample_count is not None or bounds[0] is not None
        ):
            raise ValueError("sample summary requires tokens_per_sec")
        if bounds[0] is not None and not (
            bounds[0] <= self.tokens_per_sec <= bounds[1]
        ):
            raise ValueError("median speed must be inside the sample range")
        if self.sample_count is not None and bounds[0] is None:
            raise ValueError("sample_count requires minimum and maximum")
        return self

    @model_validator(mode="after")
    def validate_quality_summary(self) -> "BenchmarkEvent":
        quality_fields = (
            self.quality_pack_id,
            self.quality_pack_version,
            self.quality_correct,
            self.quality_total,
            self.quality_accuracy,
        )
        if any(f is not None for f in quality_fields) and any(f is None for f in quality_fields):
            raise ValueError("quality fields must all be supplied together")
        if self.quality_correct is not None and self.quality_total is not None:
            if self.quality_correct > self.quality_total:
                raise ValueError("quality_correct cannot exceed quality_total")
            if self.quality_accuracy is not None and abs(
                self.quality_accuracy - self.quality_correct / self.quality_total
            ) > 1e-4:
                raise ValueError("quality_accuracy must equal quality_correct / quality_total")
        return self

    @model_validator(mode="after")
    def validate_versioned_contract(self) -> "BenchmarkEvent":
        if self.benchmark_version < 7:
            if self.tokens_per_sec is None:
                raise ValueError("legacy success events require tokens_per_sec")
            if any(
                value is not None
                for value in (
                    self.outcome,
                    self.failure_reason,
                    self.confirmation_attempts,
                    self.timeout_seconds,
                )
            ):
                raise ValueError("outcome fields require benchmark_version 7 or 8")
        if self.benchmark_version not in (5, 6, 7, 8):
            return self
        if self.benchmark_version in (7, 8):
            return self._validate_outcome_contract()
        required_model_metadata = (
            self.parameter_count_b,
            self.active_parameter_count_b,
            self.quant_bits,
            self.engine_version,
            self.client_version,
        )
        if any(value is None for value in required_model_metadata):
            raise ValueError("v5+ requires model metadata and component versions")
        if self.active_parameter_count_b > self.parameter_count_b:
            raise ValueError("active_parameter_count_b cannot exceed parameter_count_b")
        required_runtime = (
            self.runtime_profile,
            self.context_length,
            self.gpu_offload_percent,
            self.cpu_threads,
            self.num_batch,
        )
        if any(value is None for value in required_runtime):
            raise ValueError("v5+ requires runtime metadata")
        if not self.runtime_profile.strip():
            raise ValueError("v5+ runtime_profile must be non-empty")
        if not 256 <= self.context_length <= 131_072:
            raise ValueError("v5+ context_length must be between 256 and 131072")
        if not 1 <= self.cpu_threads <= 1024:
            raise ValueError("v5+ cpu_threads must be between 1 and 1024")
        if not 1 <= self.num_batch <= 65_536:
            raise ValueError("v5+ num_batch must be between 1 and 65536")
        required_samples = (
            self.sample_count,
            self.tokens_per_sec_min,
            self.tokens_per_sec_max,
        )
        if any(value is None for value in required_samples):
            raise ValueError("v5+ requires sample summary")
        if self.sample_count < 3:
            raise ValueError("v5+ sample_count must be at least 3")
        if self.benchmark_version == 6:
            cpu_fields = (
                self.cpu_model,
                self.cpu_arch,
                self.cpu_physical_cores,
                self.cpu_logical_cores,
            )
            if any(value is None for value in cpu_fields):
                raise ValueError("v6 requires CPU model, architecture, and core counts")
            if self.cpu_physical_cores > self.cpu_logical_cores:
                raise ValueError("physical CPU cores cannot exceed logical CPU cores")
        return self

    def _validate_outcome_contract(self) -> "BenchmarkEvent":
        version = f"v{self.benchmark_version}"
        if self.engine != "ollama":
            raise ValueError(f"{version} events require the ollama engine")
        if self.outcome is None:
            raise ValueError(f"{version} events require an explicit outcome")
        if self.benchmark_version == 8 and self.cpu_model is not None:
            raise ValueError("v8 events must not include a raw CPU model name")
        speed_fields = (
            self.tokens_per_sec,
            self.sample_count,
            self.tokens_per_sec_min,
            self.tokens_per_sec_max,
        )
        if self.outcome == "success":
            if self.failure_reason is not None:
                raise ValueError(f"{version} success must not include failure_reason")
            if self.confirmation_attempts is not None or self.timeout_seconds is not None:
                raise ValueError(f"{version} success must not include confirmation fields")
            required_model_metadata = (
                self.parameter_count_b,
                self.active_parameter_count_b,
                self.quant_bits,
                self.engine_version,
                self.client_version,
            )
            if any(value is None for value in required_model_metadata):
                raise ValueError(
                    f"{version} success requires model metadata and component versions"
                )
            if self.active_parameter_count_b > self.parameter_count_b:
                raise ValueError("active_parameter_count_b cannot exceed parameter_count_b")
            required_runtime = (
                self.runtime_profile,
                self.context_length,
                self.gpu_offload_percent,
                self.cpu_threads,
                self.num_batch,
            )
            if any(value is None for value in required_runtime):
                raise ValueError(f"{version} success requires runtime metadata")
            if not self.runtime_profile.strip():
                raise ValueError(f"{version} success runtime_profile must be non-empty")
            if not 256 <= self.context_length <= 131_072:
                raise ValueError(
                    f"{version} success context_length must be between 256 and 131072"
                )
            if not 1 <= self.cpu_threads <= 1024:
                raise ValueError(
                    f"{version} success cpu_threads must be between 1 and 1024"
                )
            if not 1 <= self.num_batch <= 65_536:
                raise ValueError(
                    f"{version} success num_batch must be between 1 and 65536"
                )
            cpu_fields = (
                (
                    self.cpu_model,
                    self.cpu_arch,
                    self.cpu_physical_cores,
                    self.cpu_logical_cores,
                )
                if self.benchmark_version == 7
                else (
                    self.cpu_score,
                    self.cpu_tier,
                    self.cpu_arch,
                    self.cpu_physical_cores,
                    self.cpu_logical_cores,
                )
            )
            if any(value is None for value in cpu_fields):
                raise ValueError(
                    f"{version} success requires privacy-safe CPU metadata"
                )
            if self.cpu_physical_cores > self.cpu_logical_cores:
                raise ValueError("physical CPU cores cannot exceed logical CPU cores")
            if self.tokens_per_sec is None or self.sample_count is None:
                raise ValueError(f"{version} success requires speed and sample summary")
            if self.sample_count < 3:
                raise ValueError(f"{version} success sample_count must be at least 3")
            if self.tokens_per_sec_min is None or self.tokens_per_sec_max is None:
                raise ValueError(
                    f"{version} success requires sample minimum and maximum"
                )
            return self

        if any(value is not None for value in speed_fields):
            raise ValueError(f"{version} failure events must not include speed fields")
        if any(
            value is not None
            for value in (
                self.quality_pack_id,
                self.quality_pack_version,
                self.quality_correct,
                self.quality_total,
                self.quality_accuracy,
            )
        ):
            raise ValueError(f"{version} failure events must not include quality fields")
        reasons = {
            "model_unfit": {"out_of_memory", "unsupported_runtime"},
            "transient_error": {
                "model_load_failed",
                "generation_timeout",
                "ollama_unavailable",
                "connection_error",
                "no_timing_metrics",
                "unknown",
            },
            "performance_unfit": {"confirmed_generation_timeout"},
        }
        if self.failure_reason not in reasons[self.outcome]:
            raise ValueError(
                f"invalid failure_reason for {version} {self.outcome}"
            )
        if self.outcome == "performance_unfit":
            if self.confirmation_attempts != 2 or self.timeout_seconds is None:
                raise ValueError(
                    f"{version} performance_unfit requires two attempts and timeout_seconds"
                )
        elif self.confirmation_attempts is not None or self.timeout_seconds is not None:
            raise ValueError("confirmation fields are only valid for performance_unfit")
        return self


@lru_cache(maxsize=1)
def get_store() -> BenchmarkStore:
    configured = os.getenv("LOCALFIT_DB_PATH", "./localfit.db")
    return BenchmarkStore(Path(configured).expanduser())


def require_admin(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("LOCALFIT_ADMIN_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LOCALFIT_ADMIN_TOKEN is not configured",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


app = FastAPI(
    title="Localfit self-hosted benchmark collector",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)


@app.get("/healthz")
def health() -> dict[str, str]:
    get_store().count()
    return {"status": "ok", "storage": "sqlite"}


@app.post("/v1/benchmarks", status_code=status.HTTP_201_CREATED)
def create_benchmark(event: BenchmarkEvent) -> dict[str, int | str]:
    # Preserve the wire contract: absent optional v7/v8 failure fields stay
    # absent in event_json rather than becoming explicit nulls.
    result = get_store().insert(event.model_dump(mode="json", exclude_none=True))
    return {"id": result.id, "status": "stored" if result.created else "duplicate"}


@app.get("/v1/stats")
def stats() -> dict[str, object]:
    store = get_store()
    return {"count": store.count(), "engines": store.engine_counts()}


@app.get("/v1/benchmarks/export", dependencies=[Depends(require_admin)])
def export_benchmarks(limit: int = Query(default=100_000, ge=1, le=100_000)) -> dict[str, object]:
    rows = get_store().export(limit=limit)
    return {"count": len(rows), "benchmarks": rows}
