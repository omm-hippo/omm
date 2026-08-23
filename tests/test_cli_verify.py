from __future__ import annotations

from typer.testing import CliRunner

from omm import cli, registry, runtime_compatibility
from omm.engines import (
    LoadReceipt,
    ProbeResult,
    RuntimeHealth,
    RuntimeModel,
    UnloadResult,
)
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware() -> HardwareInfo:
    # Verify loads a model into the runtime, so its memory-guard pre-flight
    # check (like install's and benchmark's) reads live available RAM via
    # `cli.scan_hardware()`. Tests must supply deterministic hardware here
    # instead of falling through to the real machine's live state, or the
    # guard's decision - and these tests - become dependent on how much RAM
    # happens to be free on whatever host runs the suite.
    return HardwareInfo(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=16,
        ram_available_gb=12,
        unified_memory=False,
        gpu_name=None,
        vram_total_gb=None,
        vram_free_gb=None,
    )


class _CliAdapter:
    key = "ollama"

    def __init__(self, *, loaded=False):
        self.loaded = loaded

    def health(self):
        return RuntimeHealth(True, "1.0")

    def list_models(self):
        return [RuntimeModel("model", "model", self.loaded, "model" if self.loaded else None)]

    def load(self, model, options):
        runtime_model = RuntimeModel("model", "model", True, "model")
        return LoadReceipt(runtime_model, "model", self.loaded, not self.loaded)

    def generate(self, receipt, request):
        return ProbeResult("OK")

    def unload(self, receipt):
        return UnloadResult(True)


def _entry(**overrides):
    entry = {
        "linked": {"ollama": True, "lmstudio": False},
        "ollama_name": "model",
        "size_bytes": 4,
    }
    entry.update(overrides)
    return entry


def test_compatibility_ref_uses_exact_imported_ollama_runtime_name(monkeypatch):
    monkeypatch.setattr(
        cli.linker,
        "resolve_ollama_runtime_name",
        lambda filename, entry: "qwen3:4b",
    )

    reference = cli._compatibility_model_ref(
        "qwen3-4b.gguf",
        _entry(ollama_name="qwen3-4b"),
        "ollama",
    )

    assert reference.key == "qwen3:4b"
    assert "qwen3-4b" in reference.aliases


def test_verify_success_records_and_reports_result(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    adapter = _CliAdapter()
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: adapter)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)

    result = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "ollama", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Compatible" in result.stdout
    saved = registry.load_registry()["model.gguf"]["compatibility"]["ollama"]
    assert saved["status"] == "passed"


def test_verify_asks_before_loading_and_cancel_keeps_registry_unchanged(
    isolated_omm_home, monkeypatch
):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _CliAdapter())
    monkeypatch.setattr(cli, "_ask_confirm", lambda prompt: False)

    result = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "ollama"])

    assert result.exit_code == 0
    assert "cancelled" in result.stderr.lower()
    assert "compatibility" not in registry.load_registry()["model.gguf"]


def test_verify_does_not_ask_for_preloaded_model(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _CliAdapter(loaded=True))
    monkeypatch.setattr(
        cli,
        "_ask_confirm",
        lambda prompt: (_ for _ in ()).throw(AssertionError("asked")),
    )

    result = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "ollama"])

    assert result.exit_code == 0, result.output


def test_verify_failure_keeps_model_file_and_records_reason(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    model_file = cli.MODELS_DIR / "model.gguf"
    model_file.write_bytes(b"gguf")
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: _CliAdapter())
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    failed = runtime_compatibility.CompatibilityResult(
        "ollama",
        "failed",
        "2026-07-31T12:00:00+00:00",
        1,
        "1.0",
        "out_of_memory",
    )

    def record(filename, adapter, model_ref, **kwargs):
        registry.record_compatibility(filename, adapter.key, failed.registry_payload())
        return failed

    monkeypatch.setattr(cli, "verify_and_record", record)

    result = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "ollama", "--yes"])

    assert result.exit_code == 1
    assert "not enough memory" in result.stderr
    assert model_file.read_bytes() == b"gguf"
    assert registry.load_registry()["model.gguf"]["compatibility"]["ollama"]["failure_reason"] == "out_of_memory"


def test_verify_memory_guard_block_prevents_runtime_load(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    adapter = _CliAdapter()
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: adapter)
    monkeypatch.setattr(
        cli,
        "_guard_ollama_load",
        lambda tag, required_gb: (False, object(), False),
    )
    monkeypatch.setattr(
        cli,
        "verify_and_record",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    result = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "ollama", "--yes"])

    assert result.exit_code == 1


class _LmStudioCliAdapter:
    key = "lmstudio"

    def __init__(self, *, loaded=False):
        self.loaded = loaded

    def health(self):
        return RuntimeHealth(True, "0.4.1")

    def list_models(self):
        return [RuntimeModel("org/repo", "org/repo", self.loaded, "org/repo" if self.loaded else None)]

    def load(self, model, options):
        runtime_model = RuntimeModel("org/repo", "org/repo", True, "org/repo")
        return LoadReceipt(runtime_model, "org/repo", self.loaded, not self.loaded)

    def generate(self, receipt, request):
        return ProbeResult("OK")

    def unload(self, receipt):
        return UnloadResult(True)


def test_verify_lmstudio_memory_guard_block_prevents_runtime_load(isolated_omm_home, monkeypatch):
    """Mirrors test_verify_memory_guard_block_prevents_runtime_load, but for
    the LM Studio engine - LM Studio previously had no memory guard coverage
    at all on this command."""
    registry.save_registry(
        {
            "model.gguf": _entry(
                linked={"ollama": False, "lmstudio": True}, repo_id="org/repo"
            )
        }
    )
    adapter = _LmStudioCliAdapter()
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: adapter)
    monkeypatch.setattr(
        cli,
        "_guard_lmstudio_load",
        lambda model_key, required_gb: (False, object(), False),
    )
    monkeypatch.setattr(
        cli,
        "verify_and_record",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    result = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "lmstudio", "--yes"])

    assert result.exit_code == 1


def test_verify_lmstudio_memory_guard_allows_runtime_load(isolated_omm_home, monkeypatch):
    registry.save_registry(
        {
            "model.gguf": _entry(
                linked={"ollama": False, "lmstudio": True}, repo_id="org/repo"
            )
        }
    )
    adapter = _LmStudioCliAdapter()
    monkeypatch.setattr(cli, "_compatibility_adapter", lambda engine: adapter)
    guard_calls = []
    monkeypatch.setattr(
        cli,
        "_guard_lmstudio_load",
        lambda model_key, required_gb: (guard_calls.append(model_key) or True, object(), False),
    )

    result = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "lmstudio", "--yes"])

    assert result.exit_code == 0, result.output
    assert guard_calls == ["org/repo"]


def test_verify_rejects_unlinked_and_uninstalled_models(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry(linked={"ollama": False, "lmstudio": False})})

    unlinked = runner.invoke(cli.app, ["verify", "model.gguf", "--engine", "ollama", "--yes"])
    missing = runner.invoke(cli.app, ["verify", "missing.gguf", "--engine", "ollama", "--yes"])

    assert unlinked.exit_code == 1
    assert "not linked" in unlinked.stderr
    assert missing.exit_code == 1
    assert "not installed" in missing.stderr


def test_info_json_includes_compatibility_without_breaking_old_entries(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf", "--json"])

    assert result.exit_code == 0
    assert '"compatibility": {}' in result.stdout
