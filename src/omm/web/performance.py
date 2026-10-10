"""Owned local trials and bounded arithmetic/speed comparisons; no uploads."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import math
import os
import statistics
import threading
import uuid

from omm import cli, config, hardware, install_state, quality, registry, runtime_profiles, tuning
from omm.engines.base import LoadOptions, ProbeRequest
from omm.hashutil import sha256_file
from omm.web.probe import adapter_for


def profile_action(filename: str, engine: str, restore: bool = False) -> dict:
    path = cli._managed_model_path(filename)
    entry = registry.load_registry()[filename]
    digest = sha256_file(path)
    if restore:
        return {"filename": filename, "profile": runtime_profiles.restore(filename, engine, digest), "restored": True}
    before = runtime_profiles.describe(filename, engine, digest)
    metadata = runtime_profiles._metadata(path)
    candidate = {**entry, "filename": filename, "size_bytes": path.stat().st_size,
                 "context_length": metadata.get(f"{metadata.get('general.architecture')}.context_length")}
    profile = tuning.recommend_runtime_settings(hardware.scan_hardware(), candidate)
    options = runtime_profiles.proposed_options(profile, path, engine)
    adapter = adapter_for(engine)
    reference = cli._compatibility_model_ref(filename, entry, engine)
    previous = runtime_profiles.saved_options(filename, engine, digest)
    baseline_options = previous or LoadOptions(context_length=min(1024, options.context_length),
        cpu_threads=options.cpu_threads, batch_size=min(128, options.context_length),
        gpu_layers=options.gpu_layers, verify_applied=True)
    with install_state.cleanup_guard(filename):
        baseline = runtime_profiles.trial(path, adapter, reference, baseline_options, hardware.scan_hardware)
        proposed = runtime_profiles.trial(path, adapter, reference, options, hardware.scan_hardware)
        runtime_profiles.save_trial(filename, engine, path, digest, proposed, expected_revision=before["revision"])
    return {"filename": filename, "baseline": baseline, "proposed": proposed, "saved": True,
            "quality_evaluated": False, "uploaded": False}


def compare(filenames: list[str], engine: str, stop: threading.Event, on_progress) -> dict:
    pack, pack_hash = quality.load_pack()
    adapter = adapter_for(engine)
    if not adapter.health().reachable:
        raise ValueError("실행 앱의 로컬 서버를 먼저 켜 주세요.")
    rows = []
    for index, filename in enumerate(filenames):
        if stop.is_set():
            raise InterruptedError("성능 비교를 중단했어요.")
        if any(item.loaded for item in adapter.list_models()):
            raise ValueError("실행 앱에서 다른 모델이 사용 중이에요. 기존 모델을 유지하고 비교를 중단했어요.")
        path = cli._managed_model_path(filename)
        entry = registry.load_registry().get(filename)
        if not entry or not path.is_file() or not entry.get("linked", {}).get(engine):
            raise ValueError("모델 파일이나 실행 앱 연결 상태가 바뀌었어요.")
        digest = sha256_file(path)
        options = LoadOptions(context_length=4096, cpu_threads=min(16, os.cpu_count() or 4) if engine == "ollama" else None,
                              batch_size=128, verify_applied=True)
        metadata = runtime_profiles._metadata(path)
        maximum = metadata.get(f"{metadata.get('general.architecture')}.context_length")
        if type(maximum) is int and maximum < options.context_length:
            raise ValueError("이 비교는 4096 토큰 문맥을 지원하는 모델에서 실행할 수 있어요.")
        receipt = None
        with install_state.cleanup_guard(filename):
            runtime_profiles.ensure_memory(path, options, hardware.scan_hardware())
            try:
                receipt = adapter.load(cli._compatibility_model_ref(filename, entry, engine), options)
                if not receipt.loaded_by_omm or receipt.was_already_loaded:
                    raise ValueError("모델이 다른 작업에서 로딩됐어요. 기존 작업을 유지했어요.")
                if not receipt.applied_options:
                    raise ValueError("실행 앱이 적용된 설정을 확인해 주지 않았어요.")
                correct = 0
                items = []
                for item in pack["items"]:
                    if stop.is_set():
                        raise InterruptedError("성능 비교를 중단했어요.")
                    probe = adapter.generate(receipt, ProbeRequest("Return only the final numeric answer.\n" + item["question"], 64, 60))
                    answer = quality.parse_numeric_answer(probe.text)
                    passed = answer is not None and answer == quality._normalize_number(item["expected"])
                    correct += passed
                    items.append({"id": item["id"], "correct": passed})
                samples = []
                for _ in range(3):
                    if stop.is_set():
                        raise InterruptedError("성능 비교를 중단했어요.")
                    probe = adapter.generate(receipt, ProbeRequest("Explain in plain language how a rainbow forms.", 64, 60))
                    value = probe.tokens_per_second
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                        raise ValueError("실행 앱이 실제 속도 측정값을 반환하지 않았어요.")
                    samples.append(value)
                rows.append({"filename": filename, "sha256": digest, "engine": engine,
                    "accuracy": correct / len(items), "correct": correct, "total": len(items), "items": items,
                    "tokens_per_second": statistics.median(samples), "samples": samples,
                    "observed_options": receipt.applied_options})
            finally:
                if receipt is not None and receipt.loaded_by_omm:
                    if not adapter.unload(receipt).unloaded:
                        raise ValueError("비교용 모델의 메모리 해제를 확인하지 못했어요. 실행 앱을 확인해 주세요.")
        if sha256_file(path) != digest:
            raise ValueError("측정 도중 모델 파일이 바뀌었어요. 비교 결과를 저장하지 않았어요.")
        on_progress(index + 1, len(filenames))
    report = {"schema_version": 1, "protocol": "omm-web-arithmetic-speed-v1", "pack_id": pack["pack_id"],
              "pack_version": pack["pack_version"], "pack_sha256": pack_hash, "engine": engine,
              "checked_at": datetime.now(timezone.utc).isoformat(), "models": rows,
              "hardware": asdict(hardware.scan_hardware()), "uploaded": False, "raw_responses_stored": False,
              "generation": {"context_length": 4096, "max_output_tokens": 64, "temperature": 0,
                             "prompt": "Return only the final numeric answer.", "speed_runs": 3},
              "limitation": "8문항 산술 점검과 3회 속도 측정입니다. 일반적인 답변 품질 순위가 아닙니다."}
    root = config.EVALUATIONS_DIR
    if root.is_symlink():
        raise ValueError("평가 저장 폴더의 연결 경로를 확인해 주세요.")
    path = root / f"web-{uuid.uuid4()}.json"
    quality.write_evidence(report, path)
    return {**report, "report_filename": path.name}
