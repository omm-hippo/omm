# Auto-import Background Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `omm setting auto-import enable` start a background OS service that
automatically runs the existing import pipeline (scan → group-by-hash → adopt-into-hub →
link) whenever Ollama/LM Studio/etc. download a new model natively, so the user never has
to manually run `omm import` again.

**Architecture:** A new `watch.py` module watches every supported local-app model
directory with `watchdog`, debounces bursts of filesystem events, waits for each
candidate file to stop changing size, then calls the same `scan_import.find_external_models`
→ `group_by_hash` → `adopt_group` pipeline `omm import` already uses, skipping hashes
already in the hub. A new `watch_service.py` module registers/unregisters this as a
per-user background process via launchd (macOS), systemd --user (Linux), or Task
Scheduler (Windows). A new `notify.py` posts a desktop notification per model adopted.
Everything is wired together by a hidden `omm _auto-import-run` subcommand and the
`omm setting auto-import enable|disable|status` CLI surface. Off by default (opt-in).

**Tech Stack:** Python 3.10+, Typer, `watchdog` (new optional dep, FS event watching),
`plyer` (new optional dep, cross-platform desktop notifications), existing
`scan_import.py`/`linker.py`/`registry.py`.

**Spec:** `docs/superpowers/specs/2026-09-12-auto-import-watch-design.md`

## Global Constraints

- Default OFF (opt-in only) — never enabled by `omm setup` onboarding.
- `watchdog`/`plyer` are a new optional extra `watch` in `pyproject.toml`, never a
  hard runtime dependency — `omm help` must keep working with neither installed.
