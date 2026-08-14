"""Consent-aware memory planning for OMM-owned local runtime operations."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Protocol, Sequence

from omm.hardware import HardwareInfo, calculate_memory_budget


class GuardDecision(Enum):
    SAFE = "safe"
    WARN = "warn"
    BLOCK = "block"


class GuardPolicy(Enum):
    ASK = "ask"
    BLOCK = "block"
    OBSERVE = "observe"


@dataclass(frozen=True)
class ResidentModel:
    engine: str
    model_id: str
    size_gb: float
    owned_by_omm: bool
    ram_gb: float | None = None
    vram_gb: float | None = None
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("size_gb", self.size_gb),
            ("ram_gb", self.ram_gb),
            ("vram_gb", self.vram_gb),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True)
class MemoryGuardPlan:
    decision: GuardDecision
    required_gb: float
    available_gb: float
    reserve_gb: float
    managed_residents: tuple[ResidentModel, ...]
    reclaimable_gb: float
    reasons: tuple[str, ...]
    required_ram_gb: float | None = None
    available_ram_gb: float | None = None
    required_vram_gb: float | None = None
    available_vram_gb: float | None = None


@dataclass(frozen=True)
class GuardExecution:
    allowed: bool
    decision: GuardDecision
    unloaded: tuple[ResidentModel, ...]
    reasons: tuple[str, ...]
    recalculated_plan: MemoryGuardPlan | None = None


class MemoryGuardRuntime(Protocol):
    def unload(self, resident: ResidentModel) -> bool: ...

    def is_resident(self, resident: ResidentModel) -> bool | None: ...


def normalize_policy(value: object) -> GuardPolicy:
    try:
        return GuardPolicy(str(value))
    except ValueError:
        return GuardPolicy.ASK


def _finite_non_negative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def plan_memory_guard(
    required_gb: float,
    hardware: HardwareInfo,
    residents: Sequence[ResidentModel] = (),
    *,
    required_ram_gb: float | None = None,
    required_vram_gb: float | None = None,
    exclude_model_id: str | None = None,
) -> MemoryGuardPlan:
    required = _finite_non_negative(required_gb, "required_gb")
    if required_ram_gb is not None:
        required_ram_gb = _finite_non_negative(required_ram_gb, "required_ram_gb")
    if required_vram_gb is not None:
        required_vram_gb = _finite_non_negative(required_vram_gb, "required_vram_gb")
    budget = calculate_memory_budget(hardware)
    managed = tuple(
        resident
        for resident in residents
        if resident.owned_by_omm and resident.model_id != exclude_model_id
    )

    if hardware.unified_memory:
        # Ollama may report unified-memory bytes in ``size_vram`` and leave
        # the derived RAM component at zero. The total resident size is the
        # single shared-pool footprint and must be counted exactly once.
        reclaimable = sum(resident.size_gb for resident in managed)
        fits_now = required <= budget.ram_budget_gb
        fits_after = required <= budget.ram_budget_gb + reclaimable
        available = budget.ram_budget_gb
        reserve = budget.ram_safety_reserve_gb
        available_ram = budget.ram_budget_gb
        available_vram = None
    elif required_ram_gb is not None or required_vram_gb is not None:
        ram_need = required_ram_gb or 0.0
        vram_need = required_vram_gb or 0.0
        available_ram = budget.ram_budget_gb
        available_vram = budget.vram_budget_gb or 0.0
        reclaimable_ram = sum(
            resident.ram_gb if resident.ram_gb is not None else 0.0 for resident in managed
        )
        reclaimable_vram = sum(
            resident.vram_gb if resident.vram_gb is not None else 0.0 for resident in managed
        )
        fits_now = ram_need <= available_ram and vram_need <= available_vram
        fits_after = (
            ram_need <= available_ram + reclaimable_ram
            and vram_need <= available_vram + reclaimable_vram
        )
        reclaimable = max(reclaimable_ram, reclaimable_vram)
        available = max(available_ram, available_vram)
        reserve = max(
            budget.ram_safety_reserve_gb,
            budget.vram_safety_reserve_gb or 0.0,
        )
    else:
        reclaimable = sum(resident.size_gb for resident in managed)
        fits_now = required <= budget.model_budget_gb
        fits_after = required <= budget.model_budget_gb + reclaimable
        available = budget.model_budget_gb
        reserve = max(
            budget.ram_safety_reserve_gb,
            budget.vram_safety_reserve_gb or 0.0,
        )
        available_ram = budget.ram_budget_gb
        available_vram = budget.vram_budget_gb

    if fits_now:
        decision = GuardDecision.SAFE
        reasons: tuple[str, ...] = ()
    elif fits_after and managed:
        decision = GuardDecision.WARN
        reasons = ("managed_release_can_recover",)
    else:
        decision = GuardDecision.BLOCK
        reasons = (
            "insufficient_live_memory",
            "no_owned_release_available" if not managed else "owned_release_insufficient",
        )
    return MemoryGuardPlan(
        decision=decision,
        required_gb=required,
        available_gb=available,
        reserve_gb=reserve,
        managed_residents=managed,
        reclaimable_gb=reclaimable,
        reasons=reasons,
        required_ram_gb=required_ram_gb,
        available_ram_gb=available_ram,
        required_vram_gb=required_vram_gb,
        available_vram_gb=available_vram,
    )


def execute_guard(
    plan: MemoryGuardPlan,
    policy: GuardPolicy | str,
    runtime: MemoryGuardRuntime,
    *,
    consent: Callable[[MemoryGuardPlan], bool] | None = None,
    recalculate: Callable[[], MemoryGuardPlan] | None = None,
) -> GuardExecution:
    selected_policy = policy if isinstance(policy, GuardPolicy) else normalize_policy(policy)
    if plan.decision is GuardDecision.SAFE:
        return GuardExecution(True, plan.decision, (), plan.reasons, plan)
    if selected_policy is GuardPolicy.OBSERVE:
        return GuardExecution(True, GuardDecision.WARN, (), (*plan.reasons, "observe_only"), plan)
    if selected_policy is GuardPolicy.BLOCK or plan.decision is GuardDecision.BLOCK:
        return GuardExecution(False, GuardDecision.BLOCK, (), plan.reasons, plan)
    if consent is None or not consent(plan):
        return GuardExecution(False, GuardDecision.BLOCK, (), (*plan.reasons, "consent_denied"), plan)

    unloaded = []
    refreshed = None
    for resident in sorted(plan.managed_residents, key=lambda item: item.size_gb, reverse=True):
        if not resident.owned_by_omm:
            continue
        if not runtime.unload(resident):
            return GuardExecution(
                False,
                GuardDecision.BLOCK,
                tuple(unloaded),
                (*plan.reasons, "unload_failed"),
            )
        if runtime.is_resident(resident) is not False:
            return GuardExecution(
                False,
                GuardDecision.BLOCK,
                tuple(unloaded),
                (*plan.reasons, "unload_not_confirmed"),
            )
        unloaded.append(resident)
        if recalculate is not None:
            refreshed = recalculate()
            if refreshed.decision is GuardDecision.SAFE:
                return GuardExecution(
                    True,
                    GuardDecision.SAFE,
                    tuple(unloaded),
                    (),
                    refreshed,
                )
        elif sum(item.size_gb for item in unloaded) + plan.available_gb >= plan.required_gb:
            break

    if recalculate is not None and refreshed is None:
        refreshed = recalculate()
    if refreshed is None or refreshed.decision is not GuardDecision.SAFE:
        return GuardExecution(
            False,
            GuardDecision.BLOCK,
            tuple(unloaded),
            (*plan.reasons, "recalculation_not_safe"),
            refreshed,
        )
    return GuardExecution(True, GuardDecision.SAFE, tuple(unloaded), (), refreshed)


def omm_managed_model_ids(registry_data: Mapping[str, object], engine: str) -> set[str]:
    managed = set()
    for filename, raw_entry in registry_data.items():
        if not isinstance(raw_entry, dict):
            continue
        linked = raw_entry.get("linked")
        if not isinstance(linked, dict) or linked.get(engine) is not True:
            continue
        if engine == "ollama":
            runtime_id = raw_entry.get("ollama_name")
            if isinstance(runtime_id, str) and runtime_id:
                managed.add(runtime_id)
        elif isinstance(filename, str) and filename:
            managed.add(filename)
    return managed


def _same_ollama_id(left: str, right: str) -> bool:
    left_key = left.casefold()
    right_key = right.casefold()
    return (
        left_key == right_key
        or left_key.removesuffix(":latest") == right_key.removesuffix(":latest")
    )


class OllamaManagedRuntime:
    """Ollama bridge that uses only its API and OMM's ownership registry."""

    def __init__(self, registry_data: Mapping[str, object]) -> None:
        self._managed = omm_managed_model_ids(registry_data, "ollama")

    def list_residents(self) -> tuple[ResidentModel, ...]:
        from omm import quality

        try:
            rows = quality._request_json("GET", "/api/ps", timeout=10).get("models")
        except quality.QualityEvaluationError:
            return ()
        if not isinstance(rows, list):
            return ()
        residents = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("name") or row.get("model")
            if not isinstance(model_id, str) or not model_id:
                continue
            size_bytes = row.get("size")
            vram_bytes = row.get("size_vram")
            size_gb = (
                float(size_bytes) / (1024**3)
                if isinstance(size_bytes, (int, float)) and not isinstance(size_bytes, bool)
                else 0.0
            )
            vram_gb = (
                float(vram_bytes) / (1024**3)
                if isinstance(vram_bytes, (int, float)) and not isinstance(vram_bytes, bool)
                else None
            )
            owned = any(_same_ollama_id(model_id, managed) for managed in self._managed)
            residents.append(
                ResidentModel(
                    engine="ollama",
                    model_id=model_id,
                    size_gb=max(0.0, size_gb),
                    owned_by_omm=owned,
                    ram_gb=max(0.0, size_gb - (vram_gb or 0.0)),
                    vram_gb=vram_gb,
                    receipt_id=model_id,
                )
            )
        return tuple(residents)

    def unload(self, resident: ResidentModel) -> bool:
        if not resident.owned_by_omm or resident.engine != "ollama":
            return False
        from omm import quality

        return quality.ensure_model_unloaded(resident.model_id)

    def is_resident(self, resident: ResidentModel) -> bool | None:
        from omm import quality

        return quality._model_is_loaded(resident.model_id)


