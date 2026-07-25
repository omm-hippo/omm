"""Shared pytest fixtures for the omm test suite."""

from __future__ import annotations

import pytest

from omm import calibration, catalog, cli, config, linker, predictor, registry, scan_import


@pytest.fixture(autouse=True)
def _no_real_engine_writes(tmp_path, monkeypatch):
    """Safety net for the engines that detect themselves via a fixed,
    real-home-relative directory (Jan/AnythingLLM/Msty) or a heuristic
    filesystem search (KoboldCpp/text-generation-webui): point their
    directory lookups at a throwaway tmp_path instead of the real machine,
    so a test can never silently write into (or "detect") a real local AI
    app actually installed on whatever machine runs the suite. Redirects
    the underlying directory functions rather than stubbing is_X_installed
    itself, so tests that exercise that detection logic directly still can.
    Ollama/LM Studio are untouched here - existing tests already discipline
    themselves to mock those two explicitly wherever it matters."""
    monkeypatch.setattr(linker, "jan_app_dir", lambda: tmp_path / "_no_real_jan")
    monkeypatch.setattr(linker, "anythingllm_app_dir", lambda: tmp_path / "_no_real_anythingllm")
    monkeypatch.setattr(linker, "mstystudio_app_dir", lambda: tmp_path / "_no_real_mstystudio")
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path / "_no_real_heuristic_root"])
    monkeypatch.setattr(linker, "_APP_BUNDLE_SEARCH_ROOTS", [tmp_path / "_no_real_app_bundle_root"])
    linker.find_koboldcpp_binary.cache_clear()
    linker.find_textgenwebui_root.cache_clear()
    yield
    linker.find_koboldcpp_binary.cache_clear()
    linker.find_textgenwebui_root.cache_clear()


@pytest.fixture
def isolated_omm_home(tmp_path, monkeypatch):
    """Redirect all of omm's ~/.omm paths into a throwaway tmp_path so
    tests never touch (or depend on) the real user home directory."""
    home = tmp_path / ".omm"
    models_dir = home / "models"

    monkeypatch.setattr(config, "OMM_HOME", home)
    monkeypatch.setattr(config, "MODELS_DIR", models_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(config, "REGISTRY_PATH", home / "models.json")
    monkeypatch.setattr(config, "RULES_PATH", home / "rules.json")
    monkeypatch.setattr(config, "RECOMMEND_MODEL_PATH", home / "recommend-model.json")
    monkeypatch.setattr(config, "EVALUATIONS_DIR", home / "evaluations")
    monkeypatch.setattr(config, "CALIBRATION_PATH", home / "calibration.json")
    monkeypatch.setattr(config, "CATALOG_HISTORY_DIR", home / "catalog-history")

    monkeypatch.setattr(registry, "REGISTRY_PATH", config.REGISTRY_PATH)
    monkeypatch.setattr(cli, "MODELS_DIR", models_dir)
    monkeypatch.setattr(scan_import, "MODELS_DIR", models_dir)
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", config.RECOMMEND_MODEL_PATH)
    monkeypatch.setattr(calibration, "CALIBRATION_PATH", config.CALIBRATION_PATH)
    monkeypatch.setattr(catalog, "RECOMMEND_MODEL_PATH", config.RECOMMEND_MODEL_PATH)
    monkeypatch.setattr(catalog, "CATALOG_HISTORY_DIR", config.CATALOG_HISTORY_DIR)

    config.ensure_omm_home()
    return home
