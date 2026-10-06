"""Real local HTTP requests exercise authentication and operation boundaries."""
from __future__ import annotations

import json
import threading
import uuid

import pytest
import requests

from omm.web.server import WebServer
from omm.web.service import WebService, identifier
from omm.web.jobs import JobManager, JobConflict
from omm import config, hub, linker, predictor, registry
from omm.hardware import HardwareInfo


@pytest.fixture
def server(tmp_path, isolated_omm_home):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>OMM</title>",encoding="utf-8")
    instance = WebServer(0, static_root=assets)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(5)


def session(server):
    client = requests.Session()
    response = client.get(server.url, timeout=5)
    assert response.status_code == 200
    return client


def test_management_routes_keep_session_and_confirmation_boundaries(server, monkeypatch):
    from omm.web import management
    for route in ("connections", "diagnostics", "settings", "files", "imports", "wiki/packages?id=qwen3-8b"):
        assert requests.get(server.url + "api/" + route, timeout=5).status_code == 401
    client = session(server)
    headers = {"X-OMM-Web": "1"}
    assert client.post(server.url + "api/settings", json={"changes": {"usage_stats_policy": "enabled"}}, headers=headers, timeout=5).status_code == 400
    result = client.post(server.url + "api/settings", json={"changes": {"usage_stats_policy": "enabled"}, "confirmed": True}, headers=headers, timeout=5)
    assert result.status_code == 200
    assert client.get(server.url + "api/settings", timeout=5).json()["usage_stats_policy"] == "enabled"
    monkeypatch.setattr(management, "check_network", lambda: {"requests": 2, "targets": []})
    assert client.post(server.url + "api/connections/check", json={"url": "https://evil.example"}, headers=headers, timeout=5).status_code == 400
    assert client.post(server.url + "api/connections/check", json={}, headers=headers, timeout=5).json()["requests"] == 2
    assert client.post(server.url + "api/wiki/discover", json={"id": "missing", "profile": "balanced"}, headers=headers, timeout=5).status_code == 400


def test_api_requires_a_local_browser_session(server):
    assert requests.get(server.url + "api/jobs", timeout=5).status_code == 401
    client = session(server)
    assert client.get(server.url + "api/jobs", timeout=5).json() == []


def test_bootstrap_cookie_and_security_headers(server):
    response = requests.get(server.url, timeout=5)
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_foreign_origin_cannot_bootstrap_or_read(server, headers):
    assert requests.get(server.url, headers=headers, timeout=5).status_code == 403
    client = session(server)
    assert client.get(server.url + "api/jobs", headers=headers, timeout=5).status_code == 403


def test_operations_require_json_confirmation_and_non_simple_header(server):
    client = session(server)
    assert client.post(server.url + "api/jobs", json={}, timeout=5).status_code == 403
    assert client.post(server.url + "api/jobs", data="{}", headers={"X-OMM-Web": "1"}, timeout=5).status_code == 415
    assert client.post(server.url + "api/jobs", json={"operation": "shell", "confirmed": True}, headers={"X-OMM-Web": "1"}, timeout=5).status_code == 400
    assert client.options(server.url + "api/jobs", timeout=5).status_code == 403


def test_body_limit_is_checked_before_parsing(server):
    client = session(server)
    response = client.post(server.url + "api/jobs", data=b"x" * 65537,
                           headers={"Content-Type": "application/json", "X-OMM-Web": "1"}, timeout=5)
    assert response.status_code == 413


def test_no_filesystem_path_or_arbitrary_download_url_is_accepted(isolated_omm_home):
    service = WebService()
    with pytest.raises(ValueError):
        service.request({"operation": "install", "id": "x"*64, "url": "https://evil.example/model.gguf", "confirmed": True})
    with pytest.raises(ValueError):
        service.request({"operation": "uninstall", "id": "../data", "confirmed": True})
    with pytest.raises(ValueError):
        service.request({"operation": "uninstall", "id": "x"*64, "confirmed": True})


def test_model_view_reflects_persisted_files_not_only_registry(isolated_omm_home):
    filename = "model-1B-Q4_K_M.gguf"
    registry.upsert_entry(filename, linked={"ollama": True})
    [missing] = WebService().models()
    assert missing["exists"] is False
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    (config.MODELS_DIR / filename).write_bytes(b"GGUF")
    [present] = WebService().models()
    assert present["exists"] is True and present["size_bytes"] == 4
    assert present["id"] == identifier(filename)


