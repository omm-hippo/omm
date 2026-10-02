from __future__ import annotations

from types import SimpleNamespace
import pytest

from omm import config, registry, runtime_compatibility
from omm.engines import LoadReceipt, ProbeRequest, ProbeResult, RuntimeHealth, RuntimeModel, RuntimeModelRef, UnloadResult
from omm.web import probe


class Runtime:
    key = 'ollama'
    def __init__(self, *, preloaded=False, reachable=True, unloads=True):
        self.preloaded, self.reachable, self.unloads = preloaded, reachable, unloads
        self.calls = []
    def health(self):
        return RuntimeHealth(self.reachable, version='test', failure_reason=None if self.reachable else 'server_unavailable')
    def list_models(self):
        return [RuntimeModel('model', 'model', self.preloaded)]
    def load(self, model, options):
        self.calls.append('load')
        return LoadReceipt(RuntimeModel('model', 'model', True), 'model', self.preloaded, not self.preloaded)
    def generate(self, receipt, request):
        self.calls.append('generate')
        assert request.max_output_tokens == 8 and request.timeout_seconds == 30
        return ProbeResult('OK')
    def unload(self, receipt):
        self.calls.append('unload')
        return UnloadResult(self.unloads, None if self.unloads else 'unload_failed')


@pytest.fixture
def prepared(monkeypatch, isolated_omm_home):
    path = config.MODELS_DIR / 'model.gguf'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'GGUF')
    entry = {'linked': {'ollama': True}, 'ollama_name': 'model'}
    registry.save_registry({'model.gguf': entry})
    monkeypatch.setattr(probe.runtime_profiles, 'saved_options_for_file', lambda *args: None)
    monkeypatch.setattr(probe.hardware, 'scan_hardware', lambda: object())
    monkeypatch.setattr(probe.memory_guard, 'plan_memory_guard', lambda *args: SimpleNamespace(decision=probe.memory_guard.GuardDecision.SAFE))
    return entry


@pytest.mark.parametrize('preloaded', [False, True])
def test_response_and_owned_cleanup_are_reported(monkeypatch, prepared, preloaded):
    runtime = Runtime(preloaded=preloaded)
    monkeypatch.setattr(probe, 'adapter_for', lambda engine: runtime)
    value = probe.check('model.gguf', prepared, 'ollama')
    assert value['response'] == 'OK' and value['runtime_verified'] is True
    assert ('unload' in runtime.calls) is not preloaded
    assert value['released_test_load'] is not preloaded
    saved = registry.load_registry()['model.gguf']['compatibility']['ollama']
    assert saved['status'] == 'passed' and 'response' not in saved


def test_unavailable_server_does_not_load(monkeypatch, prepared):
    runtime = Runtime(reachable=False)
    monkeypatch.setattr(probe, 'adapter_for', lambda engine: runtime)
    value = probe.check('model.gguf', prepared, 'ollama')
    assert value['runtime_verified'] is False and value['response'] is None
    assert runtime.calls == []
    assert value['compatibility']['failure_reason'] == 'server_unavailable'


def test_memory_pressure_stops_before_loading(monkeypatch, prepared):
    runtime = Runtime()
    monkeypatch.setattr(probe, 'adapter_for', lambda engine: runtime)
    monkeypatch.setattr(probe.memory_guard, 'plan_memory_guard', lambda *args: SimpleNamespace(decision=probe.memory_guard.GuardDecision.BLOCK))
    value = probe.check('model.gguf', prepared, 'ollama')
    assert value['compatibility']['failure_reason'] == 'out_of_memory'
    assert runtime.calls == []


def test_unload_failure_is_not_reported_as_success(monkeypatch, prepared):
    runtime = Runtime(unloads=False)
    monkeypatch.setattr(probe, 'adapter_for', lambda engine: runtime)
    value = probe.check('model.gguf', prepared, 'ollama')
    assert value['response'] == 'OK' and value['runtime_verified'] is False
    assert value['released_test_load'] is False
    assert value['compatibility']['failure_reason'] == 'unload_failed'


def test_response_observer_failure_still_releases_owned_load():
    runtime = Runtime()
    def bad_observer(response):
        raise ValueError('observer failed')
    with pytest.raises(ValueError):
        runtime_compatibility.verify_runtime(runtime, RuntimeModelRef('model'), on_response=bad_observer, probe_request=ProbeRequest(timeout_seconds=30))
    assert runtime.calls[-1] == 'unload'


def test_runtime_environment_cannot_target_a_remote_host(monkeypatch):
    monkeypatch.setenv('OLLAMA_HOST', 'https://example.com')
    with pytest.raises(ValueError, match='loopback'):
        probe.adapter_for('ollama')
