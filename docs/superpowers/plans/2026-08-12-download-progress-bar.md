# Homebrew-Style Download Progress Bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the default rich progress bar in `omm install` / `omm contribute` downloads with a single-line, Homebrew-flavored bar (`#`-filled, no brackets/percent, `ETA <time>`) whose width adapts to the terminal instead of being a fixed 40 chars.

**Architecture:** Two small custom `rich.progress.ProgressColumn` subclasses (`HashBarColumn`, `EtaColumn`) added to `src/omm/downloader.py`, then wired into the existing `_progress()` factory alongside a `SpinnerColumn` and the existing `DownloadColumn`/`TransferSpeedColumn`. `_progress()` is the single call site both install and contribute already share, so no other file changes.

**Tech Stack:** Python, `rich` (already a dependency), `pytest`.

## Global Constraints

- Single change point: `src/omm/downloader.py`'s `_progress()` (per spec, both install and contribute paths use it — no call-site changes elsewhere).
- Bar renders with plain `#` for filled and ` ` (space) for empty — no brackets, no inline percentage.
- Bar width is not fixed: it uses rich's ratio-based expansion (`Column(ratio=1)` + `Progress(expand=True)`), the same mechanism `BarColumn` itself relies on for full-width bars, clamped to `[10, 60]` characters so it neither collapses on a narrow terminal nor stretches absurdly wide on an ultra-wide one. No manual resize polling — rich recomputes column width on every render already.
- Trailing time column reads `ETA <time>` (English label), using `TimeRemainingColumn`'s existing compact `MM:SS` formatting (no leading hour digit under an hour).
- Everything stays on one line: `<spinner> <filename> <bar> <size> <speed> ETA <time>`.
- Out of scope: color/gradient bars, percentage display, per-thread parallel-download sub-bars, changes to what install/contribute print before/after the bar.

---

### Task 1: `HashBarColumn` — adaptive `#`-filled bar

**Files:**
- Modify: `src/omm/downloader.py` (imports at top, new class near `_progress()`)
- Test: `tests/test_downloader.py`

