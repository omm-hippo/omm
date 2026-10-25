from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from omm import config, install_safety, linker, registry
from omm.web import probe, task_flow, worker
from omm.web.jobs import JobManager, JobConflict
from omm.web.service import WebService, identifier


@pytest.fixture
def flow(monkeypatch, isolated_omm_home):
    path = config.MODELS_DIR / "tiny.gguf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test bytes, never executed")
    registry.upsert_entry(path.name, repo_id="test/tiny", linked={})
    calls = []
    monkeypatch.setattr(linker, "is_engine_installed", lambda engine: True)
    monkeypatch.setattr(install_safety, "verify_installed", lambda engine: calls.append("safety"))
    monkeypatch.setattr(probe, "adapter_for", lambda engine: SimpleNamespace(health=lambda: SimpleNamespace(reachable=True), list_models=lambda: []))
    def execute(request, stop):
        calls.append(request["operation"])
        if request["operation"] == "verify":
            return {"runtime_verified": True, "filename": path.name, "engine": request["engine"], "response": "fixture response"}
        return {"linked": {request["engine"]: True}}
    monkeypatch.setattr(worker, "execute", execute)
    return path, calls


def request(**changes):
    return {"operation": "start_chat", "filename": "tiny.gguf", "engine": "ollama", **changes}


@pytest.mark.parametrize("engine", ["ollama", "lmstudio"])
def test_existing_file_runs_safety_link_and_real_response_contract(flow, engine):
    _, calls = flow
    updates = []
    result = task_flow.prepare(request(engine=engine), threading.Event(), lambda *x: updates.append(x))
    assert calls == ["safety", "link", "verify"]
    assert updates == [(stage, status) for stage in task_flow.STAGES for status in ("running", "completed")]
    assert result["chat_ready"] and result["runtime_verified"]
    assert result["model_id"] == identifier("tiny.gguf")
    assert result["uploaded"] is False


def test_candidate_reuses_the_existing_install_implementation(flow):
    _, calls = flow
    task_flow.prepare(request(ref="test/tiny:tiny.gguf"), threading.Event(), lambda *args: None)
    assert calls == ["safety", "install", "link", "verify"]


@pytest.mark.parametrize("success", [True, False])
def test_targeted_link_keeps_other_runners_and_checks_selected_result(isolated_omm_home, monkeypatch, success):
    from omm import cli
    path = config.MODELS_DIR / "tiny.gguf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF fixture")
    registry.upsert_entry(path.name, linked={"lmstudio": True})
    monkeypatch.setattr(cli, "_link_model", lambda *args, **kwargs: {"ollama": success, "lmstudio": False})
    if success:
        result = worker.execute({"operation": "link", "filename": path.name, "engine": "ollama"}, threading.Event())
        assert result["linked"] == {"ollama": True, "lmstudio": True}
    else:
        with pytest.raises(ValueError, match="No runner link"):
            worker.execute({"operation": "link", "filename": path.name, "engine": "ollama"}, threading.Event())
    assert registry.load_registry()[path.name]["linked"]["lmstudio"] is True


def test_safety_failure_never_downloads_or_marks_success(flow, monkeypatch):
    _, calls = flow
    def failed(engine):
        raise install_safety.SafetyError("mandatory scan failed")
    monkeypatch.setattr(install_safety, "verify_installed", failed)
    with pytest.raises(install_safety.SafetyError):
        task_flow.prepare(request(ref="test/tiny:tiny.gguf"), threading.Event(), lambda *args: None)
    assert calls == []
    assert install_safety.last_result("ollama")["status"] == "failed"


def test_runtime_unavailable_stops_before_model_download(flow, monkeypatch):
    _, calls = flow
    monkeypatch.setattr(probe, "adapter_for", lambda engine: SimpleNamespace(health=lambda: SimpleNamespace(reachable=False)))
    with pytest.raises(ValueError, match="로컬 서버"):
        task_flow.prepare(request(ref="test/tiny:tiny.gguf"), threading.Event(), lambda *args: None)
    assert calls == ["safety"]


def test_failed_model_response_cannot_be_completed_or_open_chat(flow, monkeypatch):
    updates = []
    monkeypatch.setattr(worker, "execute", lambda request, stop: {"linked": {"ollama": True}, "runtime_verified": False, "message": "no response"})
    with pytest.raises(ValueError, match="no response"):
        task_flow.prepare(request(), threading.Event(), lambda *x: updates.append(x))
    assert ("verify", "completed") not in updates


def test_existing_unrelated_runtime_load_stops_before_download(flow, monkeypatch):
    from omm.engines.base import RuntimeModel
    _, calls = flow
    monkeypatch.setattr(probe, "adapter_for", lambda engine: SimpleNamespace(
        health=lambda: SimpleNamespace(reachable=True),
        list_models=lambda: [RuntimeModel("other", "other", True, "other")]))
    with pytest.raises(ValueError, match="다른 모델"):
        task_flow.prepare(request(ref="test/tiny:tiny.gguf"), threading.Event(), lambda *args: None)
    assert calls == ["safety"]


