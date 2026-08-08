# omm benchmark all + 진행 피드백 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `omm benchmark all`이 Ollama에 설치된 모델 전부를 자동으로 벤치마크하게 하고, 벤치마크 진행 중 모델 단위 스피너 피드백을 보여준다.

**Architecture:** `quality.py`에 새 헬퍼 `list_benchmarkable_tags()`(clip 제외 `/api/tags` 조회)와 `collect_evidence()`의 옵션 콜백 `on_model_start`를 추가한다. `cli.py`의 `benchmark_cmd`는 단독 `all` 인자를 감지해 확장하고, 기존 `_run_pipx_install_with_progress`와 같은 Rich `Progress` 스타일로 콜백을 소비한다.

**Tech Stack:** Python, typer, rich.progress, pytest, requests (via 기존 `_request_json`).

## Global Constraints

- 스펙: `docs/superpowers/specs/2026-07-27-benchmark-all-and-progress-design.md`
- `all`은 **단독 인자일 때만** 키워드. 다른 태그와 섞이면 에러로 반려.
- `all` 확장 대상은 Ollama에 설치된 태그 전부, `details.family == "clip"` 제외.
- 진행 피드백은 모델 단위만 (quality pack 문항 단위는 스코프 밖).
- `--json` 여부와 무관하게 Progress는 항상 표시 (기존 다운로드/reinstall 관례와 동일).
- confirm-performance-timeout 재시도는 `on_model_start`를 다시 부르지 않는다 (모델당 1회).

---

### Task 1: `quality.list_benchmarkable_tags()`

**Files:**
- Modify: `src/omm/quality.py` (새 함수, `_tag_matches` 아래 `_model_metadata` 위 근처에 추가)
- Test: `tests/test_quality.py`

**Interfaces:**
- Produces: `quality.list_benchmarkable_tags() -> list[str]` — Ollama에 설치된 태그 중 `details.family == "clip"`이 아닌 것들의 `name`, 오름차순 정렬. `/api/tags`에 `models` 키가 없거나 리스트가 아니면 빈 리스트 반환 (에러를 던지지 않음 — 호출부가 "설치된 모델 없음"으로 처리하도록).

- [ ] **Step 1: Write the failing test**

```python
def test_list_benchmarkable_tags_excludes_clip_and_sorts(monkeypatch):
    def fake_request(method, path, payload=None, timeout=10):
        assert path == "/api/tags"
        return {
            "models": [
                {"name": "zebra:latest", "details": {"family": "llama"}},
                {"name": "mmproj:latest", "details": {"family": "clip"}},
                {"name": "alpha:latest", "details": {"family": "llama"}},
            ]
        }

    monkeypatch.setattr(quality, "_request_json", fake_request)

    assert quality.list_benchmarkable_tags() == ["alpha:latest", "zebra:latest"]


def test_list_benchmarkable_tags_empty_when_nothing_installed(monkeypatch):
    monkeypatch.setattr(quality, "_request_json", lambda *a, **k: {"models": []})

    assert quality.list_benchmarkable_tags() == []


def test_list_benchmarkable_tags_empty_when_models_key_missing(monkeypatch):
    monkeypatch.setattr(quality, "_request_json", lambda *a, **k: {})

    assert quality.list_benchmarkable_tags() == []
```