- Never import `watchdog` or `plyer` at module top-level anywhere reachable from
  `cli.py`'s own top-level imports — both must be imported lazily, inside the one
  function that needs them, exactly like `questionary`/`requests` elsewhere in this
  repo (see `cli.py`'s lazy-import convention) — this keeps `omm help` at ~140ms for
  everyone who never touches this feature.
- Reuse `scan_import.find_external_models`/`group_by_hash`/`adopt_group` as-is —
  no new scan/adopt logic, no new external-app directory discovery.
- All new hidden/background CLI entry points follow the existing `_bg-version-check`
  pattern: `@app.command(name="...", hidden=True)`, added to
  `_SKIP_AUTO_IMPORT_SUBCOMMANDS`, `_SKIP_UPDATE_CHECK_SUBCOMMANDS`, and
  `_SKIP_ONBOARDING_SUBCOMMANDS`.
- Local only — nothing in this feature uploads anything anywhere; the desktop
  notification and the background scan both stay on-machine.

---

### Task 1: `notify.py` — best-effort desktop notifications

**Files:**
- Create: `src/omm/notify.py`
- Modify: `pyproject.toml` (add `plyer` to a new `watch` extra and to `dev`)
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `notify.notify(title: str, body: str) -> None` — never raises, silently
  no-ops if `plyer` isn't installed or the OS notification call fails. Used by Task 3.

- [ ] **Step 1: Add the `watch` extra and add `plyer`/`watchdog` to `dev`**

Edit `pyproject.toml`'s `[project.optional-dependencies]` table:

```toml
dev = [
    "pytest>=8",
    "fastapi>=0.115",
    "httpx2>=2.12",
    "uvicorn>=0.30",
    "scikit-learn>=1.4",
    "watchdog>=4",
    "plyer>=2.1",
]
server = [
    "fastapi>=0.115",
    "pydantic>=2.9",
    "uvicorn>=0.30",
]
# NVIDIA VRAM detection (hardware.py._scan_nvidia_vram already lazy-imports
# pynvml behind a broad try/except, so it's safe to omit entirely on
# machines without an NVIDIA GPU - e.g. every Mac).
nvidia = ["nvidia-ml-py>=12"]
# Background auto-import (omm setting auto-import enable): watches local AI
# app directories and desktop-notifies on adopt. Optional - omm.watch/omm.notify
# lazy-import both, so a plain `pip install omm-model` never needs either.
watch = ["watchdog>=4", "plyer>=2.1"]
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_notify.py
import sys
import types

from omm import notify


def test_notify_calls_plyer_when_available(monkeypatch):
    calls = []
    fake_notification = types.SimpleNamespace(
        notify=lambda **kwargs: calls.append(kwargs)
    )
    fake_plyer = types.SimpleNamespace(notification=fake_notification)
    monkeypatch.setitem(sys.modules, "plyer", fake_plyer)
    monkeypatch.setitem(sys.modules, "plyer.notification", fake_notification)

    notify.notify("title", "body")

    assert calls == [
        {"title": "title", "message": "body", "app_name": "omm", "timeout": 8}
    ]


def test_notify_is_silent_when_plyer_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "plyer", None)

    notify.notify("title", "body")  # must not raise


def test_notify_is_silent_when_plyer_raises(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("no notification daemon")

    fake_notification = types.SimpleNamespace(notify=_boom)
    fake_plyer = types.SimpleNamespace(notification=fake_notification)
    monkeypatch.setitem(sys.modules, "plyer", fake_plyer)
    monkeypatch.setitem(sys.modules, "plyer.notification", fake_notification)

    notify.notify("title", "body")  # must not raise
```

- [ ] **Step 3: Run tests, confirm they fail with `ModuleNotFoundError: No module named 'omm.notify'`**

Run: `python -m pytest tests/test_notify.py -v`

- [ ] **Step 4: Implement `src/omm/notify.py`**

```python
"""Best-effort desktop notifications for background automation (see
watch.py). A missed notification is far less bad than the auto-import loop
dying because a notification backend glitched, so every failure here is
swallowed rather than raised."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def notify(title: str, body: str) -> None:
    try:
        from plyer import notification
    except ImportError:
        log.debug("plyer not installed; skipping desktop notification")
        return
    try:
        notification.notify(title=title, message=body, app_name="omm", timeout=8)
    except Exception:
        log.debug("desktop notification failed", exc_info=True)
```

- [ ] **Step 5: Run tests, confirm they pass**

Run: `python -m pytest tests/test_notify.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/omm/notify.py tests/test_notify.py
git commit -m "feat: add best-effort desktop notification helper"
```

---

### Task 2: `watch.py` — stability check + single scan/adopt pass

**Files:**
- Create: `src/omm/watch.py`
- Test: `tests/test_watch_run_once.py`

**Interfaces:**
- Consumes: `notify.notify(title, body)` from Task 1;
  `scan_import.find_external_models()`, `scan_import.group_by_hash(found)`,
  `scan_import.adopt_group(group)`, `scan_import.ModelGroup`, `scan_import.ExternalGguf`
  (all existing); `registry.load_registry()` (existing); `linker.LinkError` (existing).
- Produces: `watch.DEBOUNCE_SECONDS: float`, `watch.STABILITY_WAIT_SECONDS: float`,
  `watch._file_size(path: Path) -> int | None`, `watch._is_group_stable(group) -> bool`,
  `watch.run_once() -> list[scan_import.AdoptResult]`, `watch.watch_target_dirs() -> list[Path]`
  — all consumed by Task 3.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watch_run_once.py
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
```

- [ ] **Step 2: Run tests, confirm they fail with `ModuleNotFoundError: No module named 'omm.watch'`**

Run: `python -m pytest tests/test_watch_run_once.py -v`

- [ ] **Step 3: Implement `src/omm/watch.py` (this task's slice only)**

```python
"""Background auto-import: watches every supported local AI app's model
directory and, when a new file settles, runs the same scan -> group -> adopt
pipeline as `omm import` (see scan_import.py) without a prompt.

Adopting a file that is still being written by another process is unsafe -
scan_import.adopt_group re-hashes right before finalizing the move and
raises linker.LinkError if the content changed underneath it, but paying for
a full sha256 of a large in-progress download just to hit that guard is
wasteful. _is_group_stable is a cheap pre-filter: skip anything whose size
is still moving and pick it up again next pass once it has settled.

Design: docs/superpowers/specs/2026-09-12-auto-import-watch-design.md
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from omm import linker, notify, registry, scan_import

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 15.0
STABILITY_WAIT_SECONDS = 2.0


def watch_target_dirs() -> list[Path]:
    """Every real, existing local-app model directory scan_import.py already
    knows how to scan. A missing (uninstalled) app's directory is silently
    skipped - watchdog can't watch a directory that doesn't exist."""
    candidates = [
        linker.ollama_models_dir(),
        linker.lmstudio_models_dir(),
        linker.anythingllm_ollama_models_dir(),
        linker.mstystudio_models_dir(),
        linker.textgenwebui_models_dir(),
        linker.koboldcpp_models_dir(),
        linker.jan_models_dir(),
    ]
    return [path for path in candidates if path is not None and path.exists()]


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _is_group_stable(group: scan_import.ModelGroup) -> bool:
    sizes_before = [_file_size(loc.path) for loc in group.locations]
    if any(size is None for size in sizes_before):
        return False
    time.sleep(STABILITY_WAIT_SECONDS)
    sizes_after = [_file_size(loc.path) for loc in group.locations]
    return sizes_before == sizes_after


def run_once() -> list[scan_import.AdoptResult]:
    """One full scan -> group -> adopt pass. Skips any group whose hash is
    already in the hub and any group that hasn't settled yet. Returns the
    AdoptResult for every model newly adopted this pass (empty if nothing
    new)."""
    found = scan_import.find_external_models()
    groups = scan_import.group_by_hash(found)
    reg = registry.load_registry()
    known_hashes = {
        entry.get("sha256") for entry in reg.values() if isinstance(entry, dict)
    }

    results: list[scan_import.AdoptResult] = []
    for group in groups:
        if group.sha256 in known_hashes:
            continue
        if not _is_group_stable(group):
            continue
        try:
            result = scan_import.adopt_group(group)
        except (OSError, linker.LinkError) as error:
            log.info("Skipping %s this pass: %s", group.display_name, error)
            continue
        results.append(result)
        notify.notify(
            "omm: new model auto-imported",
            f"{result.filename} is now available to every installed local AI app.",
        )
    return results
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `python -m pytest tests/test_watch_run_once.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/omm/watch.py tests/test_watch_run_once.py
git commit -m "feat: add auto-import scan/stability/adopt pass (watch.py)"
```

---

### Task 3: `watch.py` — watchdog observer + debounce wiring

**Files:**
- Modify: `src/omm/watch.py` (append)
- Test: `tests/test_watch_debounce.py`

**Interfaces:**
- Consumes: `watch.DEBOUNCE_SECONDS`, `watch.run_once`, `watch.watch_target_dirs` from
  Task 2.
- Produces: `watch._build_debounced_handler(on_settle: Callable[[], object]) -> FileSystemEventHandler`,
  `watch.run_watch_loop() -> None` (blocks forever; consumed by Task 4's hidden command).

- [ ] **Step 1: Install the newly added dev dependencies**

Task 1 added `watchdog`/`plyer` to the `dev` extra in `pyproject.toml`, but the
current environment's venv was set up before that edit. Re-run:

Run: `python -m pip install -e ".[dev]"`

- [ ] **Step 2: Write the failing test**

```python
# tests/test_watch_debounce.py
import time

from omm import watch


def test_debounced_handler_fires_once_after_quiet_period(monkeypatch):
    monkeypatch.setattr(watch, "DEBOUNCE_SECONDS", 0.05)
    calls = []
    handler = watch._build_debounced_handler(lambda: calls.append(1))

    handler.on_any_event(None)
    handler.on_any_event(None)  # resets the timer - still only one eventual call

    time.sleep(0.2)
    assert calls == [1]


def test_debounced_handler_restarts_after_firing(monkeypatch):
    monkeypatch.setattr(watch, "DEBOUNCE_SECONDS", 0.05)
    calls = []
    handler = watch._build_debounced_handler(lambda: calls.append(1))

    handler.on_any_event(None)
    time.sleep(0.2)
    handler.on_any_event(None)
    time.sleep(0.2)

    assert calls == [1, 1]
```

This test requires the `watchdog` package (added to `dev` in Task 1's
`pyproject.toml` edit); it is not guarded with `pytest.importorskip` because
`pip install -e ".[dev]"` (the documented dev setup) always provides it.

- [ ] **Step 3: Run test, confirm it fails with `AttributeError: module 'omm.watch' has no attribute '_build_debounced_handler'`**

Run: `python -m pytest tests/test_watch_debounce.py -v`

- [ ] **Step 4: Append to `src/omm/watch.py`**

```python
import threading


def _build_debounced_handler(on_settle):
    """Return a watchdog FileSystemEventHandler that calls on_settle() once,
    DEBOUNCE_SECONDS after the last filesystem event it saw - so a
    multi-file download only triggers one scan pass, run after things have
    quieted down rather than mid-download."""
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def __init__(self) -> None:
            self._timer: threading.Timer | None = None
            self._lock = threading.Lock()

        def on_any_event(self, event) -> None:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(DEBOUNCE_SECONDS, on_settle)
                self._timer.daemon = True
                self._timer.start()

    return _Handler()


def run_watch_loop() -> None:
    """Entry point for `omm _auto-import-run`. Blocks forever; the OS
    service registered via `omm setting auto-import enable`
    (see watch_service.py) is what starts/stops this process, not the
    user directly."""
    from watchdog.observers import Observer

    dirs = watch_target_dirs()
    if not dirs:
        log.info("No supported local AI app directories found; nothing to watch yet.")
    handler = _build_debounced_handler(run_once)
    observer = Observer()
    for directory in dirs:
        observer.schedule(handler, str(directory), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
```

Add `import threading` to the existing import block at the top of `watch.py`
(alongside `import logging`, `import time`).

- [ ] **Step 5: Run test, confirm it passes**

Run: `python -m pytest tests/test_watch_debounce.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/omm/watch.py tests/test_watch_debounce.py
git commit -m "feat: wire watchdog observer with debounce into watch.py"
```

---

### Task 4: hidden `omm _auto-import-run` subcommand

**Files:**
- Modify: `src/omm/cli.py`

**Interfaces:**
- Consumes: `watch.run_watch_loop()` from Task 3.

- [ ] **Step 1: Add `watch` to the alphabetical `from omm import (...)` block**

In `src/omm/cli.py`, in the `from omm import (...)` block (starts at line 42), add
`watch,` right after the existing `version_check,` line:

```python
    usage,
    version_check,
    watch,
)
```

- [ ] **Step 2: Add the hidden subcommand next to `_bg_version_check_cmd`**

In `src/omm/cli.py`, right after the existing `_bg_version_check_cmd` function
(around line 1343), add:

```python
@app.command(name="_auto-import-run", hidden=True)
def _auto_import_run_cmd() -> None:
    """Internal. Started by the OS service registered via
    `omm setting auto-import enable` (see watch_service.py); blocks forever
    watching every supported local AI app's model directory and adopting
    new models into the omm hub."""
    watch.run_watch_loop()
```

- [ ] **Step 3: Add `"_auto-import-run"` to the three skip sets**

In `src/omm/cli.py`:

```python
_SKIP_UPDATE_CHECK_SUBCOMMANDS = {"update", "doctor", "help", "_bg-version-check", "_auto-import-run"}
```

```python
_SKIP_ONBOARDING_SUBCOMMANDS = {"setup", "doctor", "help", "update", "_bg-version-check", "_auto-import-run"}
```

```python
_SKIP_AUTO_IMPORT_SUBCOMMANDS = {
    "update",
    "help",
    "import",
    "contribute",
    "doctor",
    "_bg-version-check",
    "_auto-import-run",
}
```

- [ ] **Step 4: Manually verify the command is wired (no automated test - it blocks forever)**

Run: `python -c "from omm import cli; print('_auto-import-run' in [c.name for c in cli.app.registered_commands])"`
Expected: `True`

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py
git commit -m "feat: add hidden _auto-import-run subcommand"
```

---

### Task 5: `watch_service.py` — per-OS background service registration

**Files:**
- Create: `src/omm/watch_service.py`
- Test: `tests/test_watch_service.py`

**Interfaces:**
- Produces: `watch_service.install() -> None`, `watch_service.uninstall() -> None`,
  `watch_service.is_installed() -> bool` — consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watch_service.py
import subprocess
import sys

import pytest

from omm import watch_service


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path)
    return tmp_path


def test_darwin_install_writes_plist_and_loads_it(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )

    watch_service.install()

    plist_path = watch_service._launchd_plist_path()
    assert plist_path.exists()
    content = plist_path.read_text(encoding="utf-8")
    assert "com.omm.autoimport" in content
    assert sys.executable in content
    assert "_auto-import-run" in content
    assert calls[0][0] == (["launchctl", "load", "-w", str(plist_path)],)


def test_darwin_uninstall_unloads_and_removes_plist(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(watch_service.subprocess, "run", lambda *a, **k: None)
    watch_service.install()
    assert watch_service._launchd_plist_path().exists()

    watch_service.uninstall()

    assert not watch_service._launchd_plist_path().exists()


def test_darwin_is_installed_reflects_plist_presence(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Darwin")
    assert watch_service.is_installed() is False
    monkeypatch.setattr(watch_service.subprocess, "run", lambda *a, **k: None)
    watch_service.install()
    assert watch_service.is_installed() is True


def test_linux_install_writes_unit_and_enables_it(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Linux")
    calls = []
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )

    watch_service.install()

    unit_path = watch_service._systemd_unit_path()
    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert sys.executable in content
    assert "_auto-import-run" in content
    commands = [c[0][0] for c in calls]
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert ["systemctl", "--user", "enable", "--now", "omm-auto-import.service"] in commands


def test_windows_install_calls_schtasks_create(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Windows")
    calls = []
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )

    watch_service.install()

    args = calls[0][0][0]
    assert args[0] == "schtasks"
    assert "/Create" in args
    assert "ommAutoImport" in args


def test_install_raises_on_unsupported_platform(fake_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Plan9")

    with pytest.raises(RuntimeError):
        watch_service.install()
```

- [ ] **Step 2: Run tests, confirm they fail with `ModuleNotFoundError: No module named 'omm.watch_service'`**

Run: `python -m pytest tests/test_watch_service.py -v`

- [ ] **Step 3: Implement `src/omm/watch_service.py`**

```python
"""Per-OS background-service registration for `omm _auto-import-run`
(see watch.py). install()/uninstall() only ever touch the one per-user
service definition this feature owns; nothing here needs admin/root."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from omm.config import OMM_HOME

_LAUNCHD_LABEL = "com.omm.autoimport"
_SYSTEMD_UNIT_NAME = "omm-auto-import.service"
_SCHTASKS_NAME = "ommAutoImport"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT_NAME


def _launchd_plist_content() -> str:
    log_path = OMM_HOME / "logs" / "auto-import.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>omm.cli</string>
        <string>_auto-import-run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def _systemd_unit_content() -> str:
    return f"""[Unit]
Description=omm auto-import watcher

[Service]
ExecStart={sys.executable} -m omm.cli _auto-import-run
Restart=on-failure

[Install]
WantedBy=default.target
"""


def install() -> None:
    system = platform.system()
    if system == "Darwin":
        plist_path = _launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        (OMM_HOME / "logs").mkdir(parents=True, exist_ok=True)
        plist_path.write_text(_launchd_plist_content(), encoding="utf-8")
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
    elif system == "Linux":
        unit_path = _systemd_unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(_systemd_unit_content(), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT_NAME], check=True
        )
    elif system == "Windows":
        command = f'"{sys.executable}" -m omm.cli _auto-import-run'
        subprocess.run(
            [
                "schtasks", "/Create", "/TN", _SCHTASKS_NAME, "/TR", command,
                "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
            ],
            check=True,
        )
    else:
        raise RuntimeError(f"auto-import is not supported on {system}")