def test_destructive_operations_require_explicit_confirmation(isolated_omm_home):
    filename = "model-1B-Q4_K_M.gguf"
    registry.upsert_entry(filename)
    with pytest.raises(ValueError, match="confirmed"):
        WebService().request({"operation": "uninstall", "id": identifier(filename)})
    request = WebService().request({"operation": "uninstall", "id": identifier(filename), "confirmed": True})
    assert request["filename"] == filename


def test_recommendation_uses_offline_rules_without_claiming_measurement(monkeypatch, isolated_omm_home):
    from omm.web import service as module
    monkeypatch.setattr(module.hardware, "scan_hardware", lambda: HardwareInfo("macOS", "", "Apple M5", 24, 20, True, "Apple M5", 24, 20))
    monkeypatch.setattr(predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(module.recommend_status, "detect_installation_statuses", lambda xs: [module.recommend_status.NOT_INSTALLED] * len(xs))
    value = WebService().recommendations("balanced")
    assert value["catalog_source"] == "bundled_rules" and value["models"]
    assert all(row["predicted_tokens_per_second"] is None for row in value["models"])
    assert all(row["evidence"]["calibrated_interval"] is False for row in value["models"])
    assert all(row["memory_required_gb"] <= value["budget_gb"] for row in value["models"])


def test_only_seen_exact_package_can_be_installed(isolated_omm_home):
    service = WebService()
    cid = identifier("org/repo:model-1B-Q4_K_M.gguf")
    service.candidates[cid] = {"ref": "org/repo:model-1B-Q4_K_M.gguf", "filename": "model-1B-Q4_K_M.gguf"}
    request = service.request({"operation": "install", "id": cid, "confirmed": True, "engine": "ollama"})
    assert request["ref"] == "org/repo:model-1B-Q4_K_M.gguf"
    with pytest.raises(ValueError):
        service.request({"operation": "install", "id": cid, "confirmed": True, "engine": "arbitrary"})


def test_recommendations_distinguish_runner_owned_installation(monkeypatch, isolated_omm_home):
    from omm.web import service as module
    monkeypatch.setattr(module.hardware, "scan_hardware", lambda: HardwareInfo("macOS", "", "Apple M5", 24, 20, True, "Apple M5", 24, 20))
    monkeypatch.setattr(predictor, "load_cached_model", lambda: None)
    status = module.recommend_status.InstallationStatus(installed=True, engines=("ollama",), match_kind="model_identity")
    monkeypatch.setattr(module.recommend_status, "detect_installation_statuses", lambda xs: [status] * len(xs))
    rows = WebService().recommendations()["models"]
    assert rows
    assert all(row["installed"] and not row["managed_by_omm"] for row in rows)
    assert all(row["installation_match"] == "model_identity" and row["installed_engines"] == ["ollama"] for row in rows)


def test_jobs_are_idempotent_and_serialized(monkeypatch, isolated_omm_home):
    manager = JobManager()
    monkeypatch.setattr(manager, "_run", lambda *args: None)
    request = {"operation": "install", "filename": "model.gguf", "ref": "org/repo:model.gguf", "engine": None}
    key = str(uuid.uuid4())
    first = manager.start(request, key)
    assert manager.start(request, key)["id"] == first["id"]
    with pytest.raises(JobConflict):
        manager.start({**request, "ref": "org/other:model.gguf"}, key)
    with pytest.raises(JobConflict):
        manager.start(request, str(uuid.uuid4()))
    assert manager.cancel(key)["status"] == "cancelled"
    manager.close()


def test_server_restart_marks_unconfirmed_job_interrupted(monkeypatch, isolated_omm_home):
    manager = JobManager()
    monkeypatch.setattr(manager, "_run", lambda *args: None)
    key = str(uuid.uuid4())
    manager.start({"operation": "install", "filename": "model.gguf"}, key)
    manager.close()
    restored = JobManager()
    [job] = restored.list()
    assert job["status"] == "interrupted" and job["id"] == key
    restored.close()


def test_second_server_cannot_claim_an_active_home(isolated_omm_home):
    manager = JobManager()
    try:
        with pytest.raises(JobConflict, match="already running"):
            JobManager()
    finally:
        manager.close()
    restored = JobManager()
    restored.close()


def test_shutdown_does_not_launch_a_queued_worker(monkeypatch, isolated_omm_home):
    from omm.web import jobs as module
    manager = JobManager()
    monkeypatch.setattr(manager, "_run", lambda *args: None)
    key = str(uuid.uuid4())
    request = {"operation": "install", "filename": "model.gguf"}
    manager.start(request, key)
    manager.close()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("worker started after shutdown"))
    JobManager._run(manager, key, request)
    assert manager.list()[0]["status"] == "interrupted"