class PressureAction(Enum):
    CONTINUE = "continue"
    CANCEL_OWNED_OPERATION = "cancel_owned_operation"


@dataclass
class SustainedPressureMonitor:
    threshold_gb: float
    required_consecutive_samples: int = 3
    low_memory_seconds: float = 3.0
    _consecutive: int = 0
    _first_low_at: float | None = None

    def __post_init__(self) -> None:
        self.threshold_gb = _finite_non_negative(self.threshold_gb, "threshold_gb")
        if (
            isinstance(self.required_consecutive_samples, bool)
            or not isinstance(self.required_consecutive_samples, int)
            or self.required_consecutive_samples < 2
        ):
            raise ValueError("required_consecutive_samples must be an integer >= 2")
        self.low_memory_seconds = _finite_non_negative(
            self.low_memory_seconds, "low_memory_seconds"
        )

    def observe(
        self,
        available_gb: float,
        *,
        operation_owned_by_omm: bool,
        captured_at: float | None = None,
    ) -> PressureAction:
        available = _finite_non_negative(available_gb, "available_gb")
        now = time.monotonic() if captured_at is None else float(captured_at)
        if available >= self.threshold_gb:
            self._consecutive = 0
            self._first_low_at = None
            return PressureAction.CONTINUE
        self._consecutive += 1
        if self._first_low_at is None:
            self._first_low_at = now
        sustained = (
            self._consecutive >= self.required_consecutive_samples
            and now - self._first_low_at >= self.low_memory_seconds
        )
        if sustained and operation_owned_by_omm:
            return PressureAction.CANCEL_OWNED_OPERATION
        return PressureAction.CONTINUE


