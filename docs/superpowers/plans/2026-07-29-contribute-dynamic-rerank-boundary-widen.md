# Contribute dynamic re-rank + boundary widening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `omm contribute` (1) re-rank its remaining candidate queue immediately after every local-calibration update instead of only at session start, and (2) once the fixed candidate pool is exhausted, probe sibling quantization files of the two fit/unfit-boundary repos to narrow in on this machine's actual ceiling.

**Architecture:** Both changes live in `ContributionQueue` (`src/omm/contribute.py`), which stays free of network/console dependencies per its existing design — network access (fetching a repo's other GGUF files, their sizes) is injected as a `fetch_siblings` callback, mirroring the existing `refetch` callback pattern. The callback itself (`_fetch_sibling_candidates`) lives in `cli.py` next to the other network-touching contribute helpers and is wired into `_run_contribution_loop` → `queue.next_candidate()`.

**Tech Stack:** Python, pytest, existing `omm.hub` / `omm.providers.*` / `omm.featurize` modules (HuggingFace + ModelScope REST APIs, no new dependencies).

## Global Constraints

- Design doc: `docs/superpowers/specs/2026-07-29-contribute-dynamic-rerank-boundary-widen-design.md` — every requirement below traces back to it.
- No new CLI flags; both behaviors are always-on for `omm contribute`.
- No new repos introduced beyond the existing ~30-candidate pool — Phase C only fetches sibling files of repos already in that pool.
- `contribute.py` must stay pure/unit-testable — no direct `requests` calls or console output added to that file. All network access goes through injected callables.
- Reuse existing helpers rather than re-implementing: `predictor.rank_candidates`, `featurize.parse_quant_bits`, `featurize.is_mmproj_filename`, `hub.remote_file_size`, provider `fetch_repo_files`.
- Run `pytest` after every task; the suite must stay green throughout (regressions block moving to the next task).

**Note on one deviation from the design doc's literal wording:** the design doc describes the below-boundary repo as coming from Phase B's `_below_pool`. While implementing Task 3, tracing actual execution showed `_below_pool` can never yield anything once Task 2's "re-rank on every `mark_seen`" is in place (and, on inspection, even without it — Phase A already exhausts every viable candidate and marks it seen before Phase B ever runs, so `_below_pool`, which contains the same viable candidates, is always already-seen by the time Phase B starts). Task 3 instead tracks the below-boundary as the **last candidate popped from Phase A** (Phase A is speed-sorted descending, so its last pop is exactly "weakest-still-viable" — the same thing the design doc intended, via a path that's actually reachable). The above-boundary tracking (first candidate drawn from `_above_pool`) is unaffected and implemented exactly as specified.

---

### Task 1: `hub.fetch_repo_files` provider-routing wrapper

**Files:**
- Modify: `src/omm/hub.py:106-111` (add wrapper next to `download_url`/`remote_file_size`)
- Test: `tests/test_hub_multi_provider.py`

**Interfaces:**
- Produces: `hub.fetch_repo_files(provider: str, repo_id: str) -> tuple[list[str], float | None]` — routes to `_PROVIDER_MODULES[provider].fetch_repo_files(repo_id)`. Used by Task 5's `cli._fetch_sibling_candidates`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hub_multi_provider.py`:

```python
def test_fetch_repo_files_routes_to_provider_module(monkeypatch):
    def fake(repo_id):
        return ["a.Q4_K_M.gguf", "a.Q8_0.gguf"], 7.0

    monkeypatch.setattr(huggingface, "fetch_repo_files", fake)

    files, param_count_b = hub.fetch_repo_files("huggingface", "org/repo")

    assert files == ["a.Q4_K_M.gguf", "a.Q8_0.gguf"]
    assert param_count_b == 7.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hub_multi_provider.py::test_fetch_repo_files_routes_to_provider_module -v`
Expected: FAIL with `AttributeError: module 'omm.hub' has no attribute 'fetch_repo_files'`