def test_catalog_refresh_is_same_origin_and_rejects_override_urls(server, monkeypatch):
    client = session(server)
    monkeypatch.setattr(server.service, "refresh_catalog", lambda: {"updated": True})
    assert client.post(server.url + "api/catalog/refresh", json={}, timeout=5).status_code == 403
    headers = {"X-OMM-Web": "1"}
    assert client.post(server.url + "api/catalog/refresh", json={"url": "https://evil.example"}, headers=headers, timeout=5).status_code == 400
    assert client.post(server.url + "api/catalog/refresh", json={}, headers=headers, timeout=5).json() == {"updated": True}


def test_local_correction_does_not_claim_environment_confidence(monkeypatch, isolated_omm_home):
    from omm.web import service as module
    hw = HardwareInfo("macOS", "", "Apple M5", 24, 20, True, "Apple M5", 24, 20)
    monkeypatch.setattr(module.recommend_evidence.calibration, "load_profiles", lambda: {"profiles": {
        module.recommend_evidence.calibration.hardware_bucket(hw): {"sample_count": 3, "factor": 1.2}
    }})
    evidence = module.recommendation_evidence({"trees": []}, hw)
    assert evidence["status"] == "local_calibrated"
    assert evidence["local_calibration_samples"] == 3
    assert evidence["matching_environment_samples"] is None
    assert evidence["calibrated_interval"] is False
    assert module.recommendation_evidence(None, hw)["status"] == "static_rules"


def test_catalog_refresh_preserves_configured_signature_verification(monkeypatch, isolated_omm_home):
    settings = config.load_config()
    received = []
    monkeypatch.setattr(predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(predictor, "fetch_and_cache_model", lambda *args: received.append(args) or {"candidates": []})
    assert WebService().refresh_catalog() == {"updated": True}
    assert received == [(settings["model_url"], settings["catalog_manifest_url"], settings["catalog_public_key"])]
    assert received[0][1] and received[0][2]


def test_cli_web_does_not_launch_onboarding_or_browser_by_default(monkeypatch, isolated_omm_home):
    from omm import cli
    from omm.web import server as module
    from typer.testing import CliRunner
    seen = []
    monkeypatch.setattr(module, "serve", lambda port, **kwargs: seen.append((port, kwargs)))
    monkeypatch.setattr(cli.onboarding, "run_wizard", lambda *args: pytest.fail("unexpected onboarding"))
    monkeypatch.setattr(cli, "_spawn_bg_version_check", lambda *args: pytest.fail("unexpected version request"))
    monkeypatch.setattr(cli.telemetry, "flush_pending", lambda *args: pytest.fail("unexpected queued upload"))
    result = CliRunner().invoke(cli.app, ["web", "--port", "8766"])
    assert result.exit_code == 0, result.output
    assert seen == [(8766, {"open_browser": False})]


def test_verification_requires_a_linked_supported_engine(isolated_omm_home):
    filename='model.gguf'
    config.MODELS_DIR.mkdir(parents=True,exist_ok=True)
    (config.MODELS_DIR/filename).write_bytes(b'GGUF')
    registry.save_registry({filename:{'linked':{'ollama':True}}})
    service=WebService()
    base={'operation':'verify','id':identifier(filename),'confirmed':True}
    assert service.request({**base,'engine':'ollama'})['operation']=='verify'
    for engine in (None,'lmstudio','jan'):
        with pytest.raises(ValueError):service.request({**base,'engine':engine})


def test_packaged_worker_uses_the_hidden_binary_entry(monkeypatch):
    from omm.web import jobs
    monkeypatch.setattr(jobs.sys,'frozen',True,raising=False)
    assert jobs.worker_argv()==[jobs.sys.executable,'_web-worker']


def test_second_click_reuses_the_confirmed_manager_address(server,monkeypatch):
    from omm.web import server as module
    import webbrowser
    opened=[]
    monkeypatch.setattr(webbrowser,'open',lambda url: opened.append(url))
    assert module.running_url()==server.url
    module.serve(open_browser=True)
    assert opened==[server.url]


def test_stale_or_foreign_manager_address_is_not_opened(isolated_omm_home,monkeypatch):
    from omm.web import server as module
    path=config.OMM_HOME/'web-jobs'/'server-info.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'url':'https://example.com/','instance':'stale'}),encoding='utf-8')
    monkeypatch.setattr(requests,'get',lambda *args,**kwargs:pytest.fail('foreign manager address was fetched'))
    assert module.running_url() is None
