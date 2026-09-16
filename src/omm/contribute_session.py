"""Ownership and limits for one contribution session."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Callable

from omm import install_state, registry
from omm.downloader import DownloadBudget


@dataclass(frozen=True)
class ContributionLimits:
    seconds: float | None = None
    download_bytes: int | None = None
    models: int | None = None

    def __post_init__(self):
        for value in (self.seconds, self.download_bytes, self.models):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value <= 0):
                raise ValueError("Contribution limits must be positive, finite numbers.")
        if self.seconds is not None and self.seconds > threading.TIMEOUT_MAX:
            raise ValueError("The time limit is too large.")
        if any(value is not None and type(value) is not int for value in (self.download_bytes, self.models)):
            raise ValueError("Download bytes and model counts must be whole numbers.")


class ContributionSession:
    """Keep a model lock until this session finishes its owned cleanup.

    Existing files, download fragments and registrations are never adopted by an
    unattended contribution. Even a stale registration belongs to its user.
    """

    def __init__(self, cleanup: Callable[[str], bool | None], *,
                 limits: ContributionLimits | None = None, stop_event=None):
        self.cleanup = cleanup
        self._lock = ExitStack()
        self.filename: str | None = None
        self.preserved: set[str] = set()
        self.removed: set[str] = set()
        self.cleanup_failed: set[str] = set()
        self.limits = limits or ContributionLimits()
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.stop_reason: str | None = None
        self.attempted: set[str] = set()
        self.started = time.monotonic()
        self.download_budget = DownloadBudget(self.limits.download_bytes,
                                             lambda: self.stop("download_limit"))
        self._timer = None
        if self.limits.seconds is not None:
            self._timer = threading.Timer(self.limits.seconds, lambda: self.stop("time_limit"))
            self._timer.daemon = True
            self._timer.start()

    def stop(self, reason: str) -> None:
        if not self.stop_event.is_set():
            self.stop_reason = reason
        self.stop_event.set()

    def check_limits(self) -> bool:
        if self.limits.seconds is not None and time.monotonic() - self.started >= self.limits.seconds:
            self.stop("time_limit")
        if self.download_budget.remaining == 0:
            self.stop("download_limit")
        return self.stop_event.is_set()

    def begin_model(self, reference: str) -> bool:
        if self.check_limits():
            return False
        if reference not in self.attempted and self.limits.models is not None and len(self.attempted) >= self.limits.models:
            self.stop("model_limit")
            return False
        self.attempted.add(reference)
        return True

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self.release()

    def claim(self, filename: str, path: Path) -> bool:
        self.release()
        self._lock.enter_context(install_state.cleanup_guard(filename))
        registered = any(name.casefold() == filename.casefold() for name in registry.load_registry())
        partials = list(path.parent.glob(path.name + ".part*"))
        if registered or path.exists() or path.is_symlink() or partials:
            self.preserved.add(filename)
            self._lock.close()
            return False
        self.filename = filename
        return True

    def clean(self) -> None:
        if self.filename is None:
            return
        filename, self.filename = self.filename, None
        try:
            result = self.cleanup(filename)
            if result is True:
                self.removed.add(filename)
            elif result is False:
                self.cleanup_failed.add(filename)
        except Exception:
            self.cleanup_failed.add(filename)
            raise

    def release(self) -> None:
        try:
            self.clean()
        finally:
            self._lock.close()
