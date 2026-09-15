"""Tests for `recommend()`'s candidate ranking and install-ref building."""

from __future__ import annotations

import json

import questionary
import pytest
from typer.testing import CliRunner

from omm import cli
from omm.hardware import HardwareInfo
from omm.hub import ResolvedModel

runner = CliRunner()


def test_json_reports_profile_budget_and_eligible_package_count(monkeypatch, isolated_omm_home):
    candidates = [{"repo_id": f"org/Model{i}-1B", "filename": f"Model{i}-1B-Q4_K_M.gguf", "size_bytes": 1024**3} for i in range(12)]
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_load_recommendation_with_change_note", lambda config: ({"candidates": candidates}, False))
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda *args: [(c, 10) for c in candidates])
    result = runner.invoke(cli.app, ["recommend", "--json", "--profile", "minimal"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 10
    assert all(r["eligible_package_count"] == 12 for r in rows)
    assert all(r["profile_budget_gb"] == pytest.approx(3.2) for r in rows)
    assert all(r["within_profile"] is True and r["memory_estimate_basis"] == "file_size" for r in rows)


def test_recommend_rechecks_fit_with_cached_provider_file_size(monkeypatch, isolated_omm_home):
    import time
    from omm import recommend_facts
    large = {"repo_id": "bartowski/Qwen2.5-7B-Instruct-GGUF", "filename": "Qwen2.5-7B-Instruct-Q4_K_M.gguf"}
    small = {"repo_id": "org/small-1B", "filename": "small-1B-Q4_K_M.gguf"}
    artifact = {"candidates": [large, small]}
    hw = HardwareInfo("macOS", "", "Apple M5", 24, 8, True, "Apple M5", 24, 8)
    monkeypatch.setattr(cli, "scan_hardware", lambda: hw)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_load_recommendation_with_change_note", lambda config: (artifact, False))
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [(c, 10) for c in artifact["candidates"]])
    recommend_facts._path().write_text(json.dumps({"version": 1, "repos": {
        recommend_facts._key(large): {"fetched_at": time.time(), "metadata": {"pipeline_tag": "text-generation"}, "files": {large["filename"]: 4683074240}}
    }}), encoding="utf-8")
    monkeypatch.setattr(recommend_facts, "fetch", lambda *args: pytest.fail("ordinary recommend must not fetch provider facts"))
    result = runner.invoke(cli.app, ["recommend", "--profile", "minimal", "--json"])
    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)
    assert row["ref"] == "org/small-1B:small-1B-Q4_K_M.gguf"
    assert "size_bytes" not in large


def test_json_exposes_candidates_above_requested_profile_in_fallback(monkeypatch, isolated_omm_home):
    candidate = {"repo_id": "org/Model-8B", "filename": "Model-8B-Q4_K_M.gguf"}
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_load_recommendation_with_change_note", lambda config: ({"candidates": [candidate]}, False))
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda *args: [(candidate, 10)])
    result = runner.invoke(cli.app, ["recommend", "--json", "--profile", "minimal"])
    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)
    assert row["within_profile"] is False
    assert row["profile_budget_gb"] == pytest.approx(3.2)
    assert row["memory_estimate_basis"] == "model_name"


@pytest.fixture(autouse=True)
def _default_to_uninstalled_candidates(monkeypatch):
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [cli.recommend_status.NOT_INSTALLED] * len(candidates),
    )


def _hardware() -> HardwareInfo:
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


def test_recommend_builds_choice_values_via_exact_install_ref(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}
    captured_choices = []
    captured_options = {}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    def fake_select(prompt_text, choices, **kwargs):
        captured_choices.extend(choices)
        captured_options.update(kwargs)
        return _DummySelect()

    class _DummySelect:
        pass

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda select_obj: None)  # cancel path, avoids install()

    result = runner.invoke(cli.app, ["recommend"])
    assert result.exit_code == 0, result.stdout

    assert captured_choices[0].value == "ms:org/repo:model.gguf"
    assert captured_options["pointer"] == "❯"
    assert "Enter select" in captured_options["instruction"]


def test_static_rules_fallback_filters_gpu_host_on_vram_budget(monkeypatch, isolated_omm_home):
    """A discrete-GPU (non-unified-memory) host's static-rules budget must
    be judged against VRAM, not RAM * profile ratio - rules.matching_rules
    compares `available_gb` to each rule's `min_vram_gb` whenever
    `has_gpu` is True."""
    dgpu_hardware = HardwareInfo(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=32,
        ram_available_gb=28,
        unified_memory=False,
        gpu_name="GPU",
        vram_total_gb=4,
        vram_free_gb=4,
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: dgpu_hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (None, False)
    )
    captured = {}
    monkeypatch.setattr(
        cli.rules_mod,
        "matching_rules",
        lambda rules, available_gb, has_gpu: (
            captured.update(gb=available_gb, gpu=has_gpu),
            [],
        )[1],
    )

    result = runner.invoke(cli.app, ["recommend", "--json"])

    assert result.exit_code == 1
    assert captured["gpu"] is True
    assert captured["gb"] == pytest.approx(3.6)


