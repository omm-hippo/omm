"""Local runtime adapters used by compatibility verification."""

from omm.engines.base import (
    FAILURE_REASONS,
    LoadOptions,
    LoadReceipt,
    ProbeRequest,
    ProbeResult,
    RuntimeAdapter,
    RuntimeAdapterError,
    RuntimeHealth,
    RuntimeModel,
    RuntimeModelRef,
    UnloadResult,
    find_runtime_model,
)

__all__ = [
    "FAILURE_REASONS",
    "LoadOptions",
    "LoadReceipt",
    "ProbeRequest",
    "ProbeResult",
    "RuntimeAdapter",
    "RuntimeAdapterError",
    "RuntimeHealth",
    "RuntimeModel",
    "RuntimeModelRef",
    "UnloadResult",
    "find_runtime_model",
]
