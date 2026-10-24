"""Compose existing managed operations after one explicit task confirmation."""
from __future__ import annotations
from omm.web.recovery import ActionError

STAGES = {"engine": "실행 앱 확인", "safety": "앱 안전 검사", "runtime": "로컬 서버 확인",
          "install": "모델 파일 준비", "link": "실행 앱 연결", "verify": "실제 응답 확인"}


def prepare(request, stop, progress):
    from omm import cli, config, hub, install_safety, linker, registry
    from omm.engines.base import find_runtime_model
    from omm.web.probe import adapter_for
    from omm.web.service import identifier
    from omm.web.worker import execute

    engine = request["engine"]
    filename = hub.validate_model_filename(request["filename"])
    if engine not in {"ollama", "lmstudio"}:
        raise ValueError("대화 시작은 Ollama 또는 LM Studio를 선택해 주세요.")

    def cancelled():
        if stop.is_set():
            raise KeyboardInterrupt("작업을 중단했어요.")

    def step(name, action):
        cancelled()
        progress(name, "running")
        result = action()
        cancelled()
        progress(name, "completed")
        return result

    def ensure_engine():
        if not linker.is_engine_installed(engine):
            result = linker.install_engine(engine)
            if result.status != "installed":
                raise ValueError(result.message)
        if not linker.is_engine_installed(engine):
            raise ValueError("실행 앱 설치 결과를 확인하지 못했어요.")

    step("engine", ensure_engine)
    def check_safety():
        try:
            return install_safety.verify_installed(engine)
        except install_safety.SafetyError:
            install_safety.record_failure(engine)
            raise
    step("safety", check_safety)

    def reachable():
        adapter = adapter_for(engine)
        if not adapter.health().reachable:
            raise ActionError("server_unavailable", "실행 앱의 로컬 서버를 켠 뒤 이 작업을 다시 시작해 주세요. 모델 다운로드는 시작하지 않았어요.")
        models = adapter.list_models()
        if not any(model.loaded for model in models):
            return
        entry = registry.load_registry().get(filename)
        selected = (find_runtime_model(models, cli._compatibility_model_ref(filename, entry, engine))
                    if entry and (config.MODELS_DIR / filename).is_file() else None)
        if (selected is None or not selected.loaded) and any(model.loaded for model in models):
            raise ActionError("model_busy", "다른 모델이 이미 로딩되어 있어요. 그 모델을 직접 종료한 뒤 다시 시작해 주세요. 기존 로딩은 바꾸지 않았어요.")
    step("runtime", reachable)

    def model_file():
        if request.get("ref"):
            # The existing installer resolves the exact package and checks its
            # provenance, memory, disk, hash, partial download and registry.
            before = registry.load_registry().get(filename, {})
            try:
                return execute({"operation": "install", "filename": filename,
                                "ref": request["ref"], "engine": engine}, stop)
            finally:
                after = registry.load_registry().get(filename, {})
                if before.get("sha256") and before.get("sha256") == after.get("sha256"):
                    previous = before.get("linked", {})
                    linked = dict(after.get("linked", {}))
                    linked.update({key: value for key, value in previous.items() if key != engine})
                    if linked != after.get("linked"):
                        registry.upsert_entry(filename, linked=linked)
        path = config.MODELS_DIR / filename
        if not registry.load_registry().get(filename) or not path.is_file():
            raise ValueError("관리 중인 모델 파일을 찾지 못했어요. 추천 목록에서 다시 선택해 주세요.")
    step("install", model_file)
    linked = step("link", lambda: execute({"operation": "link", "filename": filename, "engine": engine}, stop))
    def check_response():
        value = execute({"operation": "verify", "filename": filename, "engine": engine}, stop)
        if value.get("runtime_verified") is not True:
            reason = value.get("compatibility", {}).get("failure_reason", "unknown")
            raise ActionError(reason, value.get("message") or "실제 응답을 확인하지 못했어요. 실패 단계를 확인한 뒤 다시 시도해 주세요.")
        return value
    verified = step("verify", check_response)
    return {**verified, "model_id": identifier(filename), "linked": linked["linked"],
            "chat_ready": True, "uploaded": False}
