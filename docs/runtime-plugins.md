# Reviewed runtime extension contract (v1)

Related issue: #431. This first contract covers runtime adapters used by
`runtime_compatibility.verify_runtime`. It does not install Python packages or
add arbitrary engines to the model download/linker/benchmark CLI. Model source
and document parser contracts remain future work; `documents.py` is unchanged.

## Declare, approve, use, remove

```python
from omm.runtime_plugins import Manifest, Registry
from omm.runtime_compatibility import verify_runtime
from omm.engines import RuntimeModelRef
from reviewed_extension import ExampleAdapter

registry = Registry()
registry.register(Manifest("example", "1.0.0"), ExampleAdapter, trusted=True)
adapter = registry.create("example")
result = verify_runtime(adapter, RuntimeModelRef("installed-model"))
registry.remove("example")
```

The factory is a reviewed Python callable, not an import path or a shell
command. Registration/creation are separate from model loading. `trusted=True`
is an explicit caller decision, not proof of safety: importing Python already
executes it with the process's filesystem and network permissions. Review
source and dependencies before importing an extension. There is no sandbox.
OMM does not auto-discover installed entry points or expose a plugin install API.

The manifest includes a unique lowercase name, semantic package version,
contract version `1`, and all five capabilities: `health`, `list_models`, `load`,
`generate`, `unload`. Built-in `ollama` and `lmstudio` names cannot be replaced
or removed. Registry state is owned by the caller and is not persisted.
Removing an entry prevents new construction; already created adapters remain
the caller's responsibility, so unload owned models before discarding them.

## Inputs, results and failures

The signatures and result dataclasses are the existing
`omm.engines.base.RuntimeAdapter` protocol. `load` accepts `RuntimeModelRef`
and `LoadOptions`; `generate` accepts `LoadReceipt` and `ProbeRequest`;
`unload` accepts that receipt. Preexisting loads must have
`loaded_by_omm=False`; OMM releases only loads it owns.

Construction validates callable methods and matching adapter key. The wrapper
checks result types and converts unexpected failures to a classified
`RuntimeAdapterError`, without copying arbitrary exception text into output.
Plugins should themselves raise `RuntimeAdapterError` with a supported failure
reason. Contract/registration errors use `PluginError`. Validation is not a
security boundary against malicious Python or a complete semantic conformance
test. Run the actual adapter against its real service before claiming support.

`examples/runtime_plugin.py` extends the existing Ollama adapter and demonstrates
register/create/remove without network access. Tests exercise a contract fixture
through the real compatibility lifecycle, including failure cleanup. This is
not live-service verification of an additional runtime or MLX support (#29).