**Interfaces:**
- Produces: `HashBarColumn(min_width: int = 10, max_width: int = 60)`, a `rich.progress.ProgressColumn` subclass. `HashBarColumn().render(task)` returns a renderable (`_HashBar`) whose `__rich_console__` yields `#`-filled/space-filled `Segment`s sized to `max(min_width, min(max_width, options.max_width))`, proportional to `task.completed / task.total`. When `task.total` is falsy (unknown size), renders as all-empty (no `#`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_downloader.py` (near the top, after existing imports — check the file's current import block first and merge, don't duplicate):

```python
import io
from types import SimpleNamespace

from rich.console import Console

from omm.downloader import HashBarColumn


def _render_bar(completed, total, console_width, min_width=10, max_width=60):
    column = HashBarColumn(min_width=min_width, max_width=max_width)
    bar = column.render(SimpleNamespace(completed=completed, total=total))
    console = Console(file=io.StringIO(), width=console_width, color_system=None)
    options = console.options.update(max_width=console_width)
    segments = list(bar.__rich_console__(console, options))
    return "".join(segment.text for segment in segments)


def test_hash_bar_fills_proportionally_to_completed_ratio():
    text = _render_bar(completed=50, total=100, console_width=40)
    assert text == "#" * 20 + " " * 20


def test_hash_bar_uses_only_hash_and_space_no_brackets_or_percent():
    text = _render_bar(completed=30, total=100, console_width=40)
    assert set(text) <= {"#", " "}


def test_hash_bar_clamps_to_max_width_on_wide_terminal():
    text = _render_bar(completed=50, total=100, console_width=200)
    assert len(text) == 60
    assert text == "#" * 30 + " " * 30


def test_hash_bar_clamps_to_min_width_on_narrow_terminal():
    text = _render_bar(completed=50, total=100, console_width=5)
    assert len(text) == 10
    assert text == "#" * 5 + " " * 5


def test_hash_bar_fully_filled_at_completion():
    text = _render_bar(completed=100, total=100, console_width=40)
    assert text == "#" * 40


def test_hash_bar_renders_empty_when_total_unknown():
    text = _render_bar(completed=0, total=None, console_width=40)
    assert text == " " * 40
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_downloader.py -k test_hash_bar -v`
Expected: FAIL with `ImportError: cannot import name 'HashBarColumn' from 'omm.downloader'`

- [ ] **Step 3: Implement `HashBarColumn`**

In `src/omm/downloader.py`, update the `rich` import block (currently lines 24-32) to add `Segment`, `Column`, and the extra progress pieces:

```python
from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderResult
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.segment import Segment
from rich.style import StyleType
from rich.table import Column
from rich.text import Text
```

(`BarColumn` stops being used once Task 3 rewires `_progress()` — leave the import for now, Task 3 removes it.)

Add this above `_progress()` (currently `src/omm/downloader.py:84`):

```python
@dataclass
class _HashBar:
    """Homebrew-style '#'-filled bar (plain ASCII, not rich's default Unicode
    half-block chars) so it matches `curl -#`. Clamped to
    [min_width, max_width] so it neither collapses on a narrow terminal nor
    stretches absurdly wide on an ultra-wide one."""

    completed: float
    total: float | None
    min_width: int = 10
    max_width: int = 60
    complete_style: StyleType = "bar.complete"
    back_style: StyleType = "bar.back"

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(self.min_width, min(self.max_width, options.max_width))
        filled = min(width, round(width * self.completed / self.total)) if self.total else 0
        empty = width - filled
        if filled:
            yield Segment("#" * filled, console.get_style(self.complete_style))
        if empty:
            yield Segment(" " * empty, console.get_style(self.back_style))


class HashBarColumn(ProgressColumn):
    """Renders `_HashBar`, expanding to fill the space `Progress(expand=True)`
    hands its ratio-1 table column - same mechanism `BarColumn` itself uses
    for a full-width bar, just with '#' instead of Unicode blocks."""

    def __init__(self, min_width: int = 10, max_width: int = 60) -> None:
        self.min_width = min_width
        self.max_width = max_width
        super().__init__(table_column=Column(ratio=1))

    def render(self, task) -> _HashBar:
        return _HashBar(task.completed, task.total, self.min_width, self.max_width)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_downloader.py -k test_hash_bar -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/omm/downloader.py tests/test_downloader.py
git commit -m "feat: add Homebrew-style adaptive-width hash bar column"
```

---

### Task 2: `EtaColumn` — `ETA MM:SS` label

**Files:**
- Modify: `src/omm/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `EtaColumn()`, a `rich.progress.TimeRemainingColumn` subclass whose `render(task)` returns `Text("ETA <compact-time>")` (e.g. `"ETA 23:31"`, no hour digit when under an hour) when a duration is known, `Text("ETA --:--")` when the duration isn't known yet, and an empty `Text` when `task.total` is `None` (mirrors the base column's own empty-when-unknown-size behavior).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_downloader.py`:

```python
from omm.downloader import EtaColumn


def _fake_task(**overrides):
    fields = dict(finished=False, finished_time=None, time_remaining=None, total=100)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_eta_column_shows_eta_prefixed_compact_minutes_seconds():
    task = _fake_task(time_remaining=95)  # 1:35
    assert EtaColumn().render(task).plain == "ETA 01:35"


def test_eta_column_shows_placeholder_when_time_remaining_unknown():
    task = _fake_task(time_remaining=None, total=100)
    assert EtaColumn().render(task).plain == "ETA --:--"