Add these to `tests/test_quality.py`, near the other `_model_metadata`/`_request_json` tests (e.g. after `test_model_metadata_rejects_already_linked_clip_mmproj`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_quality.py -k list_benchmarkable_tags -v`
Expected: FAIL with `AttributeError: module 'omm.quality' has no attribute 'list_benchmarkable_tags'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/omm/quality.py`, right before `def _model_metadata(tag: str) -> dict:`:

```python
def list_benchmarkable_tags() -> list[str]:
    """All Ollama tags that could plausibly be benchmarked right now.

    Excludes mmproj/clip projector models (see _model_metadata) - they
    have no tokenizer of their own and would just fail every time.
    """
    tags = _request_json("GET", "/api/tags", timeout=10).get("models")
    if not isinstance(tags, list):
        return []
    names = []
    for item in tags:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        details = item.get("details")
        if not isinstance(name, str):
            continue
        if isinstance(details, dict) and details.get("family") == "clip":
            continue
        names.append(name)
    return sorted(names)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_quality.py -k list_benchmarkable_tags -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/quality.py tests/test_quality.py
git commit -m "feat: add quality.list_benchmarkable_tags for omm benchmark all"
```

---

### Task 2: `collect_evidence(..., on_model_start=...)` callback

**Files:**
- Modify: `src/omm/quality.py:813` (`collect_evidence` signature + loop body)
- Test: `tests/test_quality.py`

**Interfaces:**
- Consumes: nothing new from Task 1.
- Produces: `collect_evidence(tags, hardware, pack_path=None, speed_runs=3, *, confirm_performance_timeout=False, on_model_start: Callable[[str, int, int], None] | None = None) -> dict`. `on_model_start(tag, index, total)` is called exactly once per tag in `tags`, in order, `index` is 1-based, before that tag's `_evaluate_tag_once` runs. Never called again for the confirm-performance-timeout retry of the same tag.

- [ ] **Step 1: Write the failing test**

Add near the other `collect_evidence` tests in `tests/test_quality.py` (e.g. after the `test_collect_evidence_...` block around line 107):

```python
def test_collect_evidence_calls_on_model_start_once_per_tag_in_order(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")
    monkeypatch.setattr(
        quality,
        "evaluate_model",
        lambda tag, pack, speed_runs=3: {"tag": tag, "quality": {}, "speed": {}},
    )
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)
    calls = []

    quality.collect_evidence(
        ["model:one", "model:two"],
        _hardware(),
        on_model_start=lambda tag, index, total: calls.append((tag, index, total)),
    )

    assert calls == [("model:one", 1, 2), ("model:two", 2, 2)]
```

Check `_hardware()` already exists as a module-level helper in `tests/test_quality.py` (used by the existing `test_collect_evidence_...` test around line 107) — reuse it, don't redefine.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_quality.py -k on_model_start -v`
Expected: FAIL with `TypeError: collect_evidence() got an unexpected keyword argument 'on_model_start'`

- [ ] **Step 3: Write minimal implementation**

In `src/omm/quality.py`, add `from typing import Callable` to the existing imports near the top (alongside the other stdlib imports), then modify `collect_evidence`:

```python
def collect_evidence(
    tags: list[str],
    hardware: HardwareInfo,
    pack_path: Path | None = None,
    speed_runs: int = 3,
    *,
    confirm_performance_timeout: bool = False,
    on_model_start: Callable[[str, int, int], None] | None = None,
) -> dict:
    if not tags:
        raise QualityEvaluationError("at least one Ollama model tag is required")
    if len(tags) > 20:
        raise QualityEvaluationError("at most 20 Ollama models may be evaluated at once")
    if len(set(tags)) != len(tags) or any(not tag or len(tag) > 256 for tag in tags):
        raise QualityEvaluationError("model tags must be unique non-empty strings")
    pack, pack_sha256 = load_pack(pack_path)
    models = []
    total = len(tags)
    for index, tag in enumerate(tags, start=1):
        if on_model_start is not None:
            on_model_start(tag, index, total)
        entry = _evaluate_tag_once(tag, hardware, pack, speed_runs)
        if (
            confirm_performance_timeout
            and entry.get("outcome") == "transient_error"
            and entry.get("failure_reason") == FAILURE_REASON_GENERATION_TIMEOUT
        ):
            entry = _confirm_generation_timeout(tag, hardware, pack, speed_runs)
        models.append(entry)
    return {
```

(Only the signature and the `for tag in tags:` loop line change — everything else in the function body after `models = []` stays the same, just re-indent the existing loop content into the new `for index, tag in enumerate(tags, start=1):` loop.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_quality.py -k "on_model_start or list_benchmarkable_tags" -v`
Expected: PASS (4 tests). Then run the full file to check nothing else broke: `python -m pytest tests/test_quality.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/omm/quality.py tests/test_quality.py
git commit -m "feat: add on_model_start progress callback to collect_evidence"
```

---

### Task 3: `omm benchmark all` expansion in `cli.py`

**Files:**
- Modify: `src/omm/cli.py:2293-2308` (inside `benchmark_cmd`, right after the `models = [_resolve_benchmark_tag(m) for m in models]` line and the daemon-reachable block)
- Test: `tests/test_cli_benchmark.py`

**Interfaces:**
- Consumes: `quality_mod.list_benchmarkable_tags()` from Task 1 (module already imported as `quality_mod` in `cli.py`).
- Produces: `benchmark_cmd` now treats `models == ["all"]` as "expand to every installed tag"; rejects `"all"` mixed with other args; errors cleanly when nothing is installed.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_benchmark.py` (near the top-level tests, after `test_benchmark_uploads_when_confirmed` or similar):

```python
def test_benchmark_all_expands_to_every_installed_tag(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "list_benchmarkable_tags", lambda: ["a:latest", "b:latest"])
    seen_tags = []
    monkeypatch.setattr(
        cli.quality_mod,
        "collect_evidence",
        lambda tags, *a, **k: seen_tags.append(list(tags)) or _full_report(),
    )
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["benchmark", "all"])

    assert result.exit_code == 0, result.stdout
    assert seen_tags == [["a:latest", "b:latest"]]
    assert "Expanding 'all' to 2 model(s)" in result.stdout


def test_benchmark_all_errors_when_nothing_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.quality_mod, "list_benchmarkable_tags", lambda: [])

    result = runner.invoke(cli.app, ["benchmark", "all"])

    assert result.exit_code == 1
    assert "no models" in result.stdout.lower() or "no models" in result.output.lower()


def test_benchmark_all_mixed_with_other_tag_is_rejected(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    result = runner.invoke(cli.app, ["benchmark", "all", "other:latest"])

    assert result.exit_code == 1
    assert "must be the only argument" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_benchmark.py -k benchmark_all -v`
Expected: FAIL — `"all"` gets treated as a literal tag, so `list_benchmarkable_tags` is never called and `collect_evidence` receives `["all"]` / `["all", "other:latest"]` unchanged; the "Expanding" / "no models" / "must be the only argument" strings never appear.

- [ ] **Step 3: Write minimal implementation**

In `src/omm/cli.py`, inside `benchmark_cmd`, immediately after the existing `models = [_resolve_benchmark_tag(m) for m in models]` line and before the `started_daemon = None` / daemon-reachable block, insert the mixed-argument check; then after the daemon-reachable block (which guarantees the daemon is up or the process has already exited), insert the expansion:

```python
    models = [_resolve_benchmark_tag(m) for m in models]
    if "all" in models and models != ["all"]:
        err_console.print("[red]`all` must be the only argument.[/red]")
        raise typer.Exit(1)
    started_daemon = None
    if not benchmark.ollama_daemon_reachable():
        if _stdin_is_tty() and _ask_confirm(
            "Ollama isn't running. Start it now, benchmark, then stop it afterward?"
        ):
            started_daemon = benchmark.start_ollama_daemon()
            if started_daemon is None:
                err_console.print("[red]Couldn't start the Ollama daemon.[/red]")
                raise typer.Exit(1)
        else:
            err_console.print("[red]Ollama is not running at http://localhost:11434.[/red]")
            raise typer.Exit(1)
    if models == ["all"]:
        models = quality_mod.list_benchmarkable_tags()
        if not models:
            err_console.print("[red]No models are installed in Ollama to benchmark.[/red]")
            raise typer.Exit(1)
        console.print(f"[dim]Expanding 'all' to {len(models)} model(s): {', '.join(models)}[/dim]")
```

(This replaces the existing `models = [_resolve_benchmark_tag(m) for m in models]` line through the end of the existing daemon-reachable `if` block — i.e. everything currently at `src/omm/cli.py:2295-2307` — with the block above.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_benchmark.py -v`
Expected: PASS (all tests in the file, including the 3 new ones and the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_benchmark.py
git commit -m "feat: support omm benchmark all to expand to every installed model"
```

---

### Task 4: Rich Progress spinner during `benchmark_cmd`

**Files:**
- Modify: `src/omm/cli.py` (inside `benchmark_cmd`, the `quality_mod.collect_evidence(...)` call around line 2313)
- Test: `tests/test_cli_benchmark.py`

**Interfaces:**
- Consumes: `on_model_start` callback param from Task 2; `Progress`, `SpinnerColumn`, `TextColumn`, `TimeElapsedColumn` already imported in `cli.py` (see the `from rich.progress import (...)` block used by `_run_pipx_install_with_progress`, `src/omm/cli.py:22`) — check that `TimeElapsedColumn` is already in that import list; add it if missing.
- Produces: `benchmark_cmd` now shows a spinner with `"Benchmarking {tag} ({i}/{n})"` while `collect_evidence` runs, finishing at 100% before printing results.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_benchmark.py`:

```python
def test_benchmark_shows_progress_per_model(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)

    def fake_collect_evidence(tags, *a, on_model_start=None, **k):
        for index, tag in enumerate(tags, start=1):
            if on_model_start is not None:
                on_model_start(tag, index, len(tags))
        return _full_report()

    monkeypatch.setattr(cli.quality_mod, "collect_evidence", fake_collect_evidence)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert "Benchmarking small:latest (1/1)" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_benchmark.py -k progress_per_model -v`
Expected: FAIL — `collect_evidence` is currently called without `on_model_start`, so the fake never prints anything and `"Benchmarking small:latest (1/1)"` never appears in stdout.

- [ ] **Step 3: Write minimal implementation**

In `src/omm/cli.py`, check the `from rich.progress import (...)` block (`src/omm/cli.py:22`) already includes `TimeElapsedColumn`; if not, add it there (it's already used by `_run_pipx_install_with_progress`, so it should already be present — confirm before editing).

Replace the existing:

```python
            report = quality_mod.collect_evidence(
                models,
                scan_hardware(),
                pack_path=pack,
                speed_runs=speed_runs,
                confirm_performance_timeout=confirm_performance_timeout,
            )
```

with:

```python
            with Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description}[/cyan]"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task_id = progress.add_task(f"Benchmarking ({len(models)} model(s))...", total=len(models))

                def _on_model_start(tag: str, index: int, total: int) -> None:
                    progress.update(
                        task_id,
                        description=f"Benchmarking {tag} ({index}/{total})",
                        completed=index - 1,
                    )

                report = quality_mod.collect_evidence(
                    models,
                    scan_hardware(),
                    pack_path=pack,
                    speed_runs=speed_runs,
                    confirm_performance_timeout=confirm_performance_timeout,
                    on_model_start=_on_model_start,
                )
                progress.update(task_id, completed=len(models))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_benchmark.py -v`
Expected: PASS (all tests, including the new progress test)

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS, no regressions elsewhere (e.g. `tests/test_contribute.py`, which also calls `benchmark`-adjacent code paths, must still be green).

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_benchmark.py
git commit -m "feat: show per-model progress spinner during omm benchmark"
```

## Self-Review Notes

- Spec coverage: Task 1 = spec §1 helper, Task 3 = spec §2 cli expansion, Task 2+4 = spec §3-4 progress callback + Rich wrapper. All four spec sections have a task.
- `on_model_start` signature (`tag: str, index: int, total: int`) is identical across Task 2's production code, Task 2's test, and Task 4's `_on_model_start` closure — checked for consistency.
- No placeholders: every step has literal code, not a description.
