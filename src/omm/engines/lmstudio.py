"""LM Studio native v1 runtime adapter."""

from __future__ import annotations

import os
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

DEFAULT_LMSTUDIO_URL = "http://127.0.0.1:1234"


class LMStudioAdapter:
    key = "lmstudio"

    def __init__(
        self,
        base_url: str = DEFAULT_LMSTUDIO_URL,
        *,
        api_token: str | None = None,
    ) -> None:
        token = api_token if api_token is not None else os.environ.get("LM_API_TOKEN")
        self._client = LoopbackJsonClient(base_url, token=token)

    def health(self) -> RuntimeHealth:
        try:
            response = self._client.request(
                "GET",
                "/api/v1/models",
                timeout=5,
                default_failure="unsupported_runtime",
            )
        except RuntimeAdapterError as error:
            return RuntimeHealth(False, failure_reason=error.reason)
        version = response.headers.get("x-lm-studio-version")
        if not version or len(version) > 64:
            version = None
        return RuntimeHealth(True, version=version)

    def _model_rows(self) -> list[dict]:
        rows = self._client.request(
            "GET",
            "/api/v1/models",
            timeout=10,
            default_failure="unsupported_runtime",
        ).data.get("models")
        if not isinstance(rows, list):
            raise RuntimeAdapterError("unknown", "LM Studio returned an invalid model list")
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _instance_ids(row: dict) -> tuple[str, ...]:
        instances = row.get("loaded_instances")
        if not isinstance(instances, list):
            return ()
        return tuple(
            instance["id"]
            for instance in instances
            if isinstance(instance, dict)
            and isinstance(instance.get("id"), str)
            and instance["id"]
        )

    def list_models(self) -> list[RuntimeModel]:
        models = []
        for row in self._model_rows():
            if row.get("type") != "llm":
                continue
            key = row.get("key")
            if not isinstance(key, str) or not key:
                continue
            display_name = row.get("display_name")
            if not isinstance(display_name, str) or not display_name:
                display_name = key
            instance_id = next(iter(self._instance_ids(row)), None)
            aliases = []
            for field in ("variants",):
                values = row.get(field)
                if isinstance(values, list):
                    aliases.extend(value for value in values if isinstance(value, str))
            selected_variant = row.get("selected_variant")
            if isinstance(selected_variant, str):
                aliases.append(selected_variant)
            models.append(
                RuntimeModel(key, display_name, instance_id is not None, instance_id, tuple(aliases))
            )
        return models

    def load(self, model: RuntimeModelRef, options: LoadOptions) -> LoadReceipt:
        selected = find_runtime_model(self.list_models(), model)
        if selected is None:
            raise RuntimeAdapterError("model_not_visible", "the model is not visible in LM Studio")
        if selected.loaded:
            return LoadReceipt(selected, selected.instance_id or selected.key, True, False)
        try:
            response = self._client.request(
                "POST",
                "/api/v1/models/load",
                payload={"model": selected.key, "context_length": options.context_length},
                timeout=120,
                default_failure="load_failed",
                timeout_failure="load_failed",
            ).data
            instance_id = response.get("instance_id")
            if response.get("status") != "loaded" or not isinstance(instance_id, str) or not instance_id:
                raise RuntimeAdapterError("load_failed", "LM Studio did not confirm the model load")
            loaded_model = RuntimeModel(
                selected.key,
                selected.display_name,
                True,
                instance_id,
                selected.aliases,
            )
            return LoadReceipt(loaded_model, instance_id, False, True)
        except RuntimeAdapterError as original:
            try:
                uncertain = find_runtime_model(
                    self.list_models(), RuntimeModelRef(selected.key, selected.aliases)
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
                            "unload_failed",
                            "LM Studio load failed and cleanup was not confirmed",
                        ) from original
            except RuntimeAdapterError as cleanup_error:
                if cleanup_error.reason == "unload_failed":
                    raise
            raise original

    def generate(self, receipt: LoadReceipt, request: ProbeRequest) -> ProbeResult:
        response = self._client.request(
            "POST",
            "/api/v1/chat",
            payload={
                "model": receipt.instance_id,
                "input": request.prompt,
                "stream": False,
                "store": False,
                "temperature": 0,
                "max_output_tokens": request.max_output_tokens,
            },
            timeout=request.timeout_seconds,
            default_failure="unknown",
            timeout_failure="generation_timeout",
        ).data
        output = response.get("output")
        text = "\n".join(
            item.get("content", "")
            for item in output
            if isinstance(item, dict)
            and item.get("type") == "message"
            and isinstance(item.get("content"), str)
        ) if isinstance(output, list) else ""
        if not text.strip():
            raise RuntimeAdapterError("empty_response", "LM Studio returned no text")
        return ProbeResult(text)

    def unload(self, receipt: LoadReceipt) -> UnloadResult:
        if not receipt.loaded_by_omm:
            return UnloadResult(True)
        try:
            self._client.request(
                "POST",
                "/api/v1/models/unload",
                payload={"instance_id": receipt.instance_id},
                timeout=30,
                default_failure="unload_failed",
                timeout_failure="unload_failed",
            )
            for attempt in range(4):
                # A second client may have loaded another instance of this
                # model. Confirm only that the instance OMM owns disappeared.
                still_loaded = any(
                    receipt.instance_id in self._instance_ids(row)
                    for row in self._model_rows()
                )
                if not still_loaded:
                    return UnloadResult(True)
                if attempt < 3:
                    time.sleep(0.1)
        except RuntimeAdapterError:
            return UnloadResult(False, "unload_failed")
        return UnloadResult(False, "unload_failed")