def uninstall() -> None:
    system = platform.system()
    if system == "Darwin":
        plist_path = _launchd_plist_path()
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", "-w", str(plist_path)], check=False)
            plist_path.unlink()
    elif system == "Linux":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT_NAME], check=False
        )
        _systemd_unit_path().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    elif system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/TN", _SCHTASKS_NAME, "/F"], check=False)
    else:
        raise RuntimeError(f"auto-import is not supported on {system}")


def is_installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return _launchd_plist_path().exists()
    if system == "Linux":
        return _systemd_unit_path().exists()
    if system == "Windows":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", _SCHTASKS_NAME], capture_output=True
        )
        return result.returncode == 0
    return False
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `python -m pytest tests/test_watch_service.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/omm/watch_service.py tests/test_watch_service.py
git commit -m "feat: add per-OS auto-import service registration"
```

---

### Task 6: `omm setting auto-import enable|disable|status` CLI surface

**Files:**
- Modify: `src/omm/config.py` (add `auto_import_enabled` to `DEFAULT_CONFIG`)
- Modify: `src/omm/cli.py` (new `watch_app` sub-Typer + 3 commands + main menu entry)
- Test: `tests/test_cli_auto_import.py`

**Interfaces:**
- Consumes: `watch_service.install/uninstall/is_installed` (Task 5),
  `config_mod.update_config`, `load_config` (existing).