def test_static_rules_fallback_unified_memory_uses_ram_budget(monkeypatch, isolated_omm_home):
    unified_hardware = HardwareInfo(
        os_name="Darwin",
        os_version="",
        cpu="CPU",
        ram_total_gb=32,
        ram_available_gb=28,
        unified_memory=True,
        gpu_name="GPU",
        vram_total_gb=32,
        vram_free_gb=32,
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: unified_hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (None, False)
    )
    captured = {}
    monkeypatch.setattr(
        cli.rules_mod,
        "matching_rules",
        lambda rules, available_gb, has_gpu: (
            captured.update(gb=available_gb, gpu=has_gpu),
            [],
        )[1],
    )

    result = runner.invoke(cli.app, ["recommend", "--json"])

    assert result.exit_code == 1
    assert captured["gb"] == pytest.approx(32 * 0.45)


def test_recommend_quiet_suppresses_status_lines(monkeypatch, isolated_omm_home):
    """`--quiet` accepts the flag (issue #80) but used to leave the
    "fetched updated data"/"falling back to static rules" status lines
    printing unconditionally - only the rules-fetch line was ever gated."""
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (None, True)
    )
    monkeypatch.setattr(cli.rules_mod, "matching_rules", lambda *a, **k: [])

    result = runner.invoke(cli.app, ["recommend", "--quiet"])

    assert result.exit_code == 1
    assert "Fetched updated recommendation data" not in result.output
    assert "No trained model available" not in result.output


def test_recommend_without_quiet_prints_status_lines(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (None, True)
    )
    monkeypatch.setattr(cli.rules_mod, "matching_rules", lambda *a, **k: [])

    result = runner.invoke(cli.app, ["recommend"])

    assert result.exit_code == 1
    assert "Fetched updated recommendation data" in result.output
    assert "No trained model available" in result.output


