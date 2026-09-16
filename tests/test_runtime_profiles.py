from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from omm import cli, config, registry, runtime_profiles as profiles
from omm.engines import LoadOptions, LoadReceipt, ProbeResult, RuntimeHealth, RuntimeModel, RuntimeModelRef, UnloadResult
from omm.hardware import HardwareInfo


def hardware():
    return HardwareInfo(os_name="macOS", os_version="test", cpu="CPU", ram_total_gb=16,
                        ram_available_gb=12, unified_memory=True, gpu_name="GPU",
                        vram_total_gb=None, vram_free_gb=None)


class Adapter:
    key = "ollama"
    def __init__(self):
        self.loaded = False
        self.requests = []
        self.release = True
        self.fail_generation = False
    def health(self):
        return RuntimeHealth(True, "test-runtime")
    def list_models(self):
        return [RuntimeModel("model", "model", self.loaded)]
    def load(self, model, options):
        self.requests.append(options)
        self.loaded = True
        return LoadReceipt(RuntimeModel("model", "model", True), "instance", False, True,
                           options, {"context_length": options.context_length})
    def generate(self, receipt, request):
        if self.fail_generation:
            raise profiles.RuntimeAdapterError("generation_timeout", "timed out")
        return ProbeResult("private response must never be stored", 20.0, 8)
    def unload(self, receipt):
        if self.release:
            self.loaded = False
        return UnloadResult(self.release)