- [ ] **Step 1: Add the config default**

In `src/omm/config.py`'s `DEFAULT_CONFIG` dict, add (near `external_scan_done`):

```python
    # Whether `omm setting auto-import enable` has registered the background
    # OS service (watch_service.py). Off by default - opt-in only, never
    # touched by onboarding. The source of truth for "is it actually
    # registered" is watch_service.is_installed(), not this flag; this flag
    # is only what `omm setting auto-import status` shows as "the user's
    # choice" versus the service being externally removed.
    "auto_import_enabled": False,
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_cli_auto_import.py
from typer.testing import CliRunner

from omm import cli, config, watch_service

runner = CliRunner()


def test_enable_reports_missing_dependency(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 1
    assert "omm-model[watch]" in result.stdout + result.stderr
    assert config.load_config()["auto_import_enabled"] is False


def test_enable_installs_service_and_sets_flag(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: True)
    installed = []
    monkeypatch.setattr(watch_service, "is_installed", lambda: False)
    monkeypatch.setattr(watch_service, "install", lambda: installed.append(True))

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 0, result.stdout
    assert installed == [True]
    assert config.load_config()["auto_import_enabled"] is True


def test_enable_is_idempotent_when_already_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: True)
    monkeypatch.setattr(watch_service, "is_installed", lambda: True)

    def _fail_install():
        raise AssertionError("install() should not be called when already installed")

    monkeypatch.setattr(watch_service, "install", _fail_install)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 0, result.stdout


def test_disable_uninstalls_service_and_clears_flag(isolated_omm_home, monkeypatch):
    config.update_config(auto_import_enabled=True)
    monkeypatch.setattr(watch_service, "is_installed", lambda: True)
    uninstalled = []
    monkeypatch.setattr(watch_service, "uninstall", lambda: uninstalled.append(True))

    result = runner.invoke(cli.app, ["setting", "auto-import", "disable"])

    assert result.exit_code == 0, result.stdout
    assert uninstalled == [True]
    assert config.load_config()["auto_import_enabled"] is False


def test_status_shows_enabled_and_registered(isolated_omm_home, monkeypatch):
    config.update_config(auto_import_enabled=True)
    monkeypatch.setattr(watch_service, "is_installed", lambda: True)

    result = runner.invoke(cli.app, ["setting", "auto-import", "status"])

    assert result.exit_code == 0, result.stdout
    assert "enabled" in result.stdout
```

