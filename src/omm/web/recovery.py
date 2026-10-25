"""Typed recovery hints; never turn an error message into an executable action."""
from __future__ import annotations

import errno


class ActionError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def failure(error) -> dict:
    from omm.install_safety import SafetyError
    reason = getattr(error, "reason", "unknown")
    if isinstance(error, SafetyError):
        reason = "safety_failed"
    elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
        reason = "disk_full"
    if not isinstance(reason, str) or reason not in {"server_unavailable", "out_of_memory", "disk_full", "safety_failed",
                      "model_not_visible", "model_busy", "unsupported_runtime"}:
        reason = "unknown"
    return {"reason": reason}
