from pathlib import Path

import pytest

from omm import linker, notify, registry, scan_import, watch


def test_file_size_returns_none_for_missing_file(tmp_path):
    assert watch._file_size(tmp_path / "missing.gguf") is None


def test_is_group_stable_true_for_unchanged_file(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "STABILITY_WAIT_SECONDS", 0.01)
    path = tmp_path / "model.gguf"
    path.write_bytes(b"x" * 100)
    group = scan_import.ModelGroup(
        "hash", [scan_import.ExternalGguf("ollama", "model.gguf", path, 100, "hash")]
    )
    assert watch._is_group_stable(group) is True


def test_is_group_stable_false_when_size_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "STABILITY_WAIT_SECONDS", 0.01)
    path = tmp_path / "model.gguf"
    path.write_bytes(b"x" * 10)
    group = scan_import.ModelGroup(
        "hash", [scan_import.ExternalGguf("ollama", "model.gguf", path, 10, "hash")]
    )
    sizes = iter([10, 999])
    monkeypatch.setattr(watch, "_file_size", lambda p: next(sizes))
    assert watch._is_group_stable(group) is False


def test_watch_target_dirs_skips_missing_and_keeps_existing(tmp_path, monkeypatch):
    present = tmp_path / "ollama-models"
    present.mkdir()
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(linker, "ollama_models_dir", lambda: present)
    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: missing)
    monkeypatch.setattr(linker, "anythingllm_ollama_models_dir", lambda: missing)
    monkeypatch.setattr(linker, "mstystudio_models_dir", lambda: missing)
    monkeypatch.setattr(linker, "textgenwebui_models_dir", lambda: None)
    monkeypatch.setattr(linker, "koboldcpp_models_dir", lambda: None)
    monkeypatch.setattr(linker, "jan_models_dir", lambda: missing)

    assert watch.watch_target_dirs() == [present]


def test_run_once_adopts_new_stable_model_and_notifies(isolated_omm_home, tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "STABILITY_WAIT_SECONDS", 0.01)
    from omm.hashutil import sha256_file

    model_path = tmp_path / "external" / "model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model data")
    digest = sha256_file(model_path)
    monkeypatch.setattr(scan_import, "find_external_models", lambda: [
        scan_import.ExternalGguf(
            "lmstudio", "model.gguf", model_path, model_path.stat().st_size, digest
        )
    ])
    notified = []
    monkeypatch.setattr(notify, "notify", lambda title, body: notified.append((title, body)))

    results = watch.run_once()

    assert len(results) == 1
    assert results[0].filename == "model.gguf"
    assert len(notified) == 1
    assert "model.gguf" in notified[0][1]


def test_run_once_skips_group_already_in_registry(isolated_omm_home, tmp_path, monkeypatch):
    model_path = tmp_path / "external" / "model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model data")
    from omm.hashutil import sha256_file

    digest = sha256_file(model_path)
    reg = registry.load_registry()
    reg["model.gguf"] = {"sha256": digest, "size_bytes": model_path.stat().st_size, "linked": {}}
    registry.save_registry(reg)
    monkeypatch.setattr(scan_import, "find_external_models", lambda: [
        scan_import.ExternalGguf("lmstudio", "model.gguf", model_path, model_path.stat().st_size, digest)
    ])
    notified = []
    monkeypatch.setattr(notify, "notify", lambda title, body: notified.append((title, body)))

    results = watch.run_once()

    assert results == []
    assert notified == []


def test_run_once_skips_unstable_group(isolated_omm_home, tmp_path, monkeypatch):
    model_path = tmp_path / "external" / "model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model data")
    from omm.hashutil import sha256_file

    digest = sha256_file(model_path)
    monkeypatch.setattr(scan_import, "find_external_models", lambda: [
        scan_import.ExternalGguf("lmstudio", "model.gguf", model_path, model_path.stat().st_size, digest)
    ])
    monkeypatch.setattr(watch, "_is_group_stable", lambda group: False)
    notified = []
    monkeypatch.setattr(notify, "notify", lambda title, body: notified.append((title, body)))

    results = watch.run_once()

    assert results == []
    assert notified == []


def test_run_once_skips_group_that_fails_adopt(isolated_omm_home, tmp_path, monkeypatch):
    model_path = tmp_path / "external" / "model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model data")
    from omm.hashutil import sha256_file

    digest = sha256_file(model_path)
    monkeypatch.setattr(watch, "STABILITY_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(scan_import, "find_external_models", lambda: [
        scan_import.ExternalGguf("lmstudio", "model.gguf", model_path, model_path.stat().st_size, digest)
    ])

    def _boom(group):
        raise linker.LinkError("simulated race")

    monkeypatch.setattr(scan_import, "adopt_group", _boom)
    notified = []
    monkeypatch.setattr(notify, "notify", lambda title, body: notified.append((title, body)))

    results = watch.run_once()

    assert results == []
    assert notified == []


def test_run_watch_loop_wires_observer_to_every_dir_and_stops_cleanly(monkeypatch):
    """Integration check for the pieces run_once() and
    _build_debounced_handler() already test individually: run_watch_loop()
    must actually schedule the debounced handler on every watched
    directory, start the observer, wire run_once as the settle callback,
    and stop/join the observer on the way out rather than leaking the
    watchdog thread."""
    fake_dirs = [Path("/fake/ollama"), Path("/fake/lmstudio")]
    monkeypatch.setattr(watch, "watch_target_dirs", lambda: fake_dirs)

    on_settle_calls = []
    fake_handler = object()

    def fake_build_handler(on_settle):
        on_settle_calls.append(on_settle)
        return fake_handler

    monkeypatch.setattr(watch, "_build_debounced_handler", fake_build_handler)

    class FakeObserver:
        instances = []

        def __init__(self):
            self.scheduled = []
            self.started = False
            self.stopped = False
            self.joined = False
            FakeObserver.instances.append(self)

        def schedule(self, handler, path, recursive=True):
            self.scheduled.append((handler, path, recursive))

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def join(self):
            self.joined = True

    monkeypatch.setattr("watchdog.observers.Observer", FakeObserver)

    def fake_sleep(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(watch.time, "sleep", fake_sleep)

    watch.run_watch_loop()

    assert on_settle_calls == [watch.run_once]
    observer = FakeObserver.instances[0]
    assert observer.scheduled == [
        (fake_handler, str(fake_dirs[0]), True),
        (fake_handler, str(fake_dirs[1]), True),
    ]
    assert observer.started is True
    assert observer.stopped is True
    assert observer.joined is True
