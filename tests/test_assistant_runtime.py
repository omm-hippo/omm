from __future__ import annotations

import json

import pytest

from omm.assistant_runtime import (
    AssistantRuntimeError,
    OllamaAssistantRuntime,
)
from omm.engines.base import JsonResponse, RuntimeAdapterError


class _FakeOllamaClient:
    def __init__(
        self,
        *,
        tags=None,
        loaded=None,
        capabilities=None,
        raw_response=None,
        generation_error: RuntimeAdapterError | None = None,
    ) -> None:
        self.tags = tags or [
            {
                "name": "qwen:small",
                "size": 1_000,
                "details": {"family": "qwen"},
            }
        ]
        self.loaded = loaded or []
        self.capabilities = capabilities or {"qwen:small": ["completion"]}
        self.raw_response = raw_response or json.dumps(
            {"commandId": "verify", "reason": "실제 생성을 확인합니다."},
            ensure_ascii=False,
        )
        self.generation_error = generation_error
        self.calls = []

    def request(
        self,
        method,
        path,
        *,
        payload=None,
        timeout=10,
        default_failure="unknown",
        timeout_failure=None,
    ):
        self.calls.append((method, path, payload, timeout))
        if path == "/api/tags":
            return JsonResponse({"models": self.tags}, {})
        if path == "/api/ps":
            return JsonResponse({"models": [{"name": name} for name in self.loaded]}, {})
        if path == "/api/show":
            name = payload["model"]
            return JsonResponse(
                {
                    "capabilities": self.capabilities.get(name, []),
                    "model_info": {"general.architecture": "llama"},
                    "template": "{{ .Prompt }}",
                },
                {},
            )
        if path == "/api/generate" and payload.get("prompt"):
            if self.generation_error is not None:
                raise self.generation_error
            if payload.get("keep_alive") == -1:
                self.loaded = [payload["model"]]
            return JsonResponse({"response": self.raw_response}, {})
        if path == "/api/generate" and payload.get("keep_alive") == 0:
            self.loaded = [name for name in self.loaded if name != payload["model"]]
            return JsonResponse({"response": ""}, {})
        raise AssertionError(f"unexpected fake request: {method} {path}")


def _runtime(client, **kwargs):
    return OllamaAssistantRuntime(
        "http://127.0.0.1:11434", client=client, **kwargs
    )


def _classify(runtime, *, model=None):
    return runtime.classify(
        "설치한 모델이 실제로 답하는지 확인하고 싶어",
        ("doctor", "verify", "run"),
        "doctor: 상태 진단\nverify: 실제 생성 검증\nrun: 대화 시작",
        model=model,
    )


def _generation_payload(client):
    return next(
        payload
        for method, path, payload, _ in client.calls
        if method == "POST" and path == "/api/generate" and payload.get("prompt")
    )


def test_structured_classification_uses_schema_and_allowlist():
    client = _FakeOllamaClient()

    result = _classify(_runtime(client), model="qwen:small")

    assert result.command_id == "verify"
    assert result.reason == "실제 생성을 확인합니다."
    assert result.model == "qwen:small"
    payload = _generation_payload(client)
    assert payload["format"]["properties"]["commandId"]["enum"] == [
        "doctor",
        "verify",
        "run",
    ]
    assert payload["options"]["num_predict"] == 96
    assert payload["stream"] is False


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"commandId":"verify"}'])
def test_invalid_json_or_fields_fail_closed_without_leaking_response(raw):
    client = _FakeOllamaClient(raw_response=raw)

    with pytest.raises(AssistantRuntimeError) as caught:
        _classify(_runtime(client))

    assert caught.value.code == "invalid_response"
    assert raw not in str(caught.value)
    assert raw not in repr(caught.value)


def test_unknown_command_fails_closed():
    raw = json.dumps({"commandId": "rm-everything", "reason": "do it"})
    client = _FakeOllamaClient(raw_response=raw)

    with pytest.raises(AssistantRuntimeError) as caught:
        _classify(_runtime(client))

    assert caught.value.code == "unknown_command"
    assert "rm-everything" not in caught.value.safe_message


def test_control_characters_in_generated_reason_fail_closed():
    raw = json.dumps({"commandId": "verify", "reason": "safe\u001b[2Jspoof"})
    client = _FakeOllamaClient(raw_response=raw)

    with pytest.raises(AssistantRuntimeError) as caught:
        _classify(_runtime(client))

    assert caught.value.code == "invalid_response"
    assert "spoof" not in caught.value.safe_message