def test_recommend_json_lists_candidates_without_installing(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
        "pipeline_tag": "text-generation",
        "tags": ["coding"],
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    def fail_install(*a, **k):
        raise AssertionError("install() must not run under --json")

    monkeypatch.setattr(cli, "install", fail_install)
    monkeypatch.setattr(cli, "_select_recommended_model", fail_install)

    result = runner.invoke(cli.app, ["recommend", "--json"])

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    row = rows[0]
    assert row["rank"] == 1
    assert row["ref"] == "ms:org/repo:model.gguf"
    assert row["name"] == cli.recommend_ui.humanize_model_name(candidate)
    assert row["model_type"] == "LLM"
    assert row["use_case"] == "Coding"
    assert row["model_type_source"] == row["use_case_source"] == "Catalog metadata"
    assert row["declared_features"] == []
    assert row["predicted_tokens_per_second"] == 42.0
    assert row["installed"] is False
    assert row["managed_by_omm"] is False
    assert row["installed_engines"] == []
    assert row["installation_match"] is None


def test_recommend_yes_installs_top_candidate_without_prompting(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    def fail_select(*a, **k):
        raise AssertionError("interactive picker must not run under --yes")

    monkeypatch.setattr(cli, "_select_recommended_model", fail_select)

    installed = []
    monkeypatch.setattr(cli, "install", lambda ref: installed.append(ref))

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert installed == ["ms:org/repo:model.gguf"]


def test_recommend_install_does_not_leak_typer_option_sentinels(
    monkeypatch, isolated_omm_home
):
    """`_finish_recommendation` calls `install()` as a plain function with
    only the model name, so `skip_unfit`/`force`/`upload` used to reach
    `_install_impl` as truthy Typer OptionInfo objects. A truthy
    `skip_unfit` silently turns a link/disk failure into a no-op install."""
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "huggingface",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    monkeypatch.setattr(
        cli,
        "_resolve_model_interactive",
        lambda name: ResolvedModel(
            url="https://example.com/model.gguf",
            filename="model.gguf",
            repo_id="org/repo",
        ),
    )

    seen = {}

    def fake_install_impl(resolved, **kwargs):
        seen.update(kwargs)
        return cli.InstallOutcome("model.gguf", "org/repo", linked={"ollama": True})

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert seen["skip_unfit"] is False
    assert seen["force"] is False
    assert seen["auto_upload"] is False
    assert seen["no_upload"] is False


def test_recommend_yes_skips_installed_top_candidate(monkeypatch, isolated_omm_home):
    installed_candidate = {
        "repo_id": "org/installed",
        "filename": "installed.gguf",
        "description": "test",
    }
    new_candidate = {
        "repo_id": "org/new",
        "filename": "new.gguf",
        "description": "test",
    }
    artifact = {"candidates": [installed_candidate, new_candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor,
        "rank_candidates",
        lambda artifact, hw: [(installed_candidate, 50.0), (new_candidate, 40.0)],
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [
            cli.recommend_status.InstallationStatus(
                True, True, ("ollama",), "installed.gguf"
            ),
            cli.recommend_status.NOT_INSTALLED,
        ],
    )

    installed = []
    monkeypatch.setattr(cli, "install", lambda ref: installed.append(ref))

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert installed == ["org/new:new.gguf"]


def test_recommend_yes_stops_when_every_candidate_is_installed(
    monkeypatch, isolated_omm_home
):
    candidate = {
        "repo_id": "org/installed",
        "filename": "installed.gguf",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 50.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [
            cli.recommend_status.InstallationStatus(
                True, True, ("ollama",), "installed.gguf"
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "install",
        lambda ref: (_ for _ in ()).throw(AssertionError("must not reinstall")),
    )

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "All recommended models are already installed" in result.output


def test_recommend_selecting_installed_candidate_does_not_reinstall(
    monkeypatch, isolated_omm_home
):
    candidate = {
        "repo_id": "org/installed",
        "filename": "installed.gguf",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 50.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [
            cli.recommend_status.InstallationStatus(
                True, True, ("ollama",), "installed.gguf"
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "_select_recommended_model",
        lambda info, ranked, refs, installations, **kwargs: refs[0],
    )
    monkeypatch.setattr(
        cli,
        "install",
        lambda ref: (_ for _ in ()).throw(AssertionError("must not reinstall")),
    )

    result = runner.invoke(cli.app, ["recommend"])

    assert result.exit_code == 0, result.stdout
    assert "already installed via OMM" in result.output
    assert "omm run installed.gguf" in result.output


def _two_candidates():
    small = {
        "name": "small/repo",
        "repo_id": "small/repo",
        "filename": "small.gguf",
        "provider": "modelscope",
        "description": "test",
        "size_bytes": int(1 * 1024**3),
    }
    big = {
        "name": "big/repo",
        "repo_id": "big/repo",
        "filename": "big.gguf",
        "provider": "modelscope",
        "description": "test",
        "size_bytes": int(8 * 1024**3),
    }
    return small, big


@pytest.mark.parametrize("path", ["profile", "relaxed", "slow"])
def test_recommend_shortlist_dedupes_and_demotes_variants_in_every_path(
    monkeypatch, isolated_omm_home, path
):
    def candidate(model, uploader="org"):
        return {
            "repo_id": f"{uploader}/{model}-GGUF",
            "filename": f"{model}-Q4_K_M.gguf",
            "size_bytes": 1024**3,
        }

    special = candidate("Qwen3.8-27B-Uncensored")
    normal = candidate("Qwen3.8-27B")
    normal.update(provider="modelscope", pipeline_tag="text-generation", tags=["coding", "tool-use"])
    mirrors = [candidate("Qwen3.8-27B", str(i)) for i in range(12)]
    for mirror in mirrors:
        mirror.update(pipeline_tag="text-generation", tags=["translation"])
    installed = candidate("gpt-oss-20b")
    candidates = [special, normal, *mirrors, installed]
    artifact = {"candidates": candidates}
    speed = 2.0 if path == "slow" else 6.0
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_load_recommendation_with_change_note", lambda config: (artifact, False))
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [(c, speed) for c in candidates])
    if path == "relaxed":
        monkeypatch.setattr(cli.predictor, "filter_by_profile", lambda *args: [])
    monkeypatch.setattr(
        cli.recommend_status, "detect_installation_statuses",
        lambda rows: [cli.recommend_status.InstallationStatus(c == installed) for c in rows],
    )
    monkeypatch.setattr(cli, "install", lambda ref: pytest.fail("JSON must not install"))

    result = runner.invoke(cli.app, ["recommend", "--json", "--profile", "dedicated"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [r["name"] for r in rows] == ["Qwen3.8 27B", "gpt oss 20b", "Qwen3.8 27B Uncensored"]
    assert rows[0]["ref"] == "ms:org/Qwen3.8-27B-GGUF:Qwen3.8-27B-Q4_K_M.gguf"
    assert rows[0]["model_type"] == "LLM"
    assert rows[0]["use_case"] == "Coding"
    assert rows[0]["model_type_source"] == rows[0]["use_case_source"] == "Catalog metadata"
    assert rows[0]["declared_features"] == ["Tool use"]
    assert rows[0]["predicted_tokens_per_second"] == speed
    assert rows[1]["model_type"] == "Unknown"
    assert rows[1]["use_case"] == "—"
    assert rows[1]["installed"] is True
    assert rows[2]["warning"]


def test_recommend_yes_chooses_normal_candidate_before_specialized(monkeypatch, isolated_omm_home):
    special = {"repo_id": "org/Qwen3-8B-MTP-GGUF", "filename": "Qwen3-8B-MTP-Q4_K_M.gguf"}
    normal = {"repo_id": "org/Qwen3-4B-GGUF", "filename": "Qwen3-4B-Q4_K_M.gguf"}
    artifact = {"candidates": [special, normal]}
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_load_recommendation_with_change_note", lambda config: (artifact, False))
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [(special, 30.0), (normal, 20.0)])
    installed_refs = []
    monkeypatch.setattr(cli, "install", installed_refs.append)

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.output
    assert installed_refs == ["org/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M.gguf"]


def test_recommend_json_defaults_to_balanced_profile_and_filters_by_it(
    monkeypatch, isolated_omm_home
):
    small, big = _two_candidates()
    artifact = {"candidates": [small, big]}

    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware())
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor,
        "rank_candidates",
        lambda artifact, hw: [(small, 20.0), (big, 14.0)],
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    result = runner.invoke(cli.app, ["recommend", "--json"])

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    # 16GB * 0.45 (balanced) = 7.2GB - the ~9.6GB big candidate is excluded.
    assert [row["ref"] for row in rows] == ["ms:small/repo:small.gguf"]
    assert rows[0]["profile"] == "balanced"


def test_recommend_json_dedicated_profile_keeps_the_larger_candidate(
    monkeypatch, isolated_omm_home
):
    small, big = _two_candidates()
    artifact = {"candidates": [small, big]}

    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware())
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor,
        "rank_candidates",
        lambda artifact, hw: [(small, 20.0), (big, 14.0)],
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    result = runner.invoke(cli.app, ["recommend", "--json", "--profile", "dedicated"])

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    refs = [row["ref"] for row in rows]
    assert refs[0] == "ms:big/repo:big.gguf"
    assert rows[0]["profile"] == "dedicated"


def test_recommend_rejects_unknown_profile(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware())
    monkeypatch.setattr(cli, "load_config", lambda: {})

    result = runner.invoke(cli.app, ["recommend", "--profile", "yolo"])

    assert result.exit_code == 1
    assert "--profile must be one of" in result.output


def test_recommend_prompts_for_profile_on_a_tty(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware())
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    # questionary.select() itself probes the console on construction - stub
    # it out too, not just _ask_select, or this crashes on Windows CI where
    # there's no real console behind the spoofed _stdin_is_tty.
    monkeypatch.setattr(questionary, "select", lambda *a, **k: object())

    asked_profile_prompt = []

    def fake_ask_select(select_obj):
        # First call is the profile prompt; second is the model picker,
        # cancelled here so the test doesn't have to drive install().
        if not asked_profile_prompt:
            asked_profile_prompt.append("minimal")
            return "minimal"
        return None

    monkeypatch.setattr(cli, "_ask_select", fake_ask_select)

    seen_profiles = []
    real_filter_by_profile = cli.predictor.filter_by_profile

    def spy_filter_by_profile(usable, hw, profile):
        seen_profiles.append(profile)
        return real_filter_by_profile(usable, hw, profile)

    monkeypatch.setattr(cli.predictor, "filter_by_profile", spy_filter_by_profile)

    result = runner.invoke(cli.app, ["recommend"])

    assert result.exit_code == 0, result.stdout
    assert "Cancelled" in result.output
    assert seen_profiles == ["minimal"]


def test_recommend_cancel_at_profile_prompt_exits_cleanly(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(cli, "scan_hardware", lambda: _hardware())
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_ask_select", lambda select_obj: None)

    def fail(*a, **k):
        raise AssertionError("must not fetch recommendations after cancelling the prompt")

    monkeypatch.setattr(cli, "_load_recommendation_with_change_note", fail)

    result = runner.invoke(cli.app, ["recommend"])

    assert result.exit_code == 0, result.stdout
    assert "Cancelled" in result.output
