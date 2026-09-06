from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omm import registry, runtime_compatibility
from omm.engines import (
    LoadReceipt,
    ProbeResult,
    RuntimeAdapterError,
    RuntimeHealth,
    RuntimeModel,
    RuntimeModelRef,
    UnloadResult,
)


class _FakeAdapter:
    key = "ollama"

    def __init__(
        self,
        *,
        preloaded=False,
        generate_reason=None,
        unloads=True,
        unload_reason=None,
        reachable=True,
    ):
        self.preloaded = preloaded
        self.generate_reason = generate_reason
        self.unloads = unloads
        self.unload_reason = unload_reason
        self.reachable = reachable
        self.calls = []

    def health(self):
        self.calls.append("health")
        return RuntimeHealth(
            self.reachable,
            version="1.2.3" if self.reachable else None,
            failure_reason=None if self.reachable else "server_unavailable",
        )

    def list_models(self):
        return []

    def load(self, model, options):
        self.calls.append("load")
        runtime_model = RuntimeModel(model.key, model.key, True, model.key)
        return LoadReceipt(runtime_model, model.key, self.preloaded, not self.preloaded)

    def generate(self, receipt, request):
        self.calls.append("generate")
        if self.generate_reason:
            raise RuntimeAdapterError(self.generate_reason, "probe failed")
        return ProbeResult("OK")

    def unload(self, receipt):
        self.calls.append("unload")
        if self.unload_reason:
            raise RuntimeAdapterError(self.unload_reason, "cleanup failed")
        return UnloadResult(self.unloads, None if self.unloads else "unload_failed")


def _verify(adapter, **kwargs):
    return runtime_compatibility.verify_runtime(
        adapter,
        RuntimeModelRef("model"),
        now=lambda: datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        **kwargs,
    )


def test_successful_probe_unloads_only_omm_load():
    adapter = _FakeAdapter()

    result = _verify(adapter)

    assert result.status == "passed"
    assert result.failure_reason is None
    assert result.runtime_version == "1.2.3"
    assert adapter.calls == ["health", "load", "generate", "unload"]


def test_generation_failure_still_runs_cleanup():
    adapter = _FakeAdapter(generate_reason="empty_response")

    result = _verify(adapter)

    assert result.status == "failed"
    assert result.failure_reason == "empty_response"
    assert adapter.calls[-1] == "unload"


def test_preloaded_model_is_never_unloaded():
    adapter = _FakeAdapter(preloaded=True)

    result = _verify(adapter)

    assert result.status == "passed"
    assert result.model_was_preloaded is True
    assert "unload" not in adapter.calls


def test_keep_loaded_skips_cleanup_only_for_omm_load():
    adapter = _FakeAdapter()

    result = _verify(adapter, keep_loaded=True)

    assert result.status == "passed"
    assert result.model_left_loaded is True
    assert "unload" not in adapter.calls


def test_unload_failure_overrides_probe_success():
    result = _verify(_FakeAdapter(unloads=False))

    assert result.status == "failed"
    assert result.failure_reason == "unload_failed"


def test_unload_exception_is_reported_instead_of_escaping():
    result = _verify(_FakeAdapter(unload_reason="unload_failed"))

    assert result.status == "failed"
    assert result.failure_reason == "unload_failed"


def test_unreachable_server_never_attempts_load():
    adapter = _FakeAdapter(reachable=False)

    result = _verify(adapter)

    assert result.status == "failed"
    assert result.failure_reason == "server_unavailable"
    assert adapter.calls == ["health"]


def test_verify_and_record_atomically_persists_bounded_result(isolated_omm_home):
    registry.save_registry({"model.gguf": {"linked": {"ollama": True}}})

    result = runtime_compatibility.verify_and_record(
        "model.gguf",
        _FakeAdapter(),
        RuntimeModelRef("model"),
        now=lambda: datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
    )

    assert result.status == "passed"
    saved = registry.load_registry()["model.gguf"]["compatibility"]["ollama"]
    assert saved == {
        "status": "passed",
        "checked_at": "2026-07-31T12:00:00+00:00",
        "probe_version": 1,
        "runtime_version": "1.2.3",
        "failure_reason": None,
    }


def test_registry_preserves_other_engine_and_old_entries(isolated_omm_home):
    registry.save_registry(
        {
            "old.gguf": {"linked": {}},
            "model.gguf": {
                "linked": {"ollama": True, "lmstudio": True},
                "compatibility": {"lmstudio": {"status": "passed"}},
            },
        }
    )

    registry.record_compatibility("model.gguf", "ollama", {"status": "failed"})
    loaded = registry.load_registry()

    assert "compatibility" not in loaded["old.gguf"]
    assert loaded["model.gguf"]["compatibility"] == {
        "lmstudio": {"status": "passed"},
        "ollama": {"status": "failed"},
    }


def test_record_compatibility_rejects_missing_registry_entry(isolated_omm_home):
    with pytest.raises(KeyError):
        registry.record_compatibility("missing.gguf", "ollama", {"status": "failed"})


def test_probe_failure_reason_survives_a_failed_unload():
    result = _verify(_FakeAdapter(generate_reason="out_of_memory", unloads=False))

    assert result.status == "failed"
    assert result.failure_reason == "out_of_memory"


def test_probe_failure_reason_survives_an_unload_exception():
    result = _verify(_FakeAdapter(generate_reason="out_of_memory", unload_reason="unload_failed"))

    assert result.status == "failed"
    assert result.failure_reason == "out_of_memory"
