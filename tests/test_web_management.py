"""Management contracts, real local persistence and owned-runtime failure paths."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from omm import config, hardware, model_wiki, predictor, registry, runtime_profiles, usage
from omm.engines.base import LoadReceipt, ProbeResult, RuntimeHealth, RuntimeModel
from omm.web import discovery, maintenance, management, performance
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


def test_wiki_packages_use_exact_relations_not_model_family(monkeypatch,isolated_omm_home):
    hw=hardware.HardwareInfo("macOS","","Apple M5",24,20,True,"Apple M5",24,20)
    monkeypatch.setattr(hardware,"scan_hardware",lambda:hw)
    candidates=[{"repo_id":"Qwen/Qwen3-8B-GGUF","filename":"Qwen3-8B-Q4_K_M.gguf","size_bytes":4_000_000_000},
                {"repo_id":"unknown/Qwen3-8B-GGUF","filename":"Qwen3-8B-Q4_K_M.gguf","size_bytes":4_000_000_000}]
    monkeypatch.setattr(predictor,"load_cached_model",lambda:{"candidates":candidates})
    value=WebService().wiki_packages("qwen3-8b")
    assert len(value["packages"]) == 1
    assert value["packages"][0]["ref"].startswith("Qwen/")
    assert value["packages"][0]["fits"] is True


def test_package_discovery_rejects_name_only_and_split_shards(monkeypatch):
    model=next(x for x in model_wiki.catalog()["models"] if x["id"] == "phi-4-mini-instruct")
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,url,**kwargs):
            assert kwargs["allow_redirects"] is False
            if url.endswith('/api/models'):
                data=[{"id":"vendor/phi-4-mini-instruct-wrong","cardData":{"base_model":"other/model"}},
                      {"id":"vendor/phi-4-mini-instruct-GGUF","cardData":{"base_model":model["repository"]}}]
            else:
                assert 'wrong' not in url
                data={"cardData":{"base_model":model["repository"]},"siblings":[
                    {"rfilename":"model-Q4_K_M.gguf","size":1_000_000_000},
                    {"rfilename":"model-00001-of-00002.gguf","size":1_000_000_000},
                    {"rfilename":"../escape.gguf","size":1_000_000_000}]}
            return SimpleNamespace(json=lambda:data,raise_for_status=lambda:None,url=url)
    import requests
    monkeypatch.setattr(requests,"Session",Client)
    [row]=discovery.discover(model)
    assert row["filename"] == "model-Q4_K_M.gguf"
    assert row["wiki"]["match"] == "publisher_declared_base"


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