def test_timeout_is_bounded_safe_and_attempts_release():
    secret = "question-or-response-secret"
    client = _FakeOllamaClient(
        generation_error=RuntimeAdapterError(
            "generation_timeout", secret, transport_kind="read_timeout"
        )
    )

    with pytest.raises(AssistantRuntimeError) as caught:
        _classify(_runtime(client, timeout_seconds=7))

    assert caught.value.code == "runtime_timeout"
    assert secret not in str(caught.value)
    generation_calls = [
        (payload, timeout)
        for method, path, payload, timeout in client.calls
        if method == "POST" and path == "/api/generate"
    ]
    assert generation_calls[0][1] == 7
    assert generation_calls[0][0]["keep_alive"] == 0
    assert generation_calls[-1][0] == {
        "model": "qwen:small",
        "stream": False,
        "keep_alive": 0,
    }


def test_runtime_error_does_not_leak_local_response_body():
    secret = "sensitive-local-output"
    client = _FakeOllamaClient(
        generation_error=RuntimeAdapterError("unknown", secret)
    )

    with pytest.raises(AssistantRuntimeError) as caught:
        _classify(_runtime(client))

    assert caught.value.code == "runtime_error"
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://192.168.1.10:11434",
        "http://127.0.0.1:11434/api",
        "file:///tmp/ollama.sock",
    ],
)
def test_non_loopback_or_non_origin_endpoint_is_rejected(url):
    with pytest.raises(ValueError, match="loopback"):
        OllamaAssistantRuntime(url, client=_FakeOllamaClient())


def test_preloaded_model_is_preferred_and_never_unloaded():
    client = _FakeOllamaClient(
        tags=[
            {"name": "small:latest", "size": 500, "details": {"family": "llama"}},
            {"name": "loaded:latest", "size": 2_000, "details": {"family": "qwen"}},
        ],
        loaded=["loaded:latest"],
        capabilities={
            "small:latest": ["completion"],
            "loaded:latest": ["completion"],
        },
    )

    result = _classify(_runtime(client))

    assert result.model == "loaded:latest"
    assert _generation_payload(client)["keep_alive"] == -1
    assert "num_ctx" not in _generation_payload(client)["options"]
    assert not any(
        path == "/api/generate"
        and payload.get("keep_alive") == 0
        and not payload.get("prompt")
        for _, path, payload, _ in client.calls
    )
    assert client.loaded == ["loaded:latest"]


def test_newly_loaded_model_uses_keep_alive_zero():
    client = _FakeOllamaClient(loaded=[])

    result = _classify(_runtime(client))

    assert result.model == "qwen:small"
    assert _generation_payload(client)["keep_alive"] == 0
    assert client.loaded == []


def test_embedding_model_is_excluded_and_text_model_is_selected():
    client = _FakeOllamaClient(
        tags=[
            {
                "name": "nomic-embed-text:latest",
                "size": 100,
                "details": {"family": "bert"},
            },
            {
                "name": "qwen:small",
                "size": 1_000,
                "details": {"family": "qwen"},
            },
        ],
        capabilities={
            "nomic-embed-text:latest": ["embedding"],
            "qwen:small": ["completion"],
        },
    )

    result = _classify(_runtime(client))

    assert result.model == "qwen:small"
    assert _generation_payload(client)["model"] == "qwen:small"

    with pytest.raises(AssistantRuntimeError) as caught:
        _classify(_runtime(client), model="nomic-embed-text:latest")
    assert caught.value.code == "model_not_text"


def test_explicit_model_name_requires_exact_match():
    client = _FakeOllamaClient()

    with pytest.raises(AssistantRuntimeError) as caught:
        _classify(_runtime(client), model="qwen")

    assert caught.value.code == "model_not_found"


def test_injected_prompt_builder_receives_validated_catalog_data():
    seen = {}

    def build(question, command_ids, catalog_context):
        seen.update(
            question=question,
            command_ids=command_ids,
            catalog_context=catalog_context,
        )
        return "bounded injected prompt"

    client = _FakeOllamaClient()
    _classify(_runtime(client, prompt_builder=build))

    assert seen["command_ids"] == ("doctor", "verify", "run")
    assert _generation_payload(client)["prompt"] == "bounded injected prompt"


def test_question_and_output_limits_are_enforced_before_http():
    client = _FakeOllamaClient()
    runtime = _runtime(client)

    with pytest.raises(AssistantRuntimeError) as caught:
        runtime.classify("x" * 501, ("verify",), "verify: check")
    assert caught.value.code == "invalid_question"
    assert client.calls == []

    with pytest.raises(ValueError, match="128"):
        _runtime(client, max_output_tokens=129)
