from __future__ import annotations

import inspect
import threading

import pytest

from omm import memory_guard as guard
from omm.hardware import HardwareInfo


def _hardware(**overrides):
    values = dict(
        os_name="macOS",
        os_version="",
        cpu="CPU",
        ram_total_gb=16.0,
        ram_available_gb=12.0,
        unified_memory=True,
        gpu_name="GPU",
        vram_total_gb=16.0,
        vram_free_gb=12.0,
    )
    values.update(overrides)
    return HardwareInfo(**values)


def _resident(model="old", size=4.0, owned=True, **overrides):
    return guard.ResidentModel("ollama", model, size, owned, **overrides)


def test_safe_warn_and_block_plans_use_live_budget_and_owned_residents_only():
    safe = guard.plan_memory_guard(6.0, _hardware(), [])
    warn = guard.plan_memory_guard(12.0, _hardware(), [_resident(size=4.0)])
    blocked = guard.plan_memory_guard(12.0, _hardware(), [_resident(size=8.0, owned=False)])

    assert safe.decision is guard.GuardDecision.SAFE
    assert warn.decision is guard.GuardDecision.WARN
    assert warn.reclaimable_gb == 4.0
    assert blocked.decision is guard.GuardDecision.BLOCK
    assert blocked.managed_residents == ()


def test_unified_memory_does_not_double_count_ram_and_vram():
    plan = guard.plan_memory_guard(
        12.0,
        _hardware(),
        [_resident(size=4.0, ram_gb=4.0, vram_gb=4.0)],
    )

    assert plan.reclaimable_gb == 4.0


def test_unified_memory_uses_total_resident_size_when_runtime_labels_it_vram():
    plan = guard.plan_memory_guard(
        12.0,
        _hardware(),
        [_resident(size=4.0, ram_gb=0.0, vram_gb=4.0)],
    )

    assert plan.reclaimable_gb == 4.0
    assert plan.decision is guard.GuardDecision.WARN


def test_dedicated_ram_and_vram_requirements_are_checked_separately():
    hardware = _hardware(
        unified_memory=False,
        ram_total_gb=32.0,
        ram_available_gb=20.0,
        vram_total_gb=8.0,
        vram_free_gb=2.0,
    )
    plan = guard.plan_memory_guard(
        8.0,
        hardware,
        [_resident(size=3.0, ram_gb=0.5, vram_gb=2.5)],
        required_ram_gb=4.0,
        required_vram_gb=3.0,
    )

    assert plan.decision is guard.GuardDecision.WARN
    assert plan.available_ram_gb > plan.required_ram_gb
    assert plan.available_vram_gb < plan.required_vram_gb


class _Runtime:
    def __init__(self, unload=True, still_resident=False):
        self.unload_result = unload
        self.still_resident = still_resident
        self.calls = []

    def unload(self, resident):
        self.calls.append(("unload", resident.model_id))
        return self.unload_result

    def is_resident(self, resident):
        self.calls.append(("check", resident.model_id))
        return self.still_resident


def test_denied_consent_changes_nothing():
    plan = guard.plan_memory_guard(12.0, _hardware(), [_resident()])
    runtime = _Runtime()

    result = guard.execute_guard(plan, "ask", runtime, consent=lambda _plan: False)

    assert result.allowed is False
    assert runtime.calls == []
    assert "consent_denied" in result.reasons


def test_successful_owned_unload_requires_proof_and_safe_recalculation():
    plan = guard.plan_memory_guard(12.0, _hardware(), [_resident()])
    runtime = _Runtime()
    refreshed = guard.plan_memory_guard(
        12.0,
        _hardware(ram_available_gb=15.0),
        [],
    )

    result = guard.execute_guard(
        plan,
        "ask",
        runtime,
        consent=lambda _plan: True,
        recalculate=lambda: refreshed,
    )

    assert result.allowed is True
    assert [resident.model_id for resident in result.unloaded] == ["old"]
    assert runtime.calls == [("unload", "old"), ("check", "old")]


def test_recalculation_can_release_multiple_owned_models_until_safe():
    residents = (_resident("large", size=3.0), _resident("small", size=1.0))
    plan = guard.plan_memory_guard(12.0, _hardware(), residents)
    runtime = _Runtime()
    refreshed = iter(
        [
            guard.plan_memory_guard(12.0, _hardware(), [_resident("small", size=1.0)]),
            guard.plan_memory_guard(12.0, _hardware(ram_available_gb=15.0), []),
        ]
    )

    result = guard.execute_guard(
        plan,
        "ask",
        runtime,
        consent=lambda _plan: True,
        recalculate=lambda: next(refreshed),
    )

    assert result.allowed is True
    assert [resident.model_id for resident in result.unloaded] == ["large", "small"]


