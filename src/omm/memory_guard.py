"""Consent-aware memory planning for OMM-owned local runtime operations."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, Sequence

from omm.hardware import HardwareInfo, calculate_memory_budget

if TYPE_CHECKING:
    from omm.engines.base import RuntimeModelRef


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
            if value is not None:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value < 0
                ):
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
        if required_ram_gb is not None and required_vram_gb is not None:
            # Both pools are real constraints here, unlike the RAM-only/
            # VRAM-only sub-cases below (left on the old `max()` fallback).
            # Report whichever pool is actually binding - the one with the
            # larger shortfall against its own requirement - instead of
            # unconditionally taking the larger, possibly irrelevant pool.
            if (ram_need - available_ram) >= (vram_need - available_vram):
                available = available_ram
                reclaimable = reclaimable_ram
            else:
                available = available_vram
                reclaimable = reclaimable_vram
        else:
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
    heuristic_confirmed_safe = False

    def reclaim_priority(resident: ResidentModel) -> float:
        # When RAM and VRAM are separate constraints, prioritize residents
        # that actually reclaim the currently short pool. Sorting solely by
        # total size could unload a large RAM-only model before the small
        # GPU resident that is the reason this plan needs reclamation.
        if plan.available_vram_gb is None or (
            plan.required_ram_gb is None and plan.required_vram_gb is None
        ):
            # No per-pool requirement to score against (the CLI passes one
            # blended requirement), so reclaim the biggest resident first.
            return resident.size_gb
        score = 0.0
        if plan.required_ram_gb is not None:
            shortfall = max(
                0.0, plan.required_ram_gb - (plan.available_ram_gb or 0.0)
            )
            score += min(shortfall, resident.ram_gb or 0.0)
        if plan.required_vram_gb is not None:
            shortfall = max(
                0.0, plan.required_vram_gb - (plan.available_vram_gb or 0.0)
            )
            score += min(shortfall, resident.vram_gb or 0.0)
        return score

    def heuristic_is_safe() -> bool:
        if plan.available_vram_gb is not None and (
            plan.required_ram_gb is not None or plan.required_vram_gb is not None
        ):
            ram_reclaimed = sum(item.ram_gb or 0.0 for item in unloaded)
            vram_reclaimed = sum(item.vram_gb or 0.0 for item in unloaded)
            ram_safe = (
                plan.required_ram_gb is None
                or (plan.available_ram_gb or 0.0) + ram_reclaimed
                >= plan.required_ram_gb
            )
            vram_safe = (
                plan.required_vram_gb is None
                or (plan.available_vram_gb or 0.0) + vram_reclaimed
                >= plan.required_vram_gb
            )
            return ram_safe and vram_safe
        return (
            sum(item.size_gb for item in unloaded) + plan.available_gb
            >= plan.required_gb
        )

    for resident in sorted(
        plan.managed_residents, key=reclaim_priority, reverse=True
    ):
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
        elif heuristic_is_safe():
            heuristic_confirmed_safe = True
            break

    if recalculate is not None:
        if refreshed is None:
            refreshed = recalculate()
        if refreshed.decision is not GuardDecision.SAFE:
            return GuardExecution(
                False,
                GuardDecision.BLOCK,
                tuple(unloaded),
                (*plan.reasons, "recalculation_not_safe"),
                refreshed,
            )
        return GuardExecution(True, GuardDecision.SAFE, tuple(unloaded), (), refreshed)

    # No `recalculate` callback was supplied: the only signal we have is the
    # heuristic size comparison against the (stale) `plan`. Only report SAFE
    # when that heuristic actually confirmed enough was freed - otherwise we
    # unloaded what we could but still don't know it's enough, so block.
    if heuristic_confirmed_safe:
        return GuardExecution(True, GuardDecision.SAFE, tuple(unloaded), (), None)
    return GuardExecution(
        False,
        GuardDecision.BLOCK,
        tuple(unloaded),
        (*plan.reasons, "unload_insufficient"),
        None,
    )


def omm_managed_model_ids(registry_data: Mapping[str, object], engine: str) -> set[str]:
    managed = set()
    for filename, raw_entry in registry_data.items():
        if not isinstance(raw_entry, dict):
            continue
        linked = raw_entry.get("linked")
        if not isinstance(linked, dict) or linked.get(engine) is not True:
            continue
        if engine == "ollama":
            from omm import linker

            runtime_id = (
                linker.resolve_ollama_runtime_name(filename, raw_entry)
                if isinstance(filename, str)
                else None
            )
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


def _runtime_bytes_to_gb(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    if not math.isfinite(converted) or converted < 0:
        return None
    return converted / (1024**3)


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
            size_gb = _runtime_bytes_to_gb(size_bytes) or 0.0
            vram_gb = _runtime_bytes_to_gb(vram_bytes)
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


def _lmstudio_registry_refs(
    registry_data: Mapping[str, object],
) -> dict[str, tuple["RuntimeModelRef", float]]:
    """Build the same key/alias identity `cli._compatibility_model_ref` uses
    for every OMM-installed, LM-Studio-linked filename, plus a size estimate
    (the installed GGUF's file size) to use as this resident's reclaimable
    footprint - LM Studio's API exposes no live per-model memory figure."""
    from omm.engines.base import RuntimeModelRef

    refs: dict[str, tuple[RuntimeModelRef, float]] = {}
    for filename, raw_entry in registry_data.items():
        if not isinstance(raw_entry, dict):
            continue
        linked = raw_entry.get("linked")
        if not isinstance(linked, dict) or linked.get("lmstudio") is not True:
            continue
        repo_id = raw_entry.get("repo_id")
        stem = Path(filename).stem
        key = repo_id if isinstance(repo_id, str) and "/" in repo_id else f"local/{stem}"
        aliases = tuple(
            value for value in (repo_id, filename, stem) if isinstance(value, str) and value
        )
        size_bytes = raw_entry.get("size_bytes")
        size_gb = _runtime_bytes_to_gb(size_bytes) or 0.0
        refs[filename] = (RuntimeModelRef(key, aliases), size_gb)
    return refs


class LMStudioManagedRuntime:
    """LM Studio bridge that uses only its API and OMM's ownership registry.

    Mirrors `OllamaManagedRuntime`'s contract, but LM Studio has no daemon
    endpoint reporting live per-model memory use the way Ollama's `/api/ps`
    does, so a resident's reclaimable size is estimated from the installed
    GGUF's file size on disk (looked up through the registry) rather than
    measured live.
    """

    def __init__(self, registry_data: Mapping[str, object]) -> None:
        self._refs = _lmstudio_registry_refs(registry_data)
        self._live_models: dict[str, object] = {}

    @staticmethod
    def _adapter():
        from omm.engines.lmstudio import LMStudioAdapter

        return LMStudioAdapter()

    def list_residents(self) -> tuple[ResidentModel, ...]:
        from omm.engines.base import RuntimeAdapterError, find_runtime_model

        try:
            models = self._adapter().list_models()
        except RuntimeAdapterError:
            return ()
        residents = []
        self._live_models = {}
        for model in models:
            if not model.loaded:
                continue
            instance_id = model.instance_id or model.key
            self._live_models[instance_id] = model
            owned = False
            size_gb = 0.0
            for ref, ref_size_gb in self._refs.values():
                if find_runtime_model([model], ref) is not None:
                    owned = True
                    size_gb = ref_size_gb
                    break
            residents.append(
                ResidentModel(
                    engine="lmstudio",
                    model_id=model.key,
                    size_gb=size_gb,
                    owned_by_omm=owned,
                    receipt_id=instance_id,
                )
            )
        return tuple(residents)

    def unload(self, resident: ResidentModel) -> bool:
        if not resident.owned_by_omm or resident.engine != "lmstudio":
            return False
        from omm.engines.base import LoadReceipt, RuntimeAdapterError, RuntimeModel

        instance_id = resident.receipt_id or resident.model_id
        live = self._live_models.get(instance_id)
        model = live if live is not None else RuntimeModel(
            resident.model_id, resident.model_id, True, instance_id
        )
        receipt = LoadReceipt(model, instance_id, False, True)
        try:
            return self._adapter().unload(receipt).unloaded
        except RuntimeAdapterError:
            return False

    def is_resident(self, resident: ResidentModel) -> bool | None:
        from omm.engines.base import RuntimeAdapterError, RuntimeModelRef, find_runtime_model

        try:
            models = self._adapter().list_models()
        except RuntimeAdapterError:
            return None
        aliases = (resident.receipt_id,) if resident.receipt_id else ()
        match = find_runtime_model(models, RuntimeModelRef(resident.model_id, aliases))
        if match is None:
            return None
        return match.loaded


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
        now = (
            time.monotonic()
            if captured_at is None
            else _finite_non_negative(captured_at, "captured_at")
        )
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
        self.pressure_observed = False
        self.cancelled = False
        self.cancellation_timed_out = False
        self.samples_gb: list[float] = []
        self._samples_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RuntimePressureWatcher":
        self.sample_now()
        self._thread = threading.Thread(target=self._run, name="omm-memory-guard", daemon=True)
        self._thread.start()
        return self

    def sample_now(self) -> float | None:
        try:
            available = float(self.sample_available_gb())
            if not math.isfinite(available) or available < 0:
                return None
        except Exception:
            return None
        with self._samples_lock:
            self.samples_gb.append(available)
        if available < self.monitor.threshold_gb:
            self.pressure_observed = True
        return available

    @property
    def first_available_gb(self) -> float | None:
        with self._samples_lock:
            return self.samples_gb[0] if self.samples_gb else None

    @property
    def minimum_available_gb(self) -> float | None:
        with self._samples_lock:
            return min(self.samples_gb) if self.samples_gb else None

    @property
    def last_available_gb(self) -> float | None:
        with self._samples_lock:
            return self.samples_gb[-1] if self.samples_gb else None

    def _run(self) -> None:
        for _ in range(self.max_samples):
            if self._stop.is_set():
                return
            available = self.sample_now()
            if available is None:
                return
            if self._stop.is_set():
                return
            action = self.monitor.observe(
                available,
                operation_owned_by_omm=self.operation_owned_by_omm,
            )
            if action is PressureAction.CANCEL_OWNED_OPERATION:
                with self._state_lock:
                    self.pressure_triggered = True
                try:
                    cancelled = bool(self.cancel_owned_operation())
                except Exception:
                    cancelled = False
                # __exit__ may have timed out while a slow cancellation
                # callback was still running. Do not let that abandoned
                # daemon thread later overwrite the timeout result.
                with self._state_lock:
                    if not self.cancellation_timed_out:
                        self.cancelled = cancelled
                return
            if self._stop.wait(self.poll_seconds):
                return

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.cancel_wait_seconds)
            if self._thread.is_alive():
                with self._state_lock:
                    self.cancellation_timed_out = True
                    self.cancelled = False
        self.sample_now()
