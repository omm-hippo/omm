"""Orchestrate a bounded local load/generate/unload compatibility probe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from omm import registry
from omm.engines.base import (
    FailureReason,
    LoadOptions,
    ProbeRequest,
    RuntimeAdapter,
    RuntimeAdapterError,
    RuntimeModelRef,
)

PROBE_VERSION = 1


@dataclass(frozen=True)
class CompatibilityResult:
    engine: str
    status: str
    checked_at: str
    probe_version: int
    runtime_version: str | None
    failure_reason: FailureReason | None
    model_was_preloaded: bool = False
    model_left_loaded: bool = False

    def registry_payload(self) -> dict:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "probe_version": self.probe_version,
            "runtime_version": self.runtime_version,
            "failure_reason": self.failure_reason,
        }


def verify_runtime(
    adapter: RuntimeAdapter,
    model: RuntimeModelRef,
    *,
    keep_loaded: bool = False,
    load_options: LoadOptions | None = None,
    probe_request: ProbeRequest | None = None,
    now: Callable[[], datetime] | None = None,
) -> CompatibilityResult:
    """Verify one model without persisting prompt or generated text."""
    clock = now or (lambda: datetime.now(timezone.utc))
    checked_at = clock().astimezone(timezone.utc).isoformat()
    health = adapter.health()
    if not health.reachable:
        return CompatibilityResult(
            adapter.key,
            "failed",
            checked_at,
            PROBE_VERSION,
            health.version,
            health.failure_reason or "server_unavailable",
        )

    receipt = None
    failure_reason: FailureReason | None = None
    try:
        receipt = adapter.load(model, load_options or LoadOptions())
        adapter.generate(receipt, probe_request or ProbeRequest())
    except RuntimeAdapterError as error:
        failure_reason = error.reason
    finally:
        if receipt is not None and receipt.loaded_by_omm and not keep_loaded:
            try:
                unload = adapter.unload(receipt)
            except RuntimeAdapterError:
                failure_reason = "unload_failed"
            else:
                if not unload.unloaded:
                    failure_reason = "unload_failed"

    return CompatibilityResult(
        adapter.key,
        "passed" if failure_reason is None else "failed",
        checked_at,
        PROBE_VERSION,
        health.version,
        failure_reason,
        model_was_preloaded=bool(receipt and receipt.was_already_loaded),
        model_left_loaded=bool(receipt and receipt.loaded_by_omm and keep_loaded),
    )


def verify_and_record(
    filename: str,
    adapter: RuntimeAdapter,
    model: RuntimeModelRef,
    **kwargs,
) -> CompatibilityResult:
    result = verify_runtime(adapter, model, **kwargs)
    registry.record_compatibility(filename, adapter.key, result.registry_payload())
    return result
