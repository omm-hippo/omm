"""A consented short response check; preserve existing runtime ownership."""
from __future__ import annotations

from datetime import datetime, timezone
import os

from omm import cli, hardware, memory_guard, registry, runtime_compatibility, runtime_profiles
from omm.engines.base import LoadOptions, ProbeRequest, find_runtime_model
from omm.engines.ollama import DEFAULT_OLLAMA_URL, OllamaAdapter

FAILURE_MESSAGES = {
    "server_unavailable": "실행 앱의 로컬 서버를 켠 뒤 다시 확인해 주세요.",
    "model_not_visible": "실행 앱에서 모델을 찾지 못했어요. 모델 연결을 먼저 확인해 주세요.",
    "load_failed": "모델을 메모리에 올리지 못했어요. 실행 앱의 모델 지원과 설정을 확인해 주세요.",
    "out_of_memory": "현재 메모리 여유가 부족해 실행 확인을 중단했어요.",
    "generation_timeout": "짧은 응답 확인 시간이 초과됐어요.",
    "empty_response": "실행 앱이 응답 내용을 반환하지 않았어요.",
    "unload_failed": "검증용 모델의 메모리 해제를 확인하지 못했어요. 실행 앱에서 상태를 확인해 주세요.",
    "unsupported_runtime": "이 실행 앱 버전이나 모델 형식은 실행 확인 API를 지원하지 않아요.",
    "unknown": "실행 앱의 로컬 API 설정을 확인해 주세요.",
}


def adapter_for(engine: str):
    if engine == "ollama":
        endpoint = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_URL)
        if "://" not in endpoint:
            endpoint = "http://" + endpoint
        return OllamaAdapter(endpoint)
    return cli._compatibility_adapter(engine)


def check(filename: str, entry: dict, engine: str) -> dict:
    adapter = adapter_for(engine)
    reference = cli._compatibility_model_ref(filename, entry, engine)
    health = adapter.health()
    result = None
    captured = []
    options = LoadOptions(verify_applied=engine == "ollama")
    if health.reachable:
        visible = find_runtime_model(adapter.list_models(), reference)
        if visible is None or not visible.loaded:
            path = cli._managed_model_path(filename)
            required = path.stat().st_size / 1024**3 * 1.2
            plan = memory_guard.plan_memory_guard(required, hardware.scan_hardware())
            if plan.decision is not memory_guard.GuardDecision.SAFE:
                result = runtime_compatibility.CompatibilityResult(
                    engine, "failed", datetime.now(timezone.utc).isoformat(),
                    runtime_compatibility.PROBE_VERSION, health.version, "out_of_memory",
                )
                registry.record_compatibility(filename, engine, result.registry_payload())
            else:
                saved = runtime_profiles.saved_options_for_file(filename, engine, path)
                if saved is not None:
                    runtime_profiles.ensure_memory(path, saved, hardware.scan_hardware())
                    options = saved
    if result is None:
        result = runtime_compatibility.verify_and_record(
            filename, adapter, reference, load_options=options,
            probe_request=ProbeRequest(max_output_tokens=8, timeout_seconds=30),
            on_response=lambda response: captured.append(response.text.strip()[:512]),
        )
    return {"filename": filename, "engine": engine, "runtime_verified": result.status == "passed",
            "compatibility": result.registry_payload(), "response": captured[0] if captured else None,
            "model_was_preloaded": result.model_was_preloaded,
            "released_test_load": result.status == "passed" and not result.model_was_preloaded,
            "message": FAILURE_MESSAGES.get(result.failure_reason, "")}