- [ ] **Step 3: Run tests, confirm they fail (no such command `auto-import`)**

Run: `python -m pytest tests/test_cli_auto_import.py -v`

- [ ] **Step 4: Add `watch_service` to the `from omm import (...)` block**

In `src/omm/cli.py`, next to the `watch,` entry added in Task 4:

```python
    usage,
    version_check,
    watch,
    watch_service,
)
```

- [ ] **Step 5: Add the sub-Typer and commands**

In `src/omm/cli.py`, right after the existing `upload_app` block (around line 422,
just after `setting_app.add_typer(upload_app)`):

```python
watch_app = typer.Typer(
    name="auto-import",
    help="Automatically adopt models Ollama/LM Studio/etc. download natively into the omm hub in the background. Off by default. See PRIVACY.md.",
    rich_markup_mode=None,
)
setting_app.add_typer(watch_app)
```

Then, near the other `@setting_app.command` definitions (e.g. right before
`_upload_channel_menu`, around line 7100), add:

```python
def _watch_dependencies_available() -> bool:
    try:
        import watchdog  # noqa: F401
        import plyer  # noqa: F401
    except ImportError:
        return False
    return True


@watch_app.command(name="enable")
@global_flags
def auto_import_enable() -> None:
    """Turn on background auto-import: watches Ollama/LM Studio/etc. and
    adopts new models into the omm hub without a prompt."""
    if not _watch_dependencies_available():
        err_console.print(
            '[error]Missing dependency. Install with: pip install "omm-model[watch]"[/error]'
        )
        raise typer.Exit(1)
    if watch_service.is_installed():
        console.print("[muted]Auto-import is already enabled.[/muted]")
        return
    try:
        watch_service.install()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        err_console.print(f"[error]Could not enable auto-import: {error}[/error]")
        raise typer.Exit(1) from error
    config_mod.update_config(auto_import_enabled=True)
    console.print(
        "[success]Auto-import enabled - it will run in the background from now on.[/success]"
    )


@watch_app.command(name="disable")
@global_flags
def auto_import_disable() -> None:
    """Turn off background auto-import."""
    if not watch_service.is_installed():
        console.print("[muted]Auto-import is already disabled.[/muted]")
        config_mod.update_config(auto_import_enabled=False)
        return
    try:
        watch_service.uninstall()
    except (OSError, subprocess.CalledProcessError) as error:
        err_console.print(f"[error]Could not disable auto-import cleanly: {error}[/error]")
        raise typer.Exit(1) from error
    config_mod.update_config(auto_import_enabled=False)
    console.print("[success]Auto-import disabled.[/success]")


@watch_app.command(name="status")
@global_flags
def auto_import_status() -> None:
    """Show whether background auto-import is enabled and registered with the OS."""
    enabled = bool(load_config().get("auto_import_enabled"))
    installed = watch_service.is_installed()
    table = _table(title="Auto-import", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    table.add_row("Setting", "enabled" if enabled else "disabled (default)")
    table.add_row("OS service registered", "yes" if installed else "no")
    console.print(table)
```