class RuntimePressureWatcher:
    """Bounded polling that may cancel only the operation explicitly marked OMM-owned."""

    def __init__(
        self,
        monitor: SustainedPressureMonitor,
        *,
        sample_available_gb: Callable[[], float],
        operation_owned_by_omm: bool,
        cancel_owned_operation: Callable[[], bool],
        poll_seconds: float = 1.0,
        max_samples: int = 3600,
        cancel_wait_seconds: float = 35.0,
    ) -> None:
        self.monitor = monitor
        self.sample_available_gb = sample_available_gb
        self.operation_owned_by_omm = operation_owned_by_omm
        self.cancel_owned_operation = cancel_owned_operation
        self.poll_seconds = _finite_non_negative(poll_seconds, "poll_seconds")
        if self.poll_seconds < 0.1 or self.poll_seconds > 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or max_samples < 1:
            raise ValueError("max_samples must be a positive integer")
        self.max_samples = max_samples
        self.cancel_wait_seconds = _finite_non_negative(
            cancel_wait_seconds, "cancel_wait_seconds"
        )
        if not 0.1 <= self.cancel_wait_seconds <= 120:
            raise ValueError("cancel_wait_seconds must be between 0.1 and 120")
        self.pressure_triggered = False
        self.cancelled = False
        self.cancellation_timed_out = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RuntimePressureWatcher":
        self._thread = threading.Thread(target=self._run, name="omm-memory-guard", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        for _ in range(self.max_samples):
            if self._stop.is_set():
                return
            try:
                available = self.sample_available_gb()
            except (OSError, RuntimeError, ValueError):
                return
            if self._stop.is_set():
                return
            action = self.monitor.observe(
                available,
                operation_owned_by_omm=self.operation_owned_by_omm,
            )
            if action is PressureAction.CANCEL_OWNED_OPERATION:
                self.pressure_triggered = True
                try:
                    self.cancelled = bool(self.cancel_owned_operation())
                except Exception:
                    self.cancelled = False
                return
            if self._stop.wait(self.poll_seconds):
                return

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.cancel_wait_seconds)
            if self._thread.is_alive():
                self.cancellation_timed_out = True
                self.cancelled = False