@pytest.mark.parametrize(
    ("runtime", "reason"),
    [(_Runtime(unload=False), "unload_failed"), (_Runtime(still_resident=True), "unload_not_confirmed")],
)
def test_failed_or_unconfirmed_unload_blocks_the_new_load(runtime, reason):
    plan = guard.plan_memory_guard(12.0, _hardware(), [_resident()])

    result = guard.execute_guard(plan, "ask", runtime, consent=lambda _plan: True)

    assert result.allowed is False
    assert reason in result.reasons


def test_observe_policy_never_unloads_and_allows_observation():
    plan = guard.plan_memory_guard(12.0, _hardware(), [_resident()])
    runtime = _Runtime()

    result = guard.execute_guard(plan, "observe", runtime)

    assert result.allowed is True
    assert result.decision is guard.GuardDecision.WARN
    assert runtime.calls == []


def test_transient_pressure_does_not_cancel():
    monitor = guard.SustainedPressureMonitor(2.0, required_consecutive_samples=3, low_memory_seconds=2)

    assert monitor.observe(1.0, operation_owned_by_omm=True, captured_at=0) is guard.PressureAction.CONTINUE
    assert monitor.observe(3.0, operation_owned_by_omm=True, captured_at=1) is guard.PressureAction.CONTINUE
    assert monitor.observe(1.0, operation_owned_by_omm=True, captured_at=2) is guard.PressureAction.CONTINUE


def test_sustained_pressure_cancels_only_current_omm_owned_operation():
    unowned = guard.SustainedPressureMonitor(2.0, required_consecutive_samples=3, low_memory_seconds=2)
    owned = guard.SustainedPressureMonitor(2.0, required_consecutive_samples=3, low_memory_seconds=2)
    for timestamp in (0, 1):
        unowned.observe(1.0, operation_owned_by_omm=False, captured_at=timestamp)
        owned.observe(1.0, operation_owned_by_omm=True, captured_at=timestamp)

    assert unowned.observe(1.0, operation_owned_by_omm=False, captured_at=2) is guard.PressureAction.CONTINUE
    assert owned.observe(1.0, operation_owned_by_omm=True, captured_at=2) is guard.PressureAction.CANCEL_OWNED_OPERATION


def test_runtime_watcher_calls_only_the_supplied_owned_operation_cancellation():
    cancelled = []
    monitor = guard.SustainedPressureMonitor(
        2.0, required_consecutive_samples=2, low_memory_seconds=0
    )

    with guard.RuntimePressureWatcher(
        monitor,
        sample_available_gb=lambda: 1.0,
        operation_owned_by_omm=True,
        cancel_owned_operation=lambda: cancelled.append("current") or True,
        poll_seconds=0.1,
        max_samples=2,
    ) as watcher:
        watcher._thread.join(timeout=1)

    assert cancelled == ["current"]
    assert watcher.pressure_observed is True
    assert watcher.pressure_triggered is True
    assert watcher.cancelled is True


def test_runtime_watcher_reports_failed_owned_operation_cancellation():
    monitor = guard.SustainedPressureMonitor(
        2.0, required_consecutive_samples=2, low_memory_seconds=0
    )

    with guard.RuntimePressureWatcher(
        monitor,
        sample_available_gb=lambda: 1.0,
        operation_owned_by_omm=True,
        cancel_owned_operation=lambda: False,
        poll_seconds=0.1,
        max_samples=2,
    ) as watcher:
        watcher._thread.join(timeout=1)

    assert watcher.pressure_triggered is True
    assert watcher.pressure_observed is True
    assert watcher.cancelled is False


def test_runtime_watcher_records_endpoints_minimum_and_transient_pressure():
    values = [2.0, 1.0, 2.0]

    def sample():
        return values.pop(0) if values else 2.0

    monitor = guard.SustainedPressureMonitor(
        1.5, required_consecutive_samples=3, low_memory_seconds=10
    )
    with guard.RuntimePressureWatcher(
        monitor,
        sample_available_gb=sample,
        operation_owned_by_omm=True,
        cancel_owned_operation=lambda: True,
        poll_seconds=0.1,
        max_samples=2,
    ) as watcher:
        watcher._thread.join(timeout=1)

    assert watcher.first_available_gb == 2.0
    assert watcher.minimum_available_gb == 1.0
    assert watcher.last_available_gb == 2.0
    assert watcher.pressure_observed is True
    assert watcher.pressure_triggered is False


def test_runtime_watcher_bounds_a_stuck_cancellation_callback():
    release = threading.Event()
    cancellation_started = threading.Event()
    monitor = guard.SustainedPressureMonitor(
        2.0, required_consecutive_samples=2, low_memory_seconds=0
    )
    watcher = guard.RuntimePressureWatcher(
        monitor,
        sample_available_gb=lambda: 1.0,
        operation_owned_by_omm=True,
        cancel_owned_operation=lambda: cancellation_started.set() or release.wait(1.0),
        poll_seconds=0.1,
        max_samples=2,
        cancel_wait_seconds=0.1,
    )

    try:
        with watcher:
            assert cancellation_started.wait(1.0)
        assert watcher.pressure_triggered is True
        assert watcher.cancellation_timed_out is True
        assert watcher.cancelled is False
    finally:
        release.set()
        watcher._thread.join(timeout=1)