`subprocess` is already imported at the top of `cli.py` (line 16), so no new
top-level import is needed for the `enable`/`disable` exception handlers.

- [ ] **Step 6: Add a discoverability entry to the `omm setting` interactive menu**

In `src/omm/cli.py`'s `setting_menu` function, add a choice before `"← Back"`
(around line 7280):

```python
                    questionary.Choice(
                        f"Auto-import (current: {'on' if current.get('auto_import_enabled') else 'off'})",
                        value="auto-import",
                    ),
```

And handle it in the `if/elif` chain right after the `"upload"` branch:

```python
            elif choice == "auto-import":
                action = _ask_select(
                    questionary.select(
                        f"Auto-import (current: {'on' if current.get('auto_import_enabled') else 'off'}):",
                        choices=[
                            questionary.Choice("Turn on", value="enable"),
                            questionary.Choice("Turn off", value="disable"),
                            questionary.Choice("Show status", value="status"),
                            questionary.Choice("← Back", value="back"),
                        ],
                    )
                )
                if action == "enable":
                    auto_import_enable()
                elif action == "disable":
                    auto_import_disable()
                elif action == "status":
                    auto_import_status()
```

- [ ] **Step 7: Run tests, confirm they pass**

Run: `python -m pytest tests/test_cli_auto_import.py -v`
Expected: 5 passed

