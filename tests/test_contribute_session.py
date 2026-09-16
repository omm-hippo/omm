import threading
from pathlib import Path

import pytest

from omm import calibration, cli, config, install_state, registry
from omm.atomic import locked
from omm.contribute_session import ContributionSession
from omm.hardware import HardwareInfo


class Queue:
    def __init__(self, filename):
        self.candidate = {"provider": "huggingface", "repo_id": "test/model", "filename": filename}
        self.seen = False

    def next_candidate(self, **kwargs):
        return None if self.seen else self.candidate

    def mark_seen(self, ref):
        self.seen = True


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.setattr(cli, "_engine_daemon_reachable", lambda engine: True)
    monkeypatch.setattr(cli, "_contribute_candidate_memory_plan", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_contribute_native_model_exists", lambda *args: False)
    return lambda filename: cli._run_contribution_loop(Queue(filename), threading.Event(), None)


@pytest.mark.parametrize("kind", ["registered", "unregistered", "partial", "stale-registration"])
def test_existing_models_are_never_adopted_or_removed(loop, isolated_omm_home, monkeypatch, kind):
    path = config.MODELS_DIR / "existing.gguf"
    if kind in {"registered", "unregistered"}:
        path.write_bytes(b"user model")
    if kind in {"registered", "stale-registration"}:
        registry.upsert_entry(path.name, sha256="a" * 64, custom_links=["user link"], linked={"ollama": True})
    if kind == "partial":
        path.with_suffix(".gguf.part").write_bytes(b"user partial download")
    before = {p: p.read_bytes() for p in config.OMM_HOME.rglob("*") if p.is_file()}
    monkeypatch.setattr(cli, "_install_impl", lambda *a, **k: pytest.fail("must not touch an existing model"))
    stats = loop(path.name)
    assert stats.preserved_models == (path.name,)
    assert stats.removed_models == () and not stats.exhausted
    assert all(p.read_bytes() == content for p, content in before.items())


def test_new_partial_is_cleaned_even_when_install_raises(loop, isolated_omm_home, monkeypatch):
    path = config.MODELS_DIR / "new.gguf.part"
    def interrupted(resolved, **kwargs):
        path.write_bytes(b"only this run owns these bytes")
        raise cli.InstallInterrupted(resolved.filename, downloaded_now=False)
    monkeypatch.setattr(cli, "_install_impl", interrupted)
    stats = loop("new.gguf")
    assert not path.exists()
    assert stats.removed_models == ("new.gguf",)


def test_native_model_outside_omm_is_preserved(loop, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_contribute_native_model_exists", lambda *args: True)
    monkeypatch.setattr(cli, "_install_impl", lambda *a, **k: pytest.fail("native model must stay untouched"))
    stats = loop("native.gguf")
    assert stats.preserved_models == ("native.gguf",)
    assert stats.removed_models == ()


def test_other_install_lock_preserves_the_model(loop, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_install_impl", lambda *a, **k: pytest.fail("must not compete with an install"))
    with locked(install_state._path("new.gguf"), timeout=0):
        stats = loop("new.gguf")
    assert stats.preserved_models == ("new.gguf",)


def test_owned_model_lock_covers_cleanup_and_is_released(isolated_omm_home):
    checks = []
    def cleanup(filename):
        from filelock import Timeout
        # Direct lock acquisition simulates another process, bypassing the
        # owner-context's deliberate reentrancy for its own cleanup calls.
        with pytest.raises(Timeout), locked(install_state._path(filename), timeout=0):
            pass
        checks.append(filename)
        return True
    session = ContributionSession(cleanup)
    assert session.claim("new.gguf", config.MODELS_DIR / "new.gguf")
    session.release()
    assert checks == ["new.gguf"]
    with locked(install_state._path("new.gguf"), timeout=0):
        pass


def test_lmstudio_calibration_does_not_change_ollama_profile(isolated_omm_home, monkeypatch):
    hardware = HardwareInfo(os_name="macOS", os_version="test", cpu="test", ram_total_gb=16,
                            ram_available_gb=12, unified_memory=True, gpu_name="test",
                            vram_total_gb=None, vram_free_gb=None)
    calibration.record_calibration(hardware, measured_tokens_per_sec=10, predicted_tokens_per_sec=20)
    monkeypatch.setattr(cli, "scan_hardware", lambda: hardware)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    def prediction(*args, **kwargs):
        assert kwargs["engine"] == "lmstudio"
        assert kwargs["apply_calibration"] is False
        return 25, 20, 30
    monkeypatch.setattr(cli.predictor, "predict_speed_interval", prediction)
    cli._maybe_auto_calibrate("model.gguf", "test/model", Path("missing-fixture.gguf"), 20, engine="lmstudio")
    assert calibration.calibration_factor(hardware, engine="lmstudio") == 0.8
    assert calibration.calibration_factor(hardware, engine="ollama") == 0.5
