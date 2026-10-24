"""A reviewed example: reuse Ollama's adapter with an independently named runtime.

From the repository root: PYTHONPATH=src python examples/runtime_plugin.py
Registration and creation make no requests. Pass the returned adapter to
runtime_compatibility.verify_runtime only when ready to load a local model.
"""
from omm.engines.ollama import OllamaAdapter
from omm.runtime_plugins import Manifest, Registry


class ExampleOllama(OllamaAdapter):
    key = "example-ollama"


def register_example(registry):
    registry.register(Manifest("example-ollama", "1.0.0"), ExampleOllama, trusted=True)


if __name__ == "__main__":
    registry = Registry()
    register_example(registry)
    adapter = registry.create("example-ollama")
    print(f"Registered {adapter.key}; no model was loaded.")
    registry.remove("example-ollama")
    print("Removed example; built-in adapters remain:", ", ".join(registry.names()))