def test_warm_document_embedding_model_does_not_block_chat_preparation(flow, monkeypatch):
    from omm.engines.base import RuntimeModel
    _, calls = flow
    monkeypatch.setattr(probe, "adapter_for", lambda engine: SimpleNamespace(
        health=lambda: SimpleNamespace(reachable=True),
        list_models=lambda: [RuntimeModel("bge-m3:latest", "bge-m3:latest", True, "bge-m3:latest")]))
    task_flow.prepare(request(ref="test/tiny:tiny.gguf"), threading.Event(), lambda *args: None)
    assert "verify" in calls
    # The search store was only read, never created by chat preparation.
    assert not (config.OMM_HOME / "documents").exists()


def test_cancellation_after_link_never_runs_generation(flow):
    _, calls = flow
    stop = threading.Event()
    def update(stage, status):
        if (stage, status) == ("link", "completed"):
            stop.set()
    with pytest.raises(KeyboardInterrupt):
        task_flow.prepare(request(), stop, update)
    assert calls == ["safety", "link"]


def test_failed_native_install_does_not_continue(flow, monkeypatch):
    _, calls = flow
    monkeypatch.setattr(linker, "is_engine_installed", lambda engine: False)
    monkeypatch.setattr(linker, "install_engine", lambda engine: SimpleNamespace(status="failed", message="installer blocked"))
    with pytest.raises(ValueError, match="installer blocked"):
        task_flow.prepare(request(), threading.Event(), lambda *args: None)
    assert calls == []


@pytest.mark.parametrize("engine", [None, "unknown", "jan", "https://example.com"])
def test_only_reviewed_chat_runtimes_are_admitted(flow, engine):
    with pytest.raises(ValueError):
        task_flow.prepare(request(engine=engine), threading.Event(), lambda *args: None)


def test_http_request_uses_known_selection_not_caller_supplied_ref(flow):
    service = WebService()
    key = identifier("test/tiny:tiny.gguf")
    service.candidates[key] = {"filename": "tiny.gguf", "ref": "test/tiny:tiny.gguf"}
    body = {"operation": "start_chat", "id": key, "engine": "ollama", "confirmed": True}
    assert service.request(body)["ref"] == "test/tiny:tiny.gguf"
    with pytest.raises(ValueError):
        service.request({**body, "ref": "attacker/model:large.gguf"})
    with pytest.raises(ValueError):
        service.request({**body, "confirmed": False})
    with pytest.raises(ValueError):
        service.request({**body, "engine": "jan"})
    installed = service.request({**body, "id": identifier("tiny.gguf")})
    assert installed["filename"] == "tiny.gguf" and "ref" not in installed


def saved_task(manager):
    req = request(ref="test/tiny:tiny.gguf")
    jid = str(uuid.uuid4())
    job = {"id": jid, "operation": "start_chat", "filename": "tiny.gguf", "status": "failed",
           "created_at": 1, "task_request": req, "request_digest": hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()}
    manager.jobs[jid] = job
    manager._save(job)
    return jid


def test_retry_survives_manager_restart_without_transient_candidate_cache(isolated_omm_home, monkeypatch):
    first = JobManager()
    jid = saved_task(first)
    first.close()
    restarted = JobManager()
    try:
        monkeypatch.setattr(restarted, "start", lambda req, rid: {"request": req, "id": rid})
        new_id = str(uuid.uuid4())
        assert restarted.retry_task(jid, new_id) == {"request": request(ref="test/tiny:tiny.gguf"), "id": new_id}
        restarted.jobs[jid]["task_request"]["filename"] = "changed.gguf"
        with pytest.raises(ValueError, match="달라졌"):
            restarted.retry_task(jid, str(uuid.uuid4()))
        restarted.jobs[jid]["status"] = "completed"
        with pytest.raises(JobConflict):
            restarted.retry_task(jid, str(uuid.uuid4()))
    finally:
        restarted.close()


def test_step_updates_are_persisted_from_actual_worker_pipe(isolated_omm_home, monkeypatch):
    from omm.web import jobs
    code = "import sys,json;json.load(sys.stdin);print(json.dumps({'event':'progress','bytes_received':123,'total_bytes':456}),flush=True);print(json.dumps({'event':'progress','stage':'safety','step_status':'completed'}),flush=True);print(json.dumps({'event':'progress','stage':'untrusted','step_status':'completed'}),flush=True);print(json.dumps({'event':'completed','result':{'chat_ready':True}}),flush=True)"
    monkeypatch.setattr(jobs, "worker_argv", lambda: [sys.executable, "-c", code])
    manager = JobManager()
    jid = str(uuid.uuid4())
    try:
        manager.start(request(), jid)
        for _ in range(100):
            row = manager.list()[0]
            if row["status"] == "completed":
                break
            time.sleep(.02)
        assert row["status"] == "completed"
        assert row["steps"] == {"safety": "completed"}
        assert row["bytes_received"] == 123 and row["total_bytes"] == 456
        manager.close()
        data = json.loads((config.OMM_HOME / "web-jobs" / (jid + ".json")).read_text(encoding="utf-8"))
        assert data["steps"] == row["steps"]
        assert data["task_request"] == request()
    finally:
        manager.close()