@pytest.fixture
def model(isolated_omm_home, monkeypatch):
    path = config.MODELS_DIR / "model.gguf"
    path.write_bytes(b"model bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(profiles, "_metadata", lambda path: {"general.architecture": "llama", "llama.context_length": 2048, "llama.block_count": 8})
    registry.upsert_entry(path.name, sha256=digest, size_bytes=path.stat().st_size,
                          linked={"ollama": True}, ollama_name="model")
    return path, digest


def test_successful_trial_saves_only_settings_and_evidence_then_restores_defaults(model):
    path, digest = model
    adapter = Adapter()
    options = LoadOptions(512, cpu_threads=2, batch_size=64, gpu_layers=0, verify_applied=True)
    evidence = profiles.trial(path, adapter, RuntimeModelRef("model"), options, hardware)
    assert adapter.loaded is False
    profiles.save_trial(path.name, "ollama", path, digest, evidence, expected_revision=0)
    assert profiles.saved_options_for_file(path.name, "ollama", path) == options
    saved = profiles._path().read_text(encoding="utf-8")
    assert "private response" not in saved and "prompt" not in saved
    assert profiles.restore(path.name, "ollama", digest)["status"] == "default"
    assert profiles.saved_options_for_file(path.name, "ollama", path) is None
    assert profiles.restore(path.name, "ollama", digest)["status"] == "active"


def test_generation_failure_releases_the_trial_and_leaves_store_untouched(model):
    path, _ = model
    adapter = Adapter()
    adapter.fail_generation = True
    with pytest.raises(profiles.ProfileError, match="timed out"):
        profiles.trial(path, adapter, RuntimeModelRef("model"), LoadOptions(512), hardware)
    assert adapter.loaded is False
    assert not profiles._path().exists()


def test_unconfirmed_cleanup_is_not_a_successful_trial(model):
    path, _ = model
    adapter = Adapter()
    adapter.release = False
    with pytest.raises(profiles.ProfileError, match="could not be confirmed"):
        profiles.trial(path, adapter, RuntimeModelRef("model"), LoadOptions(512), hardware)
    assert not profiles._path().exists()


def test_existing_loaded_work_is_never_unloaded_for_tuning(model):
    path, _ = model
    adapter = Adapter()
    adapter.loaded = True
    with pytest.raises(profiles.ProfileError, match="already has loaded"):
        profiles.trial(path, adapter, RuntimeModelRef("model"), LoadOptions(512), hardware)
    assert adapter.loaded is True and adapter.requests == []


def test_memory_shortage_prevents_load(model):
    path, _ = model
    adapter = Adapter()
    with pytest.raises(profiles.ProfileError, match="Not enough free RAM"):
        profiles.trial(path, adapter, RuntimeModelRef("model"), LoadOptions(512),
                       lambda: replace(hardware(), ram_available_gb=0.1))
    assert adapter.requests == []


def test_proposed_context_respects_a_small_models_declared_limit(model, monkeypatch):
    path, _ = model
    monkeypatch.setattr(profiles, "_metadata", lambda path: {"general.architecture": "llama", "llama.context_length": 128})
    proposed = profiles.tuning.recommend_runtime_settings(hardware(), {"size_bytes": path.stat().st_size})
    options = profiles.proposed_options(proposed, path, "ollama")
    assert options.context_length == 128 and options.batch_size == 128
    profiles.ensure_memory(path, options, hardware())
    monkeypatch.setattr(cli, "scan_hardware", hardware)
    preview = CliRunner().invoke(cli.app, ["tune", path.name, "--json"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["context_length"] == 128


def test_model_replacement_and_concurrent_profile_save_are_rejected(model):
    path, digest = model
    evidence = profiles.trial(path, Adapter(), RuntimeModelRef("model"), LoadOptions(512), hardware)
    profiles.save_trial(path.name, "ollama", path, digest, evidence, expected_revision=0)
    with pytest.raises(profiles.ProfileError, match="changed concurrently"):
        profiles.save_trial(path.name, "ollama", path, digest, evidence, expected_revision=0)
    path.write_bytes(b"different model")
    with pytest.raises(profiles.ProfileError, match="model changed"):
        profiles.save_trial(path.name, "ollama", path, digest, evidence, expected_revision=1)
    with pytest.raises(profiles.ProfileError, match="Model bytes changed"):
        profiles.saved_options_for_file(path.name, "ollama", path)


def test_corrupt_store_is_not_silently_overwritten(model):
    path, digest = model
    profiles._path().write_text("bad json", encoding="utf-8")
    with pytest.raises(profiles.ProfileError):
        profiles.restore(path.name, "ollama", digest)
    assert profiles._path().read_text(encoding="utf-8") == "bad json"


def test_cli_apply_verify_save_and_restore_flow(model, monkeypatch):
    path, digest = model
    adapter = Adapter()
    monkeypatch.setattr(cli, "scan_hardware", hardware)
    monkeypatch.setattr(cli, "_ensure_engine_running", lambda engine, *a, **k: (engine, None))
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: adapter)
    monkeypatch.setattr(cli, "_compatibility_model_ref", lambda *a: RuntimeModelRef("model"))
    result = CliRunner().invoke(cli.app, ["tune", path.name, "--apply", "--save", "--engine", "ollama", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["saved"] is True and report["quality_evaluated"] is False
    assert len(adapter.requests) == 2 and adapter.loaded is False
    restored = CliRunner().invoke(cli.app, ["setting", "runtime-profile", path.name, "--restore", "--json"])
    assert restored.exit_code == 0, restored.output
    assert json.loads(restored.stdout)["status"] == "default"


def test_save_flag_without_a_trial_is_rejected_before_network(model):
    path, _ = model
    result = CliRunner().invoke(cli.app, ["tune", path.name, "--save", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["exit_code"] == 2


def test_verify_receives_saved_load_options(model, monkeypatch):
    path, digest = model
    adapter = Adapter()
    options = LoadOptions(512, cpu_threads=2, batch_size=64, gpu_layers=0, verify_applied=True)
    evidence = profiles.trial(path, adapter, RuntimeModelRef("model"), options, hardware)
    profiles.save_trial(path.name, "ollama", path, digest, evidence, expected_revision=0)
    monkeypatch.setattr(cli, "scan_hardware", hardware)
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda key: adapter)
    monkeypatch.setattr(cli, "_compatibility_model_ref", lambda *a: RuntimeModelRef("model"))
    monkeypatch.setattr(cli, "_guard_engine_load", lambda *a: (True, None, False))
    result = CliRunner().invoke(cli.app, ["verify", path.name, "--engine", "ollama", "--yes"])
    assert result.exit_code == 0, result.output
    assert adapter.requests[-1] == options


def test_saved_ollama_profile_reaches_native_launcher_and_cleans_its_alias(model):
    path, _ = model
    class ProfileAdapter(Adapter):
        def create_profile_model(self, model, options, *, on_prepare=None):
            alias = RuntimeModelRef("omm-profile-" + "a" * 32)
            on_prepare(alias)
            return alias
        def remove_profile_model(self, alias):
            self.removed = alias.key
            self.loaded = False
    adapter = ProfileAdapter()
    launched = []
    result, applied = profiles.launch_with_profile(
        path, adapter, RuntimeModelRef("model"), LoadOptions(512), hardware,
        lambda tag: launched.append(tag) or SimpleNamespace(ok=True),
    )
    assert result.ok and applied
    assert launched == [adapter.removed]
    assert adapter.loaded is False
    assert list((config.OMM_HOME / "runtime-sessions").glob("*.json")) == []


def test_native_launch_failure_still_releases_the_profile_alias(model):
    path, _ = model
    class ProfileAdapter(Adapter):
        def create_profile_model(self, model, options, *, on_prepare=None):
            alias = RuntimeModelRef("omm-profile-" + "b" * 32)
            on_prepare(alias)
            return alias
        def remove_profile_model(self, alias):
            self.loaded = False
    adapter = ProfileAdapter()
    def fail(tag):
        raise OSError("native launcher failed")
    with pytest.raises(OSError):
        profiles.launch_with_profile(path, adapter, RuntimeModelRef("model"), LoadOptions(512), hardware, fail)
    assert adapter.loaded is False
    assert list((config.OMM_HOME / "runtime-sessions").glob("*.json")) == []