def test_registry_ownership_is_required_and_model_names_are_not_guessed():
    registry_data = {
        "owned.gguf": {"linked": {"ollama": True}, "ollama_name": "owned"},
        "not-linked.gguf": {"linked": {"ollama": False}, "ollama_name": "other"},
    }

    assert guard.omm_managed_model_ids(registry_data, "ollama") == {"owned"}


def test_registry_ownership_prefers_exact_ollama_runtime_name():
    registry_data = {
        "qwen3-4b.gguf": {
            "linked": {"ollama": True},
            "ollama_name": "qwen3-4b",
            "ollama_runtime_name": "qwen3:4b",
        }
    }

    assert guard.omm_managed_model_ids(registry_data, "ollama") == {"qwen3:4b"}


class _FakeLmStudioAdapter:
    def __init__(self, models):
        self._models = models
        self.unload_calls = []

    def list_models(self):
        return self._models

    def unload(self, receipt):
        from omm.engines.base import UnloadResult

        self.unload_calls.append(receipt.instance_id)
        return UnloadResult(True)


def test_lmstudio_runtime_marks_registry_linked_models_owned_with_file_size():
    from omm.engines.base import RuntimeModel

    registry_data = {
        "model.gguf": {
            "linked": {"lmstudio": True},
            "repo_id": "acme/widget",
            "size_bytes": 4 * 1024**3,
        },
        "not-linked.gguf": {
            "linked": {"lmstudio": False},
            "repo_id": "other/other",
            "size_bytes": 8 * 1024**3,
        },
    }
    runtime = guard.LMStudioManagedRuntime(registry_data)
    fake_adapter = _FakeLmStudioAdapter(
        [
            RuntimeModel("acme/widget", "Widget", True, "instance-1"),
            RuntimeModel("some/unmanaged-model", "Unmanaged", True, "instance-2"),
            RuntimeModel("acme/widget", "Widget", False, None),
        ]
    )
    runtime._adapter = lambda: fake_adapter

    residents = runtime.list_residents()

    assert len(residents) == 2  # only the two `loaded=True` rows
    owned = next(r for r in residents if r.model_id == "acme/widget")
    unmanaged = next(r for r in residents if r.model_id == "some/unmanaged-model")
    assert owned.owned_by_omm is True
    assert owned.size_gb == pytest.approx(4.0)
    assert owned.receipt_id == "instance-1"
    assert unmanaged.owned_by_omm is False
    assert unmanaged.size_gb == 0.0


def test_lmstudio_runtime_unload_forces_release_of_an_owned_resident():
    runtime = guard.LMStudioManagedRuntime({})
    fake_adapter = _FakeLmStudioAdapter([])
    runtime._adapter = lambda: fake_adapter
    resident = guard.ResidentModel(
        "lmstudio", "acme/widget", 4.0, True, receipt_id="instance-1"
    )

    assert runtime.unload(resident) is True
    assert fake_adapter.unload_calls == ["instance-1"]


def test_lmstudio_runtime_unload_refuses_unowned_residents_without_calling_the_adapter():
    runtime = guard.LMStudioManagedRuntime({})
    fake_adapter = _FakeLmStudioAdapter([])
    runtime._adapter = lambda: fake_adapter
    resident = guard.ResidentModel(
        "lmstudio", "acme/widget", 4.0, False, receipt_id="instance-1"
    )

    assert runtime.unload(resident) is False
    assert fake_adapter.unload_calls == []


def test_lmstudio_runtime_is_resident_reflects_the_live_list():
    from omm.engines.base import RuntimeModel

    runtime = guard.LMStudioManagedRuntime({})
    resident = guard.ResidentModel(
        "lmstudio", "acme/widget", 4.0, True, receipt_id="instance-1"
    )

    runtime._adapter = lambda: _FakeLmStudioAdapter(
        [RuntimeModel("acme/widget", "Widget", True, "instance-1")]
    )
    assert runtime.is_resident(resident) is True

    runtime._adapter = lambda: _FakeLmStudioAdapter([])
    assert runtime.is_resident(resident) is None


def test_registry_ownership_is_required_and_model_names_are_not_guessed_for_lmstudio():
    registry_data = {
        "owned.gguf": {"linked": {"lmstudio": True}, "repo_id": "acme/owned"},
        "not-linked.gguf": {"linked": {"lmstudio": False}, "repo_id": "acme/other"},
    }

    assert guard.omm_managed_model_ids(registry_data, "lmstudio") == {"owned.gguf"}


def test_module_has_no_process_kill_or_privilege_escalation_path():
    source = inspect.getsource(guard)

    assert "sudo" not in source
    assert "os.kill" not in source
    assert "subprocess" not in source