- [ ] **Step 3: Write minimal implementation**

In `src/omm/hub.py`, insert immediately after the `download_url` function (currently line 106-107, right before `remote_file_size`):

```python
def fetch_repo_files(provider: str, repo_id: str) -> tuple[list[str], float | None]:
    return _PROVIDER_MODULES[provider].fetch_repo_files(repo_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_hub_multi_provider.py::test_fetch_repo_files_routes_to_provider_module -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/hub.py tests/test_hub_multi_provider.py
git commit -m "feat: add hub.fetch_repo_files provider-routing wrapper"
```

---

### Task 2: Re-rank the remaining queue after every `mark_seen`

**Files:**
- Modify: `src/omm/contribute.py:96-97` (`ContributionQueue.mark_seen`)
- Test: `tests/test_contribute_selection.py`

**Interfaces:**
- Consumes: `predictor.rank_candidates(artifact, hw)` (unchanged, already used by `_rebuild`).
- Produces: `ContributionQueue.mark_seen(seen_ref: str)` now also re-ranks — no signature change, callers in `cli.py` are unaffected.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_contribute_selection.py`:

```python
def test_mark_seen_reranks_remaining_queue_from_current_rank_candidates(monkeypatch):
    a, b, c = _candidate("o", "a.gguf"), _candidate("o", "b.gguf"), _candidate("o", "c.gguf")
    call_state = {"recalibrated": False}

    def fake_rank(artifact, hw):
        if not call_state["recalibrated"]:
            return [(a, 50.0), (b, 30.0), (c, 10.0)]
        return [(a, 50.0), (c, 40.0), (b, 30.0)]  # recalibration promotes c above b

    monkeypatch.setattr(contribute.predictor, "rank_candidates", fake_rank)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    assert queue.next_candidate() is a

    call_state["recalibrated"] = True
    queue.mark_seen(contribute.ref(a))

    # Without re-ranking, the phase A queue built at construction time
    # would still serve b next (its position at construction). Re-ranking
    # must reflect c's promotion above b instead.
    assert queue.next_candidate() is c
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_contribute_selection.py::test_mark_seen_reranks_remaining_queue_from_current_rank_candidates -v`
Expected: FAIL —

```
E       AssertionError: assert {'repo_id': 'o', 'filename': 'b.gguf', ...} is {'repo_id': 'o', 'filename': 'c.gguf', ...}
```

(confirmed by running this exact test against the current code before the fix - it returns `b`, the position `phase_a_queue` fixed at construction, instead of `c`)

- [ ] **Step 3: Write minimal implementation**

In `src/omm/contribute.py`, change:

```python
    def mark_seen(self, seen_ref: str) -> None:
        self.history_refs.add(seen_ref)
```

to:

```python
    def mark_seen(self, seen_ref: str) -> None:
        self.history_refs.add(seen_ref)
        self._rebuild()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_contribute_selection.py::test_mark_seen_reranks_remaining_queue_from_current_rank_candidates -v`
Expected: PASS

- [ ] **Step 5: Run the full contribute test suite to check for regressions**

Run: `pytest tests/test_contribute_selection.py tests/test_contribute_loop.py tests/test_cli_contribute.py -v`
Expected: All PASS (existing tests don't call `mark_seen` mid-assertion in ways sensitive to `_rebuild`'s side effects — see Global Constraints note above for why this is safe).

- [ ] **Step 6: Commit**

```bash
git add src/omm/contribute.py tests/test_contribute_selection.py
git commit -m "feat: re-rank contribute queue after every mark_seen, not just refetch"
```

---

### Task 3: Track the fit/unfit boundary candidates

**Files:**
- Modify: `src/omm/contribute.py` (`ContributionQueue.__init__`, `_rebuild`, `next_candidate`)
- Test: `tests/test_contribute_selection.py`

**Interfaces:**
- Produces: `ContributionQueue._boundary_below: dict | None` — last candidate popped from Phase A (weakest-still-viable). `ContributionQueue._boundary_above: dict | None` — first candidate drawn from Phase B's `_above_pool` (least-bad-unviable), frozen after first set. Both consumed by Task 4.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_contribute_selection.py`:

