# Compact Engine Listing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `omm info`, `omm scan`, and the install-time link step from printing one row/line per engine in `linker.ENGINES` regardless of whether that engine is even installed - show only what's actually installed, plus a one-line pointer to a wiki page for the rest.

**Architecture:** Add one small shared helper (`_missing_engines_note`) in `cli.py` that turns an `{engine_key: bool}` install map into an optional summary string. Reuse it from both `omm info` and `omm scan` after filtering their existing per-engine loops down to installed-only. Separately, delete one noisy `console.print` call inside `_link_model` (the function both `install` and `update` already funnel through). No new modules, no changes to `linker.py` or the registry schema.

**Tech Stack:** Python, Typer (CLI), Rich (`Table`/`Console`), pytest + `typer.testing.CliRunner`.

## Global Constraints

- Wiki URL is a fixed string: `https://github.com/omm-hippo/omm/wiki/Compatible-Programs`. Define it once as `cli.COMPATIBLE_PROGRAMS_URL`, never inline it.
- Summary line copy is exact: `f"+ {missing} program(s) not installed — see the compatibility list: {COMPATIBLE_PROGRAMS_URL}"` where `missing` is the count of engines with `installed[key] is False`.
- `omm info --json` output is unchanged - still emits all `linker.ENGINES` keys in `linked` regardless of install state. Do not filter it.
- Never touch the real `~/.omm` install or registry while testing - every test that reaches `registry.load_registry()` (directly or via the `scan`/`info` commands) must use the `isolated_omm_home` fixture from `tests/conftest.py`.
- Every test that reaches `linker.is_engine_installed` (directly or transitively) must monkeypatch it - do not rely on the real filesystem state of the machine running the tests.

---

### Task 1: `_missing_engines_note` helper + `COMPATIBLE_PROGRAMS_URL` constant

**Files:**
- Modify: `src/omm/cli.py:127` (add constant near `REPO_URL`)
- Modify: `src/omm/cli.py:256-257` (add helper function, right after `_reconcile_stale_link_records` and before `@app.command()\ndef scan()`)
- Test: `tests/test_missing_engines_note.py` (new file)

**Interfaces:**
- Consumes: `linker.ENGINES` (`list[EngineSpec]`, each with `.key: str` and `.label: str`) - already imported in `cli.py` as `linker`.
- Produces: `cli.COMPATIBLE_PROGRAMS_URL: str` and `cli._missing_engines_note(installed: dict[str, bool]) -> str | None`, both consumed by Task 2 and Task 3.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_missing_engines_note.py`:

```python
from omm import cli, linker


def test_missing_engines_note_is_none_when_all_installed():
    installed = {spec.key: True for spec in linker.ENGINES}

    assert cli._missing_engines_note(installed) is None


def test_missing_engines_note_counts_and_links_when_some_missing():
    installed = {spec.key: spec.key == "ollama" for spec in linker.ENGINES}
    missing_count = len(linker.ENGINES) - 1

    note = cli._missing_engines_note(installed)

    assert note is not None
    assert f"+ {missing_count} program(s) not installed" in note
    assert cli.COMPATIBLE_PROGRAMS_URL in note


def test_missing_engines_note_counts_all_when_none_installed():
    installed = {spec.key: False for spec in linker.ENGINES}

    note = cli._missing_engines_note(installed)

    assert note is not None
    assert f"+ {len(linker.ENGINES)} program(s) not installed" in note
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_missing_engines_note.py -v`
Expected: FAIL with `AttributeError: module 'omm.cli' has no attribute '_missing_engines_note'` (and no `COMPATIBLE_PROGRAMS_URL`).

- [ ] **Step 3: Add the constant**

In `src/omm/cli.py`, right after the existing line:

```python
REPO_URL = "git+https://github.com/omm-hippo/omm.git"
```

add:

```python
COMPATIBLE_PROGRAMS_URL = "https://github.com/omm-hippo/omm/wiki/Compatible-Programs"
```

- [ ] **Step 4: Add the helper function**

In `src/omm/cli.py`, right after `_reconcile_stale_link_records` (ends with `return cleaned`) and before `@app.command()\ndef scan()`, add:

```python
def _missing_engines_note(installed: dict[str, bool]) -> str | None:
    """One-line pointer to the compatibility wiki page for engines not
    installed on this machine - `None` when every known engine is
    installed, so info/scan tables don't print a useless zero-count line."""
    missing = sum(1 for is_installed in installed.values() if not is_installed)
    if missing == 0:
        return None
    return f"+ {missing} program(s) not installed — see the compatibility list: {COMPATIBLE_PROGRAMS_URL}"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_missing_engines_note.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_missing_engines_note.py