- [ ] **Step 8: Run the full test suite to check for regressions**

Run: `python -m pytest -q`
Expected: all passing (or pre-existing unrelated skips only)

- [ ] **Step 9: Commit**

```bash
git add src/omm/config.py src/omm/cli.py tests/test_cli_auto_import.py
git commit -m "feat: add omm setting auto-import enable/disable/status"
```

---

### Task 7: docs — `PRIVACY.md` and `CLAUDE.md`

**Files:**
- Modify: `PRIVACY.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a line to `PRIVACY.md`'s "Local files (never uploaded)" section**

```markdown
- **Auto-import** (`omm setting auto-import enable`, off by default) watches local AI
  app directories on this machine and adopts new models into the omm hub in the
  background. It is a local filesystem automation only - nothing it does is uploaded
  or sent anywhere.
```

- [ ] **Step 2: Add an architecture note to `CLAUDE.md`**

In the `## Architecture (the parts that span files)` section of `CLAUDE.md`, add a
new paragraph after the existing "Run log" paragraph:

```markdown
**Auto-import.** `omm setting auto-import enable` (off by default) registers a
per-user background service (`watch_service.py` - launchd/systemd --user/Task
Scheduler) that runs `omm _auto-import-run`, a hidden subcommand blocking in
`watch.run_watch_loop()`. It watches every supported local AI app's model directory
(`watch.watch_target_dirs()`) with `watchdog`, debounces bursts of filesystem events,
waits for each candidate file's size to stop changing, then runs the same
`scan_import.find_external_models` -> `group_by_hash` -> `adopt_group` pipeline
`omm import` already uses (see Hub + link model above) and desktop-notifies via
`notify.py`. `watchdog`/`plyer` are the `watch` optional extra - never a runtime
dependency of a plain `omm` install.
```