```python
def test_boundary_below_tracks_last_phase_a_draw_and_boundary_above_freezes_on_first(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    c, d = _candidate("o", "c.gguf"), _candidate("o", "d.gguf")
    ranked = [(a, 40.0), (b, 20.0), (c, -1.0), (d, -5.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    queue.next_candidate()  # phase A: a
    queue.mark_seen(contribute.ref(a))
    queue.next_candidate()  # phase A: b (last/weakest phase-A draw)
    queue.mark_seen(contribute.ref(b))
    queue.next_candidate()  # phase B above: c (first unviable)
    queue.mark_seen(contribute.ref(c))
    queue.next_candidate()  # phase B above: d
    queue.mark_seen(contribute.ref(d))

    assert queue._boundary_below is b
    assert queue._boundary_above is c
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_contribute_selection.py::test_boundary_below_tracks_last_phase_a_draw_and_boundary_above_freezes_on_first -v`
Expected: FAIL with `AttributeError: 'ContributionQueue' object has no attribute '_boundary_below'`

- [ ] **Step 3: Write minimal implementation**

In `src/omm/contribute.py`, update `__init__` to initialize the new state before `_rebuild()` runs (so it survives every `_rebuild()` call, since `_rebuild` must NOT reset it):

```python
    def __init__(self, artifact: dict, hw: HardwareInfo, history_refs: set[str]) -> None:
        self.artifact = artifact
        self.hw = hw
        self.history_refs = set(history_refs)
        self._boundary_below: dict | None = None
        self._boundary_above: dict | None = None
        self._rebuild()
```

Update the Phase A loop in `next_candidate` to record the boundary on every successful pop:

```python
        while self._phase_a_queue:
            candidate = self._phase_a_queue.pop(0)
            if not matches_history(candidate, self.history_refs):
                self._boundary_below = candidate
                return candidate
```

Update the Phase B loop to record both sides (below overwrites every time; above only the first time):

```python
        for _ in range(2):  # try both sides at most once before giving up
            if self._next_side_is_below:
                candidate, self._below_cursor = _next_unseen(
                    self._below_pool, self.history_refs, self._below_cursor
                )
                if candidate is not None:
                    self._boundary_below = candidate
            else:
                candidate, self._above_cursor = _next_unseen(
                    self._above_pool, self.history_refs, self._above_cursor
                )
                if candidate is not None and self._boundary_above is None:
                    self._boundary_above = candidate
            self._next_side_is_below = not self._next_side_is_below
            if candidate is not None:
                return candidate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_contribute_selection.py::test_boundary_below_tracks_last_phase_a_draw_and_boundary_above_freezes_on_first -v`
Expected: PASS

- [ ] **Step 5: Run the full contribute test suite to check for regressions**

Run: `pytest tests/test_contribute_selection.py tests/test_contribute_loop.py tests/test_cli_contribute.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/contribute.py tests/test_contribute_selection.py
git commit -m "feat: track fit/unfit boundary candidates in ContributionQueue"
```

---

### Task 4: Phase C — sibling-quant widening in `ContributionQueue`

**Files:**
- Modify: `src/omm/contribute.py` (`ContributionQueue.__init__`, `next_candidate`, new `_next_phase_c_candidate`)
- Test: `tests/test_contribute_selection.py`

**Interfaces:**
- Consumes: `_boundary_below` / `_boundary_above` from Task 3.
- Produces: `ContributionQueue.next_candidate(refetch=None, fetch_siblings: Callable[[dict], list[dict]] | None = None)` — new optional `fetch_siblings` parameter. When Phase A, Phase B, and `refetch` are all exhausted, calls `fetch_siblings(boundary_candidate)` once per boundary side (below first, then above) and serves unseen results from it before finally returning `None`. `fetch_siblings` is expected to already exclude the boundary candidate's own filename and any mmproj files — `ContributionQueue` only filters by `history_refs`, matching how every other pool in this class works. Wired to the real implementation in Task 5.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contribute_selection.py`:

```python
def test_phase_c_yields_fetch_siblings_result_after_pools_exhausted(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    c = _candidate("o", "c.gguf")
    ranked = [(a, 40.0), (b, 20.0), (c, -1.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    for _ in range(3):
        candidate = queue.next_candidate()
        queue.mark_seen(contribute.ref(candidate))

    assert queue.next_candidate() is None  # phase A/B fully exhausted
    assert queue._boundary_below is b
    assert queue._boundary_above is c

    sibling = _candidate("o", "b-q8.gguf")
    fetched_for = []

    def fake_fetch_siblings(boundary):
        fetched_for.append(boundary)
        return [sibling] if boundary is b else []

    assert queue.next_candidate(fetch_siblings=fake_fetch_siblings) is sibling
    assert fetched_for == [b]  # below tried before above


def test_phase_c_falls_through_to_above_when_below_boundary_absent(monkeypatch):
    c = _candidate("o", "c.gguf")
    ranked = [(c, -1.0)]  # only ever unviable -> phase A empty, below boundary never set
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    assert queue.next_candidate() is c  # phase B above: only unviable candidate
    queue.mark_seen(contribute.ref(c))
    assert queue.next_candidate() is None
    assert queue._boundary_below is None
    assert queue._boundary_above is c

    sibling = _candidate("o", "c-q2.gguf")
    result = queue.next_candidate(
        fetch_siblings=lambda boundary: [sibling] if boundary is c else []
    )

    assert result is sibling


def test_phase_c_returns_none_and_does_not_call_fetch_siblings_when_not_provided(monkeypatch):
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: [])

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is None


def test_phase_c_does_not_refetch_siblings_twice_for_the_same_side(monkeypatch):
    a = _candidate("o", "a.gguf")
    ranked = [(a, 40.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    queue.next_candidate()
    queue.mark_seen(contribute.ref(a))
    assert queue.next_candidate() is None
    assert queue._boundary_below is a

    call_count = {"n": 0}

    def counting_fetch(boundary):
        call_count["n"] += 1
        return []

    assert queue.next_candidate(fetch_siblings=counting_fetch) is None
    assert queue.next_candidate(fetch_siblings=counting_fetch) is None
    assert call_count["n"] == 1  # below side fetched once and cached empty, not re-fetched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_contribute_selection.py -k phase_c -v`
Expected: FAIL — `next_candidate()` raises `TypeError: next_candidate() got an unexpected keyword argument 'fetch_siblings'`

- [ ] **Step 3: Write minimal implementation**

In `src/omm/contribute.py`, update `__init__` to add the Phase C state (alongside the boundary attrs from Task 3, before `_rebuild()`):

```python
    def __init__(self, artifact: dict, hw: HardwareInfo, history_refs: set[str]) -> None:
        self.artifact = artifact
        self.hw = hw
        self.history_refs = set(history_refs)
        self._boundary_below: dict | None = None
        self._boundary_above: dict | None = None
        self._phase_c_below_queue: list[dict] = []
        self._phase_c_above_queue: list[dict] = []
        self._phase_c_below_fetched = False
        self._phase_c_above_fetched = False
        self._rebuild()
```

Update `next_candidate`'s signature and its tail (after the existing `refetch` block) to fall through to Phase C:

