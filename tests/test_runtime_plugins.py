from types import SimpleNamespace

import pytest

from omm.engines.base import (
    LoadReceipt, ProbeResult, RuntimeAdapterError, RuntimeHealth, RuntimeModel,
    RuntimeModelRef, UnloadResult,
)
from omm.runtime_compatibility import verify_runtime
from omm.runtime_plugins import CAPABILITIES, Manifest, PluginError, Registry


class Example:
    key = "example"

    def __init__(self):
        self.calls = []

    def health(self):
        return RuntimeHealth(True, "1.0")

    def list_models(self):
        return [RuntimeModel("model", "Model", False)]

    def load(self, model, options):
        self.calls.append("load")
        return LoadReceipt(RuntimeModel(model.key, model.key, False), model.key,
                           was_already_loaded=False, loaded_by_omm=True)

    def generate(self, receipt, request):
        self.calls.append("generate")
        return ProbeResult("OK")

    def unload(self, receipt):
        self.calls.append("unload")
        return UnloadResult(True)


def test_example_runs_existing_lifecycle_then_removes_without_touching_builtins():
    registry = Registry()
    original = registry.names()
    example = Example()
    registry.register(Manifest("example", "1.0.0"), lambda: example, trusted=True)
    result = verify_runtime(registry.create("example"), RuntimeModelRef("model"))
    assert result.status == "passed", result
    assert example.calls == ["load", "generate", "unload"]
    assert registry.remove("example")
    assert registry.names() == original == ("lmstudio", "ollama")
    assert registry.create("ollama").key == "ollama"
    assert registry.create("lmstudio").key == "lmstudio"
    with pytest.raises(PluginError, match="Unknown"):
        registry.create("example")


@pytest.mark.parametrize("manifest, message", [
    (Manifest("example", "1.0.0", contract_version=2), "version"),
    (Manifest("example", "1.0.0", contract_version=True), "version"),
    (Manifest("ollama", "1.0.0"), "reserved"),
    (Manifest("bad/path", "1.0.0"), "names"),
    (Manifest("example", "latest"), "semantic"),
    (Manifest("example", "1.0.0", capabilities=frozenset({"generate"})), "implement"),
    (Manifest("example", "1.0.0", capabilities=CAPABILITIES | {"shell"}), "implement"),
])
def test_invalid_contract_never_calls_factory(manifest, message):
    registry = Registry()
    calls = []
    with pytest.raises(PluginError, match=message):
        registry.register(manifest, lambda: calls.append(True), trusted=True)
    assert not calls


def test_explicit_approval_duplicate_and_builtin_removal():
    registry = Registry()
    with pytest.raises(PluginError, match="Review"):
        registry.register(Manifest("example", "1.0.0"), Example)
    registry.register(Manifest("example", "1.0.0"), Example, trusted=True)
    with pytest.raises(PluginError, match="already"):
        registry.register(Manifest("example", "2.0.0"), Example, trusted=True)
    with pytest.raises(PluginError, match="cannot be removed"):
        registry.remove("ollama")


@pytest.mark.parametrize("factory", [lambda: None, lambda: SimpleNamespace(key="example"), lambda: 1 / 0])
def test_broken_factory_is_classified(factory):
    registry = Registry()
    registry.register(Manifest("example", "1.0.0"), factory, trusted=True)
    with pytest.raises(PluginError, match="construction failed"):
        registry.create("example")


def test_generation_failure_releases_owned_load_and_redacts_exception():
    registry = Registry()
    example = Example()
    def fail(*args):
        raise RuntimeError("private-token-and-prompt")
    example.generate = fail
    registry.register(Manifest("example", "1.0.0"), lambda: example, trusted=True)
    result = verify_runtime(registry.create("example"), RuntimeModelRef("model"))
    assert result.status == "failed"
    assert result.failure_reason == "unknown"
    assert example.calls == ["load", "unload"]
    assert "private-token" not in str(result)


def test_result_type_and_classified_error_contracts():
    registry = Registry()
    example = Example()
    example.list_models = lambda: ["not-a-model"]
    registry.register(Manifest("example", "1.0.0"), lambda: example, trusted=True)
    adapter = registry.create("example")
    with pytest.raises(RuntimeAdapterError, match="contract"):
        adapter.list_models()
    def fail():
        raise RuntimeAdapterError("server_unavailable", "private-token")
    example.health = fail
    with pytest.raises(RuntimeAdapterError) as error:
        adapter.health()
    assert error.value.reason == "server_unavailable"
    assert "private-token" not in str(error.value)


def test_shipped_example_can_be_registered_and_removed_without_network():
    from examples.runtime_plugin import register_example
    registry = Registry()
    register_example(registry)
    assert registry.create("example-ollama").key == "example-ollama"
    assert registry.remove("example-ollama")
    assert registry.names() == ("lmstudio", "ollama")
