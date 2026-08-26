from __future__ import annotations

from typer.testing import CliRunner

from omm import cli
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="macOS",
        os_version="test",
        cpu="Apple Silicon",
        ram_total_gb=24,
        ram_available_gb=10,
        unified_memory=True,
        gpu_name="Apple GPU",
        vram_total_gb=24,
        vram_free_gb=10,
    )


def test_scan_displays_live_safe_budget(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Safe model budget now" in result.stdout
    assert "7.6 GB" in result.stdout
    assert "Reserved for apps/OS" in result.stdout


def test_scan_clears_stale_link_record_for_uninstalled_engine(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key != "jan")
    cli.registry.upsert_entry("model.gguf", linked={"jan": True, "ollama": True})

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Cleared stale link record" in result.stdout
    assert "model.gguf" in result.stdout
    reg = cli.registry.load_registry()
    assert reg["model.gguf"]["linked"] == {"jan": False, "ollama": True}


def test_scan_json_includes_stale_links_key(isolated_omm_home, monkeypatch):
    # The table path surfaces cleared stale link records as a "Cleared
    # stale link record(s) for: ..." hint line; --json dropped the same
    # signal entirely (see #81).
    import json

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key != "jan")
    cli.registry.upsert_entry("model.gguf", linked={"jan": True, "ollama": True})

    result = runner.invoke(cli.app, ["scan", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["stale_links"] == ["model.gguf"]


def test_scan_quiet_suppresses_hints_but_keeps_the_tables(isolated_omm_home, monkeypatch):
    # --quiet should drop the "Cleared stale link record.../Run: omm link"
    # style hints but keep the actual hardware/runner/model tables, which
    # are the result the user asked for, not decorative filler (see #80).
    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware())
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key != "jan")
    cli.registry.upsert_entry("model.gguf", linked={"jan": True, "ollama": True})

    result = runner.invoke(cli.app, ["scan", "--quiet"])

    assert result.exit_code == 0, result.stdout
    assert "Cleared stale link record" not in result.stdout
    assert "omm hardware scan" in result.stdout.lower()


def test_scan_leaves_link_record_untouched_when_engine_still_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: True)
    cli.registry.upsert_entry("model.gguf", linked={"jan": True, "ollama": True})

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Cleared stale link record" not in result.stdout
    reg = cli.registry.load_registry()
    assert reg["model.gguf"]["linked"] == {"jan": True, "ollama": True}


def test_scan_repeats_link_nag_for_unblocked_engine(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"fake-gguf")
    cli.registry.upsert_entry("model.gguf", linked={"ollama": False})

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Run: omm link" in result.stdout


def test_scan_stops_nagging_once_link_is_recorded_as_blocked(isolated_omm_home, monkeypatch):
    """A model whose only unlinked engine already failed with an unowned-
    manifest conflict (recorded by a prior `omm link` run) shouldn't keep
    telling the user to re-run `omm link` - that retry can't succeed until
    they resolve the conflict by hand."""
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"fake-gguf")
    cli.registry.upsert_entry(
        "model.gguf", linked={"ollama": False}, link_blocked=["ollama"]
    )

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Run: omm link" not in result.stdout


def test_scan_runner_table_shows_only_installed_engines(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Ollama" in result.stdout
    assert "LM Studio" not in result.stdout
    assert "not detected" not in result.stdout


def test_scan_runner_table_notes_missing_engine_count_with_link(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    missing_count = len(cli.linker.ENGINES) - 1
    assert f"+ {missing_count} program(s) not installed" in result.stdout
    assert cli.COMPATIBLE_PROGRAMS_URL in result.stdout


def test_scan_runner_table_omits_note_when_all_engines_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda: [])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: True)

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "not installed" not in result.stdout


def test_tune_uses_live_budget_for_installed_model(monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(
        cli.registry,
        "load_registry",
        lambda: {
            "model-7B-Q4.gguf": {
                "repo_id": "org/model-GGUF",
                "size_bytes": 4 * 1024**3,
            }
        },
    )

    result = runner.invoke(cli.app, ["tune", "model-7B-Q4.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "Safe model budget now" in result.stdout
    assert "7.6 GB" in result.stdout
    assert "Context length" in result.stdout


def test_tune_prompts_quant_picker_for_ambiguous_repo(monkeypatch):
    import questionary

    from omm.hub import AmbiguousModelError, ResolvedModel

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {})

    repo_id = "TheBloke/Llama-2-7B-GGUF"
    chosen_filename = "llama-2-7b.Q4_K_M.gguf"
    candidates = ["llama-2-7b.Q2_K.gguf", chosen_filename, "llama-2-7b.Q8_0.gguf"]

    calls = []

    def fake_resolve(name):
        calls.append(name)
        if name == repo_id:
            raise AmbiguousModelError(repo_id, candidates)
        return ResolvedModel(
            url="https://example.com/x.gguf", filename=chosen_filename, repo_id=repo_id, provider="huggingface"
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: chosen_filename)

    result = runner.invoke(cli.app, ["tune", repo_id])

    assert result.exit_code == 0, result.stdout
    assert calls == [repo_id, f"huggingface:{repo_id}:{chosen_filename}"]
    assert "Context length" in result.stdout


def test_tune_prompts_provider_picker_for_multi_provider_repo(monkeypatch):
    """Same dead end the quant picker fixed, one step earlier: a bare
    `org/repo` that both hubs carry has to ask, not exit 1."""
    import questionary

    from omm.hub import AmbiguousProviderError, ResolvedModel

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {})

    repo_id = "Qwen/Qwen3-8B-GGUF"
    filename = "Qwen3-8B-Q4_K_M.gguf"
    calls = []

    def fake_resolve(name):
        calls.append(name)
        if name == repo_id:
            raise AmbiguousProviderError(repo_id, ["huggingface", "modelscope"])
        return ResolvedModel(
            url="https://example.com/x.gguf",
            filename=filename,
            repo_id=repo_id,
            provider="modelscope",
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: "modelscope")

    result = runner.invoke(cli.app, ["tune", repo_id])

    assert result.exit_code == 0, result.stdout
    assert calls == [repo_id, f"modelscope:{repo_id}"]
    assert "Context length" in result.stdout


def test_tune_json_output(monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(
        cli.registry,
        "load_registry",
        lambda: {
            "model-7B-Q4.gguf": {
                "repo_id": "org/model-GGUF",
                "size_bytes": 4 * 1024**3,
            }
        },
    )

    result = runner.invoke(cli.app, ["tune", "model-7B-Q4.gguf", "--json"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("{"), result.stdout
    assert '"profile_name"' in result.stdout