```python
    def next_candidate(
        self,
        refetch: Callable[[], tuple[dict, bool]] | None = None,
        fetch_siblings: Callable[[dict], list[dict]] | None = None,
    ) -> dict | None:
        while self._phase_a_queue:
            candidate = self._phase_a_queue.pop(0)
            if not matches_history(candidate, self.history_refs):
                self._boundary_below = candidate
                return candidate

        for _ in range(2):  # try both sides at most once before giving up
            if self._next_side_is_below:
                candidate, self._below_cursor = _next_unseen(
                    self._below_pool, self.history_refs, self._below_cursor
                )
                if candidate is not None:
                    self._boundary_below = candidate
            else:
                candidate, self._above_cursor = _next_unseen(
                    self._above_pool, self.history_refs, self._above_cursor
                )
                if candidate is not None and self._boundary_above is None:
                    self._boundary_above = candidate
            self._next_side_is_below = not self._next_side_is_below
            if candidate is not None:
                return candidate

        if refetch is not None:
            new_artifact, changed = refetch()
            if changed:
                self.artifact = new_artifact
                self._rebuild()
                return self.next_candidate(refetch, fetch_siblings)

        return self._next_phase_c_candidate(fetch_siblings)

    def _next_phase_c_candidate(
        self, fetch_siblings: Callable[[dict], list[dict]] | None
    ) -> dict | None:
        if fetch_siblings is None:
            return None
        for boundary_attr, queue_attr, fetched_attr in (
            ("_boundary_below", "_phase_c_below_queue", "_phase_c_below_fetched"),
            ("_boundary_above", "_phase_c_above_queue", "_phase_c_above_fetched"),
        ):
            if not getattr(self, fetched_attr):
                setattr(self, fetched_attr, True)
                boundary = getattr(self, boundary_attr)
                if boundary is not None:
                    siblings = fetch_siblings(boundary)
                    setattr(
                        self,
                        queue_attr,
                        [c for c in siblings if not matches_history(c, self.history_refs)],
                    )
            queue = getattr(self, queue_attr)
            while queue:
                candidate = queue.pop(0)
                if not matches_history(candidate, self.history_refs):
                    return candidate
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_contribute_selection.py -k phase_c -v`
Expected: All PASS

- [ ] **Step 5: Run the full contribute test suite to check for regressions**

Run: `pytest tests/test_contribute_selection.py tests/test_contribute_loop.py tests/test_cli_contribute.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/contribute.py tests/test_contribute_selection.py
git commit -m "feat: add Phase C sibling-quant widening to ContributionQueue"
```

---

### Task 5: Wire the real sibling-fetch callback into `omm contribute`

**Files:**
- Modify: `src/omm/cli.py` (imports; new `_fetch_sibling_candidates`; `_run_contribution_loop` signature + call site; `contribute()` call site)
- Modify: `tests/test_contribute_loop.py` (`_FakeQueue.next_candidate` signature)
- Test: new `tests/test_cli_contribute_boundary_widen.py`; addition to `tests/test_cli_contribute.py`

**Interfaces:**
- Consumes: `hub.fetch_repo_files` (Task 1), `hub.remote_file_size` (existing), `featurize.parse_quant_bits` (existing), `featurize.is_mmproj_filename` (existing), `omm.hub.ModelResolutionError` (existing), `ContributionQueue.next_candidate(refetch, fetch_siblings)` (Task 4).
- Produces: `cli._fetch_sibling_candidates(boundary: dict) -> list[dict]`. `cli._run_contribution_loop(queue, stop_event, refetch, quality_pack=None, daemon_ref=None, fetch_siblings=None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_contribute_boundary_widen.py`:

```python
from omm import cli
from omm.hub import ModelResolutionError


def _candidate(repo_id="org/repo", filename="model-Q4_K_M.gguf", provider="huggingface"):
    return {"repo_id": repo_id, "filename": filename, "name": "model", "provider": provider}


def test_returns_unseen_siblings_sorted_by_quant_distance(monkeypatch):
    boundary = _candidate(filename="model-Q4_K_M.gguf")
    monkeypatch.setattr(
        cli,
        "fetch_repo_files",
        lambda provider, repo_id: (
            ["model-Q4_K_M.gguf", "model-Q2_K.gguf", "model-Q8_0.gguf", "model-Q5_K_M.gguf"],
            7.0,
        ),
    )
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: 1234)

    result = cli._fetch_sibling_candidates(boundary)

    # Q4=4 bits (already tried, excluded); Q5=5 (dist 1) < Q2=2 (dist 2) < Q8=8 (dist 4).
    assert [c["filename"] for c in result] == [
        "model-Q5_K_M.gguf",
        "model-Q2_K.gguf",
        "model-Q8_0.gguf",
    ]
    assert all(c["size_bytes"] == 1234 for c in result)
    assert all(c["repo_id"] == "org/repo" for c in result)
    assert all(c["provider"] == "huggingface" for c in result)


def test_excludes_mmproj_files(monkeypatch):
    boundary = _candidate(filename="model-Q4_K_M.gguf")
    monkeypatch.setattr(
        cli,
        "fetch_repo_files",
        lambda provider, repo_id: (
            ["model-Q4_K_M.gguf", "mmproj-model-f16.gguf", "model-Q5_K_M.gguf"],
            7.0,
        ),
    )
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)

    result = cli._fetch_sibling_candidates(boundary)

    assert [c["filename"] for c in result] == ["model-Q5_K_M.gguf"]


def test_returns_empty_list_when_repo_lookup_fails(monkeypatch):
    boundary = _candidate()

    def raise_error(provider, repo_id):
        raise ModelResolutionError("not found")

    monkeypatch.setattr(cli, "fetch_repo_files", raise_error)

    assert cli._fetch_sibling_candidates(boundary) == []


def test_returns_empty_list_when_tried_filename_has_no_parseable_quant(monkeypatch):
    boundary = _candidate(filename="model-unknownquant.gguf")

    def fail_if_called(*a):
        raise AssertionError("fetch_repo_files should not be called")

    monkeypatch.setattr(cli, "fetch_repo_files", fail_if_called)

    assert cli._fetch_sibling_candidates(boundary) == []
```

Add to `tests/test_cli_contribute.py` (same setup pattern as `test_contribute_loads_quality_pack_and_passes_it_to_loop`):

```python
def test_contribute_passes_fetch_sibling_candidates_to_loop(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 0)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    fake_pack = {"pack_id": "pack-1", "pack_version": "1.1.0", "items": []}
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: (fake_pack, "sha"))

    captured = {}

    def fake_loop(queue, stop_event, refetch, quality_pack=None, daemon_ref=None, fetch_siblings=None):
        captured["fetch_siblings"] = fetch_siblings
        return cli._ContributionStats(benchmarked=[])

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert captured["fetch_siblings"] is cli._fetch_sibling_candidates
```