def test_eta_column_shows_nothing_when_total_unknown():
    task = _fake_task(time_remaining=None, total=None)
    assert EtaColumn().render(task).plain == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_downloader.py -k test_eta_column -v`
Expected: FAIL with `ImportError: cannot import name 'EtaColumn' from 'omm.downloader'`

- [ ] **Step 3: Implement `EtaColumn`**

Add next to `HashBarColumn` in `src/omm/downloader.py`:

```python
class EtaColumn(TimeRemainingColumn):
    """`TimeRemainingColumn(compact=True)` prefixed with 'ETA ' so it reads
    clearly next to size/speed instead of a bare, easy-to-miss timestamp."""

    def __init__(self) -> None:
        super().__init__(compact=True)

    def render(self, task) -> Text:
        inner = super().render(task)
        if not inner.plain:
            return inner
        return Text(f"ETA {inner.plain}", style=inner.style)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_downloader.py -k test_eta_column -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/omm/downloader.py tests/test_downloader.py
git commit -m "feat: add ETA-prefixed compact time-remaining column"
```

---

### Task 3: Wire both columns into `_progress()`

**Files:**
- Modify: `src/omm/downloader.py:84-91` (the `_progress()` factory)
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: `HashBarColumn` (Task 1), `EtaColumn` (Task 2).
- Produces: `_progress()` returns a `Progress` whose single rendered task row is `<spinner> <filename> <hash-bar> <size> <speed> ETA <time>` on one line, used unchanged by both call sites (`src/omm/downloader.py:220-222` and `:328-329`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_downloader.py`:

```python
from omm.downloader import _progress


def test_progress_factory_renders_single_line_without_percent_or_legacy_bar_chars():
    progress = _progress()
    progress.console = Console(file=io.StringIO(), width=120, color_system=None)
    task_id = progress.add_task(
        "download", total=5_600_000_000, completed=700_000_000, filename="ornith-1.0-9b-Q4_K_M.gguf"
    )
    progress.console.print(progress.make_tasks_table(progress.tasks))
    output = progress.console.file.getvalue()

    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert "ornith-1.0-9b-Q4_K_M.gguf" in line
    assert "#" in line
    assert "%" not in line
    assert "[" not in line and "]" not in line
    assert not any(ch in line for ch in "━╸╺")
    assert task_id is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_downloader.py -k test_progress_factory -v`
Expected: FAIL — current `_progress()` still uses `BarColumn`/`TimeRemainingColumn`, so `%`/`━` show up and the assertions on `#`/no-percent fail.

- [ ] **Step 3: Rewrite `_progress()`**

Replace `src/omm/downloader.py:84-91`:

```python
def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.fields[filename]}", table_column=Column(no_wrap=True)),
        HashBarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        EtaColumn(),
        expand=True,
    )
```

Then remove the now-unused `BarColumn` import from the `rich.progress` import block added in Task 1.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_downloader.py -k "test_progress_factory or test_hash_bar or test_eta_column" -v`
Expected: 10 passed

- [ ] **Step 5: Run the full downloader test suite**

Run: `python -m pytest tests/test_downloader.py -v`
Expected: all tests pass (existing download-logic tests are untouched by this change, so this just confirms no regression)

- [ ] **Step 6: Commit**

```bash
git add src/omm/downloader.py tests/test_downloader.py
git commit -m "feat: wire Homebrew-style progress bar into install/contribute downloads"
```

---

### Task 4: Manual visual check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite once more**

Run: `python -m pytest -q`
Expected: no failures introduced by this change.

- [ ] **Step 2: Manually eyeball the bar in a real terminal**

Use the `run` skill (or, if unavailable, an isolated pipx/venv install per the project's existing "isolated pipx/git testing" recipe — never the user's real installed `omm`) to trigger an actual download (e.g. `omm install` against a small test model, or re-run an existing download-triggering test manually) and confirm in a real terminal that:
- the line reads `<spinner> <filename> <hash-bar> <size> <speed> ETA <time>` on one line
- resizing the terminal window mid-download changes the bar's width on the next refresh
- no `%`, `[`, `]`, or the old `━╸╺` characters appear

No code changes in this task — it's a confirmation step before considering the feature done.
