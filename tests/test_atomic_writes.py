from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import filelock
import pytest

from omm import atomic, benchmark_history, config, registry


def test_registry_parallel_upserts_do_not_lose_entries(isolated_omm_home):
    def write(index: int) -> None:
        registry.upsert_entry(f"model-{index}.gguf", size_bytes=index, linked={})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    saved = registry.load_registry()
    assert len(saved) == 40
    assert saved["model-39.gguf"]["size_bytes"] == 39


def test_benchmark_history_parallel_updates_do_not_lose_entries(isolated_omm_home):
    def write(index: int) -> None:
        benchmark_history.record_benchmarked(
            f"org/model-{index}:model-{index}.gguf",
            repo_id=f"org/model-{index}",
            filename=f"model-{index}.gguf",
            sha256=str(index),
            tokens_per_sec=float(index + 1),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    assert len(benchmark_history.loaded_refs()) == 40


def test_config_parallel_updates_do_not_overwrite_each_other(isolated_omm_home):
    def write(index: int) -> None:
        config.update_config(**{f"field_{index}": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    saved = config.load_config()
    assert all(saved[f"field_{index}"] == index for index in range(40))


def test_corrupt_files_are_preserved_before_safe_fallback(isolated_omm_home):
    config.CONFIG_PATH.write_text("{broken-config", encoding="utf-8")
    config.REGISTRY_PATH.write_text("{broken-registry", encoding="utf-8")

    assert config.load_config()["telemetry_send_policy"] == "ask"
    assert registry.load_registry() == {}
    assert list(config.CONFIG_PATH.parent.glob("config.json.corrupt-*"))
    assert list(config.REGISTRY_PATH.parent.glob("models.json.corrupt-*"))


def test_corrupt_backup_preserves_non_utf8_bytes_exactly(tmp_path):
    path = tmp_path / "broken.json"
    content = b'\xff\xfe{"broken"'
    path.write_bytes(content)

    backup = atomic.backup_corrupt_file(path)

    assert backup is not None
    assert backup.read_bytes() == content


def test_registry_upsert_repairs_malformed_entry(isolated_omm_home):
    config.REGISTRY_PATH.write_text('{"model.gguf": "broken"}', encoding="utf-8")

    registry.upsert_entry("model.gguf", size_bytes=12, linked={"ollama": True})

    assert registry.load_registry()["model.gguf"] == {
        "linked": {"ollama": True},
        "size_bytes": 12,
    }


def test_replace_temporary_retries_a_transient_permission_error(tmp_path, monkeypatch):
    """POSIX rename() silently succeeds over an open file; Windows raises
    PermissionError instead (AV/indexer briefly holding models.json). This
    bounded retry is the only defence and was never executed by the suite."""
    target = tmp_path / "models.json"
    monkeypatch.setattr(atomic.time, "sleep", lambda seconds: None)
    calls = {"n": 0}
    real_replace = atomic.os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("WinError 32")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", flaky_replace)

    atomic.atomic_write_text(target, '{"a": 1}')

    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert calls["n"] == 3
    assert list(tmp_path.glob(".*tmp")) == []  # no temp left behind


def test_replace_temporary_reraises_after_exhausting_retries_and_leaves_no_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "models.json"
    monkeypatch.setattr(atomic.time, "sleep", lambda seconds: None)
    attempts = {"n": 0}

    def always_locked(src, dst):
        attempts["n"] += 1
        raise PermissionError("WinError 32")

    monkeypatch.setattr(atomic.os, "replace", always_locked)

    with pytest.raises(PermissionError):
        atomic.atomic_write_text(target, '{"a": 1}')

    assert attempts["n"] == 8  # atomic.py's bounded loop
    assert not target.exists()  # destination untouched
    assert list(tmp_path.iterdir()) == []  # the finally: unlink ran


def test_atomic_write_bytes_cleans_up_when_replace_never_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(atomic.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        atomic.atomic_write_bytes(tmp_path / "blob.bin", b"\x00\x01")
    assert list(tmp_path.iterdir()) == []


def test_locked_raises_filelock_timeout_when_the_lock_is_already_held(tmp_path):
    """Callers such as usage.py and error_report.py catch
    `filelock.Timeout` (imported as FileLockTimeout); pin that this is what
    atomic.locked actually raises when it cannot acquire in time."""
    path = tmp_path / "state.json"
    with atomic.locked(path, timeout=10):
        with pytest.raises(filelock.Timeout):
            with atomic.locked(path, timeout=0):
                pass

    # The lock is released again afterwards.
    with atomic.locked(path, timeout=1):
        pass


def test_locked_keeps_its_lock_file_outside_the_protected_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    with atomic.locked(path, timeout=5):
        assert path.read_text(encoding="utf-8") == "{}"
        assert (tmp_path / "state.json.lock").exists()