(Reuses the module's existing `_FakeListener`, `config`, and `runner` fixtures/imports already present at the top of `tests/test_cli_contribute.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_contribute_boundary_widen.py tests/test_cli_contribute.py::test_contribute_passes_fetch_sibling_candidates_to_loop -v`
Expected: FAIL — `AttributeError: module 'omm.cli' has no attribute 'fetch_repo_files'` (and `_fetch_sibling_candidates` missing)

- [ ] **Step 3: Write minimal implementation**

In `src/omm/cli.py`, update the `omm.featurize` import block (lines 58-64) to add `is_mmproj_filename`:

```python
from omm.featurize import (
    candidate_active_parameter_count_billions,
    candidate_parameter_count_billions,
    candidate_quant_bits,
    is_mmproj_filename,
    parse_param_count_billions,
    parse_quant_bits,
)
```

Update the `omm.hub` import block (lines 65-78) to add `fetch_repo_files`:

```python
from omm.hub import (
    AmbiguousModelError,
    AmbiguousProviderError,
    ModelResolutionError,
    QuantVariant,
    ResolvedModel,
    best_filenames_by_tier,
    download_url,
    fetch_repo_files,
    fetch_repo_param_count_b,
    rank_quant_variants,
    remote_file_size,
    remote_file_sha256,
    resolve_model,
)
```

Add `_fetch_sibling_candidates` right after `_maybe_auto_calibrate` (which ends at line 1254) and before `_install_impl` (line 1257):

```python
def _fetch_sibling_candidates(boundary: dict) -> list[dict]:
    """Phase C helper for `omm contribute`: given the candidate dict that
    was actually benchmarked at the fit/unfit boundary, look up every
    other GGUF quantization in the same repo and hand back the unseen
    ones closest to that quant level first, so the boundary search steps
    outward one quant at a time instead of jumping to an extreme.
    Best-effort - never raises, so it can't abort the contribution loop."""
    provider = boundary.get("provider") or "huggingface"
    repo_id = boundary["repo_id"]
    tried_bits = parse_quant_bits(boundary["filename"])
    if tried_bits is None:
        return []
    try:
        filenames, _ = fetch_repo_files(provider, repo_id)
    except ModelResolutionError:
        return []

    scored = []
    for filename in filenames:
        if filename == boundary["filename"] or is_mmproj_filename(filename):
            continue
        bits = parse_quant_bits(filename)
        if bits is None:
            continue
        scored.append((abs(bits - tried_bits), filename))
    scored.sort(key=lambda item: item[0])

    siblings = []
    for _, filename in scored:
        candidate = dict(boundary)
        candidate["provider"] = provider
        candidate["filename"] = filename
        candidate.pop("quant_bits", None)
        candidate["size_bytes"] = remote_file_size(provider, repo_id, filename)
        siblings.append(candidate)
    return siblings
```

Update `_run_contribution_loop`'s signature (currently `cli.py:3009-3015`) to add `fetch_siblings`:

```python
def _run_contribution_loop(
    queue,
    stop_event: threading.Event,
    refetch,
    quality_pack: dict | None = None,
    daemon_ref: dict | None = None,
    fetch_siblings=None,
) -> _ContributionStats:
```

Update its `next_candidate` call site (currently `cli.py:3042`):

```python
        candidate = queue.next_candidate(refetch=refetch, fetch_siblings=fetch_siblings)
```

Update `contribute()`'s call into `_run_contribution_loop` (currently `cli.py:3330-3332`):

```python
            stats = _run_contribution_loop(
                queue,
                listener.stop_event,
                refetch,
                quality_pack=quality_pack,
                daemon_ref=daemon_ref,
                fetch_siblings=_fetch_sibling_candidates,
            )
```

In `tests/test_contribute_loop.py`, update `_FakeQueue.next_candidate` (currently line 12) so the existing calls from `_run_contribution_loop` (which now always pass `fetch_siblings=...`) don't raise `TypeError`:

```python
    def next_candidate(self, refetch=None, fetch_siblings=None):
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_contribute_boundary_widen.py tests/test_cli_contribute.py -v`
Expected: All PASS

- [ ] **Step 5: Run the entire test suite**

Run: `pytest -q`
Expected: All PASS, no regressions anywhere (contribute, hub, install, search, etc.)

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_contribute_boundary_widen.py tests/test_cli_contribute.py tests/test_contribute_loop.py
git commit -m "feat: wire sibling-quant boundary widening into omm contribute"
```

---

## Post-implementation

Update the two module docstrings that describe the algorithm, since both changed meaningfully:

- `src/omm/contribute.py` module docstring (lines 1-15): add a short paragraph noting (a) `mark_seen` re-ranks before the next `next_candidate()` call, and (b) once Phase A/B/refetch are exhausted, `next_candidate(fetch_siblings=...)` probes sibling quant files of the last-viable and first-unviable repos before giving up.
- No changes needed to `docs/superpowers/specs/2026-07-29-contribute-dynamic-rerank-boundary-widen-design.md` beyond what's already there — the "Out of scope" and "Testing" sections already match what was built; the boundary-source deviation is called out in this plan's Global Constraints instead, since specs describe intent and this is an implementation-level correction that preserves that intent.