git commit -m "feat: add compatibility-note helper for engine listings"
```

---

### Task 2: `omm info` - installed engines only

**Files:**
- Modify: `src/omm/cli.py:1880-1889` (the `info` command's engine loop)
- Modify: `tests/test_cli_info.py` (update 2 existing tests, add 3 new tests)

**Interfaces:**
- Consumes: `cli._missing_engines_note` and `cli.COMPATIBLE_PROGRAMS_URL` from Task 1; `linker.is_engine_installed(key: str) -> bool` (existing).
- Produces: nothing new consumed elsewhere - `info`'s table output is a leaf.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli_info.py`, update the two tests that currently invoke `info` without mocking `is_engine_installed` (they'll otherwise depend on whatever engines happen to be installed on the machine running the tests):

Replace:

```python
def test_info_shows_name_version_size_and_links(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "model.gguf" in result.stdout
    assert "abc1234" in result.stdout
    assert "2.00 GB" in result.stdout
    assert "ollama run repo-q4" in result.stdout
    assert "LM Studio" in result.stdout
```

with:

```python
def test_info_shows_name_version_size_and_links(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key in ("ollama", "lmstudio"))
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "model.gguf" in result.stdout
    assert "abc1234" in result.stdout
    assert "2.00 GB" in result.stdout
    assert "ollama run repo-q4" in result.stdout
    assert "LM Studio" in result.stdout
```

Replace:

```python
def test_info_shows_not_linked_for_unlinked_engines(isolated_omm_home):
    entry = _entry(linked={"lmstudio": False, "ollama": False})
    registry.save_registry({"model.gguf": entry})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "not linked" in result.stdout
```

with:

```python
def test_info_shows_not_linked_for_unlinked_engines(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key in ("ollama", "lmstudio"))
    entry = _entry(linked={"lmstudio": False, "ollama": False})
    registry.save_registry({"model.gguf": entry})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "not linked" in result.stdout
```

Add to `tests/test_cli_info.py`:

```python
def test_info_hides_rows_for_uninstalled_engines(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "Ollama" in result.stdout
    assert "LM Studio" not in result.stdout
    assert "Jan" not in result.stdout


def test_info_notes_missing_engine_count_with_wiki_link(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    missing_count = len(linker.ENGINES) - 1
    assert f"+ {missing_count} program(s) not installed" in result.stdout
    assert cli.COMPATIBLE_PROGRAMS_URL in result.stdout


def test_info_omits_missing_note_when_all_engines_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: True)
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "not installed" not in result.stdout
```

Also add `is_engine_installed` mocks to the two remaining tests that don't currently need output assertions on engine rows, so they don't depend on the real machine's installed engines:

In `test_info_falls_back_to_sha256_prefix_when_version_missing`, change the signature to `(isolated_omm_home, monkeypatch)` and add `monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)` as the first line of the test body.

In `test_info_accepts_numeric_index_from_last_results` (already has `monkeypatch`), add `monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)` right after the existing `monkeypatch.setattr(cli.session_cache, ...)` line.

- [ ] **Step 2: Run tests to verify the new/changed ones fail**

Run: `pytest tests/test_cli_info.py -v`
Expected: `test_info_hides_rows_for_uninstalled_engines`, `test_info_notes_missing_engine_count_with_wiki_link`, `test_info_omits_missing_note_when_all_engines_installed` FAIL (LM Studio/Jan rows still present, no summary line exists yet). The two updated pre-existing tests should still PASS (mocking doesn't change their outcome yet since the code hasn't changed).

- [ ] **Step 3: Implement**

In `src/omm/cli.py`, replace the `info` command's engine loop:

```python
    for spec in linker.ENGINES:
        if spec.key == "ollama":
            table.add_row("Ollama", f"ollama run {ollama_tag}" if linked.get("ollama") else "not linked")
        else:
            table.add_row(
                spec.label,
                f"linked (visible in {spec.label})" if linked.get(spec.key) else "not linked",
            )

    console.print(table)
```

with:

```python
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}
    for spec in linker.ENGINES:
        if not installed[spec.key]:
            continue
        if spec.key == "ollama":
            table.add_row("Ollama", f"ollama run {ollama_tag}" if linked.get("ollama") else "not linked")
        else:
            table.add_row(
                spec.label,
                f"linked (visible in {spec.label})" if linked.get(spec.key) else "not linked",
            )

    console.print(table)
    note = _missing_engines_note(installed)
    if note:
        console.print(note)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_info.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_info.py
git commit -m "feat: omm info lists only installed engines"
```

---

### Task 3: `omm scan` - "Local AI runners" table, installed engines only

**Files:**
- Modify: `src/omm/cli.py:288-296` (the `scan` command's engine table loop)
- Modify: `tests/test_cli_memory_and_tune.py` (add 3 new tests)

**Interfaces:**
- Consumes: `cli._missing_engines_note` and `cli.COMPATIBLE_PROGRAMS_URL` from Task 1.
- Produces: nothing new consumed elsewhere.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_memory_and_tune.py`, after the existing `test_scan_leaves_link_record_untouched_when_engine_still_installed` test:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_memory_and_tune.py -v -k runner_table`
Expected: FAIL - `test_scan_runner_table_shows_only_installed_engines` fails because "LM Studio"/"not detected" still print today; the note-related tests fail because no summary line exists yet.

- [ ] **Step 3: Implement**

In `src/omm/cli.py`, replace the `scan` command's engine table block:

```python
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}

    engine_table = Table(title="Local AI runners", box=None)
    engine_table.add_column("Program", style="cyan")
    engine_table.add_column("Status", style="white")
    for spec in linker.ENGINES:
        engine_table.add_row(spec.label, "installed" if installed[spec.key] else "not detected")
    console.print()
    console.print(engine_table)
```

with:

```python
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}

    engine_table = Table(title="Local AI runners", box=None)
    engine_table.add_column("Program", style="cyan")
    engine_table.add_column("Status", style="white")
    for spec in linker.ENGINES:
        if installed[spec.key]:
            engine_table.add_row(spec.label, "installed")
    console.print()
    console.print(engine_table)
    note = _missing_engines_note(installed)
    if note:
        console.print(note)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_memory_and_tune.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_memory_and_tune.py
git commit -m "feat: omm scan runner table lists only installed engines"
```

---

### Task 4: `_link_model` - drop the per-skip noise line

**Files:**
- Modify: `src/omm/cli.py:1318-1320` (inside `_link_model`)
- Modify: `tests/test_install_impl.py` (add 1 new test)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new - `_link_model`'s return type (`dict[str, bool]`) is unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_install_impl.py`:

```python
def test_link_model_does_not_print_skip_notice_for_uninstalled_engines(
    isolated_omm_home, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    monkeypatch.setattr(
        cli.linker,
        "link_engine",
        lambda key, dest, *, repo_id, ollama_tag: None,
    )
    dest = tmp_path / "model.gguf"
    dest.write_bytes(b"x")

    linked = cli._link_model(dest, "org/repo", "model-tag")

    captured = capsys.readouterr()
    assert "not detected, skipping link" not in captured.out
    assert linked == {
        "ollama": True,
        "lmstudio": False,
        "jan": False,
        "anythingllm": False,
        "mstystudio": False,
        "textgenwebui": False,
        "koboldcpp": False,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_install_impl.py::test_link_model_does_not_print_skip_notice_for_uninstalled_engines -v`
Expected: FAIL - `"not detected, skipping link"` is found in `captured.out`.

- [ ] **Step 3: Implement**

In `src/omm/cli.py`, inside `_link_model`, replace:

```python
        if not linker.is_engine_installed(spec.key):
            console.print(f"[dim]{spec.label} not detected, skipping link.[/dim]")
            continue
```

with:

```python
        if not linker.is_engine_installed(spec.key):
            continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_install_impl.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py
git commit -m "fix: stop printing per-engine skip notice during install/update link step"
```

---

### Task 5: Compatible-Programs wiki page

This task has no code and no automated tests - it publishes a new page to the
project's GitHub Wiki (a separate git repository from the main `omm` repo:
`git@github.com:omm-hippo/omm.wiki.git`). Pushing to it is a shared, visible
action, so the final push step requires explicit user confirmation before
running - don't run Step 4 without it, even though the plan overall was
pre-approved.

**Files:**
- Create (in a scratch clone, not the main repo): `Compatible-Programs.md`

**Interfaces:**
- Consumes: `linker.ENGINES` list (`ollama`/`lmstudio`/`jan`/`anythingllm`/`mstystudio`/`textgenwebui`/`koboldcpp`) as the authoritative set of engines to document - keep this page in sync with that list by hand until/unless a future change automates it (out of scope here).
- Produces: the page at `https://github.com/omm-hippo/omm/wiki/Compatible-Programs`, which is exactly the URL `cli.COMPATIBLE_PROGRAMS_URL` (Task 1) points at.

- [ ] **Step 1: Clone the wiki repo into the scratchpad**

```bash
git clone git@github.com:omm-hippo/omm.wiki.git /private/tmp/claude-501/-Users-shinmingyu-Project-Localfit/a9042a6b-5dea-481c-b2a7-2da75972b1fd/scratchpad/omm-wiki
```

If this fails because the wiki has no pages yet (empty-repo clone error), the
wiki must be initialized once through the GitHub web UI first (Wiki tab ->
"Create the first page") before it has a git remote to clone - flag this to
the user rather than working around it, since it requires a web UI action
outside this session's tools.

- [ ] **Step 2: Write the page content**

Create `Compatible-Programs.md` in the cloned wiki repo with this content:

```markdown
# Compatible Programs

`omm` links installed models into any of these local AI runners it finds on
your machine. Install one (or more) of them, then run `omm scan` or `omm
link` - omm detects it automatically, no configuration needed.

| Program | Homepage |
|---|---|
| Ollama | https://ollama.com |
| LM Studio | https://lmstudio.ai |
| Jan | https://jan.ai |
| AnythingLLM | https://anythingllm.com |
| Msty | https://msty.app |
| text-generation-webui | https://github.com/oobabooga/text-generation-webui |
| KoboldCpp | https://github.com/LostRuins/koboldcpp |

Programs you don't have installed are hidden from `omm info` and `omm scan`
output to keep those tables short - this page is the full list.
```

- [ ] **Step 3: Verify locally**

Run: `cat /private/tmp/claude-501/-Users-shinmingyu-Project-Localfit/a9042a6b-5dea-481c-b2a7-2da75972b1fd/scratchpad/omm-wiki/Compatible-Programs.md`
Expected: the file contents above, no typos in the table.

- [ ] **Step 4: Confirm with the user, then commit and push**

Ask the user to confirm before running this - it publishes to the shared
GitHub org wiki:

```bash
cd /private/tmp/claude-501/-Users-shinmingyu-Project-Localfit/a9042a6b-5dea-481c-b2a7-2da75972b1fd/scratchpad/omm-wiki
git add Compatible-Programs.md
git commit -m "Add Compatible Programs page"
git push
```

- [ ] **Step 5: Verify the live page**

Fetch `https://github.com/omm-hippo/omm/wiki/Compatible-Programs` and confirm
it renders the table from Step 2.

---

## Final verification

- [ ] Run the full test suite: `pytest -q`
- [ ] Expected: all tests pass, no new failures introduced by Tasks 1-4.
