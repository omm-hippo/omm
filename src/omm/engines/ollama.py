"""Ollama runtime adapter using its loopback REST API."""

from __future__ import annotations

import time

from omm.engines.base import (
    LoadOptions,
    LoadReceipt,
    LoopbackJsonClient,
    ProbeRequest,
    ProbeResult,
    RuntimeAdapterError,
    RuntimeHealth,
    RuntimeModel,
    RuntimeModelRef,
    UnloadResult,
    find_runtime_model,
)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


class OllamaAdapter:
    key = "ollama"

    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL) -> None:
        self._client = LoopbackJsonClient(base_url)

    def health(self) -> RuntimeHealth:
        try:
            response = self._client.request(
                "GET", "/api/version", timeout=5, default_failure="server_unavailable"
            )
        except RuntimeAdapterError as error:
            return RuntimeHealth(False, failure_reason=error.reason)
        version = response.data.get("version")
        if not isinstance(version, str) or not version or len(version) > 64:
            version = None
        return RuntimeHealth(True, version=version)

    def list_models(self) -> list[RuntimeModel]:
        available = self._client.request(
            "GET", "/api/tags", timeout=10, default_failure="server_unavailable"
        ).data.get("models")
        running = self._client.request(
            "GET", "/api/ps", timeout=10, default_failure="server_unavailable"
        ).data.get("models")
        if not isinstance(available, list) or not isinstance(running, list):
            raise RuntimeAdapterError("unknown", "Ollama returned an invalid model list")
        loaded_names = {
            value
            for row in running
            if isinstance(row, dict)
            for value in (row.get("name"), row.get("model"))
            if isinstance(value, str)
        }
        models = []
        for row in available:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("model")
            if not isinstance(name, str) or not name:
                continue
            aliases = [name.removesuffix(":latest")]
            model_value = row.get("model")
            if isinstance(model_value, str):
                aliases.append(model_value)
            loaded = any(
                running_name == name
                or (":" not in name and running_name == f"{name}:latest")
                or (":" not in running_name and name == f"{running_name}:latest")
                for running_name in loaded_names
            )
            models.append(
                RuntimeModel(name, name, loaded, name if loaded else None, tuple(aliases))
            )
        return models

    def load(self, model: RuntimeModelRef, options: LoadOptions) -> LoadReceipt:
        selected = find_runtime_model(self.list_models(), model)
        if selected is None:
            raise RuntimeAdapterError("model_not_visible", "the model is not visible in Ollama")
        if selected.loaded:
            return LoadReceipt(selected, selected.instance_id or selected.key, True, False)
        try:
            self._client.request(
                "POST",
                "/api/generate",
                payload={
                    "model": selected.key,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": -1,
                    "options": {"num_ctx": options.context_length},
                },
                timeout=120,
                default_failure="load_failed",
                timeout_failure="load_failed",
            )
            refreshed = find_runtime_model(self.list_models(), RuntimeModelRef(selected.key))
            if refreshed is None or not refreshed.loaded:
                raise RuntimeAdapterError("load_failed", "Ollama did not report the model as loaded")
            return LoadReceipt(refreshed, refreshed.instance_id or refreshed.key, False, True)
        except RuntimeAdapterError as original:
            try:
                uncertain = find_runtime_model(
                    self.list_models(), RuntimeModelRef(selected.key)
                )
                if uncertain is not None and uncertain.loaded:
                    cleanup = self.unload(
                        LoadReceipt(
                            uncertain,
                            uncertain.instance_id or uncertain.key,
                            False,
                            True,
                        )
                    )
                    if not cleanup.unloaded:
                        raise RuntimeAdapterError(
                            "unload_failed", "Ollama load failed and cleanup was not confirmed"
                        ) from original
            except RuntimeAdapterError as cleanup_error:
                if cleanup_error.reason == "unload_failed":
                    raise
            raise original

    def generate(self, receipt: LoadReceipt, request: ProbeRequest) -> ProbeResult:
        base_payload = {
            "model": receipt.model.key,
            "prompt": request.prompt,
            "stream": False,
            "keep_alive": -1,
            "options": {
                "temperature": 0,
                "seed": 0,
                "num_predict": request.max_output_tokens,
            },
        }
        try:
            response = self._client.request(
                "POST",
                "/api/generate",
                # Reasoning models can spend the entire bounded probe budget
                # in Ollama's separate `thinking` field and leave `response`
                # empty. The compatibility probe needs a short observable
                # answer, not hidden chain-of-thought.
                payload={**base_payload, "think": False},
                timeout=request.timeout_seconds,
                default_failure="unknown",
                timeout_failure="generation_timeout",
            ).data
        except RuntimeAdapterError as error:
            # Ollama rejects the top-level `think` field with a "does not
            # support thinking" 400 for any model whose capabilities don't
            # list thinking - even when the value is False. That surfaces
            # here as "unsupported_runtime" and is not a real compatibility
            # failure, so retry once without the field before giving up.
            if error.reason != "unsupported_runtime":
                raise
            response = self._client.request(
                "POST",
                "/api/generate",
                payload=base_payload,
                timeout=request.timeout_seconds,
                default_failure="unknown",
                timeout_failure="generation_timeout",
            ).data
        text = response.get("response")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeAdapterError("empty_response", "Ollama returned no text")
        return ProbeResult(text)

    def unload(self, receipt: LoadReceipt) -> UnloadResult:
        if not receipt.loaded_by_omm:
            return UnloadResult(True)
        try:
            self._client.request(
                "POST",
                "/api/generate",
                payload={"model": receipt.model.key, "stream": False, "keep_alive": 0},
                timeout=30,
                default_failure="unload_failed",
                timeout_failure="unload_failed",
            )
            for attempt in range(4):
                selected = find_runtime_model(
                    self.list_models(), RuntimeModelRef(receipt.model.key)
                )
                if selected is None or not selected.loaded:
                    return UnloadResult(True)
                if attempt < 3:
                    time.sleep(0.1)
        except RuntimeAdapterError:
            return UnloadResult(False, "unload_failed")
        return UnloadResult(False, "unload_failed")
