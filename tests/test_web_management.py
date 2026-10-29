"""Management contracts, real local persistence and owned-runtime failure paths."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from omm import config, hardware, predictor, registry, runtime_profiles, usage
from omm.engines.base import LoadReceipt, ProbeResult, RuntimeHealth, RuntimeModel
from omm.web import maintenance, management, performance
from omm.web.service import WebService, identifier


@pytest.fixture
def managed(isolated_omm_home):
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    filename = "model-1B-Q4_K_M.gguf"
    (config.MODELS_DIR / filename).write_bytes(b"GGUF-test")
    registry.upsert_entry(filename, linked={"ollama": True}, sha256="a"*64)
    return filename


def test_settings_preserve_other_fields_and_cli_policy(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"default_engine": "jan", "custom": 7}), encoding="utf-8")
    result = management.save_settings({"usage_stats_policy": "enabled", "telemetry_send_policy": "never"})
    persisted = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert persisted["custom"] == 7 and persisted["default_engine"] == "jan"
    assert usage.policy(persisted) == "enabled"
    assert result["telemetry_send_policy"] == "never"


@pytest.mark.parametrize("changes", [{}, {"model_url":"https://evil.example"}, {"usage_stats_policy":"always"}, {"memory_guard_policy":True}])
def test_settings_reject_unknown_or_invalid_values(isolated_omm_home, changes):
    with pytest.raises(ValueError):
        management.save_settings(changes)
    assert not config.CONFIG_PATH.exists()


def test_settings_inspection_does_not_initialize_or_repair(isolated_omm_home):
    assert management.settings()["usage_stats_policy"] == "never"
    assert not config.CONFIG_PATH.exists()
    config.CONFIG_PATH.write_text("broken",encoding="utf-8")
    with pytest.raises(ValueError):
        management.save_settings({"usage_stats_policy":"enabled"})
    assert config.CONFIG_PATH.read_text(encoding="utf-8") == "broken"


def test_cleanup_requires_preview_and_preserves_changed_download(managed):
    path = config.MODELS_DIR / "paused.gguf.part"
    path.write_bytes(b"partial")
    service = WebService()
    [row] = service.files()["partials"]
    request = service.request({"operation":"cleanup", "ids":[row["id"]], "confirmed":True})
    path.write_bytes(b"new data")
    with pytest.raises(ValueError, match="변경"):
        management.cleanup_selected(request["files"])
    assert path.read_bytes() == b"new data"
    [row] = service.files()["partials"]
    request = service.request({"operation":"cleanup", "ids":[row["id"]], "confirmed":True})
    assert management.cleanup_selected(request["files"])["removed"] == [path.name]
    assert (config.MODELS_DIR / managed).exists()


def test_cleanup_does_not_follow_symlink(managed,tmp_path,requires_symlink_support):
    target = tmp_path / "outside"
    target.write_bytes(b"preserve")
    (config.MODELS_DIR / "unsafe.gguf.part").symlink_to(target)
    assert WebService().files()["partials"] == []
    assert target.read_bytes() == b"preserve"


@pytest.mark.parametrize("body", [
    {"operation":"compare","ids":[],"engine":"ollama"},
    {"operation":"compare","ids":["a"]*5,"engine":"ollama"},
    {"operation":"compare","ids":["a"],"engine":"jan"},
    {"operation":"cleanup","ids":["a"]},
    {"operation":"import_scan","path":"/private"},
    {"operation":"engine_install","engine":"shell"},
    {"operation":"profile_save","id":"a"*64,"engine":"ollama"},
    {"operation":"import","id":"a"*64,"url":"https://evil.example"},
])
def test_operation_selection_is_bounded_and_server_owned(isolated_omm_home,body):
    with pytest.raises(ValueError):
        WebService().request({**body,"confirmed":True})


def test_comparison_resolves_server_owned_models(managed):
    body={"operation":"compare","ids":[identifier(managed)],"engine":"ollama","confirmed":True}
    assert WebService().request(body)["filenames"] == [managed]
    with pytest.raises(ValueError,match="confirmed"):
        WebService().request({**body,"confirmed":False})


@pytest.fixture
def comparison_runtime(monkeypatch,managed):
    class Adapter:
        key="ollama"
        unloaded=0
        load_count=0
        busy=False
        released=True
        def health(self):return RuntimeHealth(True,"test")
        def list_models(self):return [RuntimeModel("test","test",self.busy)]
        def load(self,model,options):
            self.load_count+=1
            return LoadReceipt(RuntimeModel("test","test",True),"test",False,True,options,{"context_length":options.context_length})
        def generate(self,receipt,request):return ProbeResult("18",20.0,2)
        def unload(self,receipt):
            self.unloaded+=1
            return SimpleNamespace(unloaded=self.released)
    adapter=Adapter()
    monkeypatch.setattr(performance,"adapter_for",lambda engine:adapter)
    monkeypatch.setattr(runtime_profiles,"ensure_memory",lambda *args:None)
    monkeypatch.setattr(runtime_profiles,"_metadata",lambda p:{"general.architecture":"qwen3","qwen3.context_length":32768})
    return adapter,managed


def test_comparison_persists_actual_protocol_and_releases_load(comparison_runtime):
    adapter,filename=comparison_runtime
    value=performance.compare([filename],"ollama",threading.Event(),lambda *args:None)
    assert adapter.unloaded == 1 and adapter.load_count == 1
    assert value["models"][0]["samples"] == [20.0]*3
    assert value["models"][0]["total"] == 8
    assert value["raw_responses_stored"] is False and value["uploaded"] is False
    saved=json.loads((config.EVALUATIONS_DIR/value["report_filename"]).read_text(encoding="utf-8"))
    assert saved["pack_sha256"] == value["pack_sha256"]
    assert saved["generation"]["max_output_tokens"] == 64


def test_comparison_refuses_existing_loaded_work(comparison_runtime):
    adapter,filename=comparison_runtime
    adapter.busy=True
    with pytest.raises(ValueError,match="사용 중"):
        performance.compare([filename],"ollama",threading.Event(),lambda *args:None)
    assert adapter.load_count == adapter.unloaded == 0


def test_comparison_keeps_separate_results_for_multiple_models(comparison_runtime):
    adapter,filename=comparison_runtime
    second="another-model.gguf"
    (config.MODELS_DIR/second).write_bytes(b"different-GGUF")
    registry.upsert_entry(second,linked={"ollama":True})
    value=performance.compare([filename,second],"ollama",threading.Event(),lambda *args:None)
    assert [x["filename"] for x in value["models"]] == [filename,second]
    assert len({x["sha256"] for x in value["models"]}) == 2
    assert adapter.load_count == adapter.unloaded == 2


def test_failed_comparison_cleanup_does_not_publish_result(comparison_runtime):
    adapter,filename=comparison_runtime
    adapter.released=False
    with pytest.raises(ValueError,match="메모리 해제"):
        performance.compare([filename],"ollama",threading.Event(),lambda *args:None)
    assert not config.EVALUATIONS_DIR.exists()


def test_cancelled_comparison_releases_owned_load(monkeypatch,comparison_runtime):
    adapter,filename=comparison_runtime
    stop=threading.Event()
    def generate(*args):
        stop.set()
        return ProbeResult("18",20.0,2)
    monkeypatch.setattr(adapter,"generate",generate)
    with pytest.raises(InterruptedError):
        performance.compare([filename],"ollama",stop,lambda *args:None)
    assert adapter.unloaded == 1
    assert not config.EVALUATIONS_DIR.exists()


def test_import_revalidates_selected_bytes_before_adoption(monkeypatch,isolated_omm_home,tmp_path):
    source=tmp_path/'external.gguf'
    source.write_bytes(b"GGUF-model")
    from omm.hashutil import sha256_file
    from omm.scan_import import ExternalGguf
    monkeypatch.setattr(maintenance.scan_import,"find_external_models",lambda:[ExternalGguf("import",source.name,source,source.stat().st_size,sha256_file(source))])
    [row]=maintenance.scan()["groups"]
    selected=WebService().request({"operation":"import","id":row["id"],"confirmed":True})["group"]
    source.write_bytes(b"GGUF-other")
    with pytest.raises(ValueError,match="바뀌"):
        maintenance.adopt(selected)
    assert source.read_bytes() == b"GGUF-other"


def test_successful_import_updates_registry_and_removes_preview(monkeypatch,isolated_omm_home,tmp_path):
    source=tmp_path/'external.gguf'
    source.write_bytes(b"GGUF-model")
    from omm.hashutil import sha256_file
    from omm.scan_import import ExternalGguf
    digest=sha256_file(source)
    monkeypatch.setattr(maintenance.scan_import,"find_external_models",lambda:[ExternalGguf("import",source.name,source,source.stat().st_size,digest)])
    [row]=maintenance.scan()["groups"]
    selected=WebService().request({"operation":"import","id":row["id"],"confirmed":True})["group"]
    result=maintenance.adopt(selected)
    assert sha256_file(config.MODELS_DIR/result["filename"]) == digest
    assert registry.load_registry()[result["filename"]]["sha256"] == digest
    assert maintenance.read_import_preview() == []


def test_web_import_counts_saved_storage_like_cli(monkeypatch,isolated_omm_home,tmp_path):
    source=tmp_path/'external.gguf'
    source.write_bytes(b"GGUF-model")
    from omm.hashutil import sha256_file
    from omm.scan_import import AdoptResult, ExternalGguf
    digest=sha256_file(source)
    monkeypatch.setattr(maintenance.scan_import,"find_external_models",lambda:[ExternalGguf("import",source.name,source,source.stat().st_size,digest)])
    real=maintenance.scan_import.adopt_group
    def adopt(group):
        result=real(group)
        return AdoptResult(**{**result.__dict__,"bytes_saved":1234})
    monkeypatch.setattr(maintenance.scan_import,"adopt_group",adopt)
    [row]=maintenance.scan()["groups"]
    selected=WebService().request({"operation":"import","id":row["id"],"confirmed":True})["group"]
    maintenance.adopt(selected)
    assert config.load_config()["storage_saved_bytes"]==1234
    assert management.files()["storage_saved_bytes"]==1234


def test_files_view_reports_storage_saved_without_creating_config(isolated_omm_home):
    assert management.files()["storage_saved_bytes"]==0
    assert not config.CONFIG_PATH.exists()
    config.add_storage_saved_bytes(5*1024**3)
    assert management.files()["storage_saved_bytes"]==5*1024**3


def test_network_checks_are_fixed_and_not_retried(monkeypatch):
    import requests
    calls=[]
    def head(url,**kwargs):
        calls.append((url,kwargs))
        return SimpleNamespace(status_code=200,close=lambda:None)
    monkeypatch.setattr(requests,"head",head)
    value=management.check_network()
    assert len(calls) == value["requests"] == 2
    assert all(kwargs == {"timeout":3,"allow_redirects":False} for _,kwargs in calls)
