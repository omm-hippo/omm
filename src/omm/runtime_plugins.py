"""Explicit, reviewed Python runtime extensions over the existing adapter API.

Registration is in-process and opt-in. This is not a sandbox or a package loader.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from omm.engines.base import (
    LoadReceipt, ProbeResult, RuntimeAdapterError, RuntimeHealth, RuntimeModel,
    UnloadResult,
)

CONTRACT_VERSION = 1
CAPABILITIES = frozenset({"health", "list_models", "load", "generate", "unload"})
BUILTINS = frozenset({"ollama", "lmstudio"})


class PluginError(ValueError):
    """A contract or registration failure without private plugin exception text."""


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    contract_version: int = CONTRACT_VERSION
    capabilities: frozenset[str] = CAPABILITIES


class _CheckedAdapter:
    def __init__(self, name, adapter):
        self.key = name
        self._adapter = adapter

    def _call(self, method, expected, *args):
        try:
            value = getattr(self._adapter, method)(*args)
            if not isinstance(value, expected):
                raise TypeError("invalid result")
            if method == "list_models" and not all(isinstance(v, RuntimeModel) for v in value):
                raise TypeError("invalid model list")
            return value
        except RuntimeAdapterError as error:
            # Preserve the classified reason, not arbitrary messages containing
            # prompts, filesystem paths or credentials from an external runtime.
            raise RuntimeAdapterError(error.reason, "Runtime plugin request failed.") from None
        except Exception:
            raise RuntimeAdapterError("unknown", "Runtime plugin violated its contract or failed.") from None

    def health(self):
        return self._call("health", RuntimeHealth)

    def list_models(self):
        return self._call("list_models", list)

    def load(self, model, options):
        return self._call("load", LoadReceipt, model, options)

    def generate(self, receipt, request):
        return self._call("generate", ProbeResult, receipt, request)

    def unload(self, receipt):
        return self._call("unload", UnloadResult, receipt)


class Registry:
    """A caller-owned registry. Nothing is discovered or installed on import."""

    def __init__(self):
        self._plugins: dict[str, tuple[Manifest, Callable]] = {}

    def register(self, manifest: Manifest, factory: Callable, *, trusted=False):
        if trusted is not True:
            raise PluginError("Review the Python extension and explicitly approve it with trusted=True.")
        if not isinstance(manifest, Manifest):
            raise PluginError("A runtime plugin manifest is required.")
        if type(manifest.contract_version) is not int or manifest.contract_version != CONTRACT_VERSION:
            raise PluginError("Runtime plugin contract version is unsupported; expected version 1.")
        if not isinstance(manifest.name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", manifest.name):
            raise PluginError("Runtime plugin names must use lowercase letters, digits and hyphens.")
        if manifest.name in BUILTINS or manifest.name in self._plugins:
            raise PluginError("Runtime plugin name is reserved or already registered.")
        if not isinstance(manifest.version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", manifest.version):
            raise PluginError("A semantic plugin version is required.")
        if manifest.capabilities != CAPABILITIES or not callable(factory):
            raise PluginError("Runtime plugins must implement health, list_models, load, generate and unload.")
        self._plugins[manifest.name] = (manifest, factory)

    def names(self):
        return tuple(sorted(BUILTINS | self._plugins.keys()))

    def remove(self, name):
        if name in BUILTINS:
            raise PluginError("Built-in runtime adapters cannot be removed.")
        return self._plugins.pop(name, None) is not None

    def create(self, name, **options):
        if name == "ollama":
            from omm.engines.ollama import OllamaAdapter
            return OllamaAdapter(**options)
        if name == "lmstudio":
            from omm.engines.lmstudio import LMStudioAdapter
            return LMStudioAdapter(**options)
        if name not in self._plugins:
            raise PluginError("Unknown runtime plugin. Register an explicitly reviewed factory first.")
        try:
            adapter = self._plugins[name][1](**options)
            if adapter.key != name or any(not callable(getattr(adapter, method, None)) for method in CAPABILITIES):
                raise TypeError("invalid adapter")
        except Exception:
            raise PluginError("Runtime plugin construction failed or the adapter contract is incomplete.") from None
        return _CheckedAdapter(name, adapter)