- [ ] **Step 3: Commit**

```bash
git add PRIVACY.md CLAUDE.md
git commit -m "docs: document auto-import in PRIVACY.md and CLAUDE.md"
```

---

## Post-implementation verification (not automated, do after all tasks)

Per this repo's verification style, exercise the real thing on the real machine before
calling this done:

1. `pip install -e ".[watch]"` in a throwaway venv, `OMM_HOME=$(mktemp -d) omm setting auto-import enable` on macOS (the dev machine), confirm
   `~/Library/LaunchAgents/com.omm.autoimport.plist` exists and `launchctl list | grep com.omm.autoimport` shows it loaded.
2. Trigger a real `ollama pull <small-model>` or LM Studio download, confirm a desktop
   notification appears and `omm list` shows the model without running `omm import`.
3. `omm setting auto-import disable`, confirm the plist is gone and
   `launchctl list | grep com.omm.autoimport` shows nothing.
4. Verify LM Studio's actual download temp-file naming (open question from the spec) -
   watch its models directory with `fswatch` or `ls -la` while a download runs, confirm
   `_is_group_stable` doesn't get fooled by a temp-then-rename pattern into adopting the
   temp file instead of the final one (if it does, note the temp file's extension so
   `_is_group_stable`/`scan_import` can filter it out - this is real code, not a
   documented gap, so file a follow-up issue with the finding rather than leaving it
   unverified).
5. Linux/Windows service registration is unit-tested only in this plan (Task 5) - if
   either platform is available, repeat steps 1-3 there too before considering this
   fully done cross-platform.
