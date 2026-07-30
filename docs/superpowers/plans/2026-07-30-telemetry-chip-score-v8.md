# Telemetry v8 (CPU score + GPU chip score) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop uploading the raw CPU model name in `omm` telemetry and start uploading a GPU generation score, by introducing `benchmark_version: 8`.

**Architecture:** `omm.featurize.parse_chip_score(text) -> (score, tier)` already exists and already works on both CPU and GPU name strings (it's a generic regex: model number + tier word). It is only ever called on CPU names today. The whole change is: (1) call it on `hardware.gpu_name` too, wherever it's already called on `hardware.cpu`, uploading only the two resulting numbers, never a name; (2) stop uploading `cpu_model`/raw string, uploading `cpu_score`/`cpu_tier` (from the same function) instead. Because dropping `cpu_model` breaks the v7 "direct metadata" contract, this ships as a new `benchmark_version: 8`, structurally identical to v7 everywhere except the CPU/GPU chip fields. v6 and v7 historical rows are untouched and keep parsing exactly as they do today.

**Tech Stack:** Python (omm CLI + FastAPI server + sklearn training script), Firebase Realtime Database security rules (JSON + a JS test harness run against the `firebase-tools` emulator).

## Global Constraints

- Never upload `cpu_model` or `gpu_name` (raw strings) under `benchmark_version: 8`. Only `cpu_score`/`cpu_tier`/`gpu_score`/`gpu_tier` (numbers) plus the already-non-identifying `cpu_arch`/`cpu_physical_cores`/`cpu_logical_cores`.
- v6 and v7 behavior must not change at all — every existing test for those versions must keep passing unmodified.
- `cpu_score`/`gpu_score` bounds: `[0, 99999]`. `cpu_tier`/`gpu_tier` bounds: `[0, 10]`.
- `gpu_score`/`gpu_tier` are optional on every event (a machine with no GPU sends neither) — their absence must never block the rest of a v8 event from being accepted.
- Design reference: `docs/superpowers/specs/2026-07-30-telemetry-chip-score-v8-design.md`. Tracking issue: https://github.com/minigu5/Omm/issues/12 (reference it in every commit and the eventual PR; close it once merged).
- Editing `database.rules.json` in the repo does **not** change what Firebase actually enforces — after merge, the file's contents must be pasted into the Firebase console's Realtime Database Rules editor and Published by a human. This plan cannot do that step; flag it clearly when Task 3 is done.

---

## Task 1: `parse_chip_score` already handles GPU names — pin it with a test

**Files:**
- Test: `tests/test_featurize.py` (create if it doesn't exist — check with `ls tests/test_featurize*.py` first; if one exists, add to it instead of creating a new file)

**Interfaces:**
- Consumes: `omm.featurize.parse_chip_score(text: str) -> tuple[float, float]` (already exists, `src/omm/featurize.py:239`).
- Produces: nothing new — this task only adds regression coverage before Task 2 relies on this behavior.

- [ ] **Step 1: Check whether a featurize test file already exists**

Run: `ls /Users/shinmingyu/Project/Localfit/tests/ | grep -i featurize`

If a file is listed, add the test from Step 3 to the end of it (matching its existing import style). If nothing is listed, create `tests/test_featurize.py` with the header from Step 2 first.

- [ ] **Step 2 (only if the file doesn't exist yet): file header**

```python
from __future__ import annotations

from omm.featurize import parse_chip_score
```

- [ ] **Step 3: Write the failing test**

```python
def test_parse_chip_score_handles_gpu_names_the_same_way_as_cpu_names():
    assert parse_chip_score("NVIDIA GeForce RTX 4090") == (4090.0, 0.0)
    assert parse_chip_score("NVIDIA GeForce RTX 3080 Ti") == (3080.0, 1.0)
    assert parse_chip_score("Apple M2 Max") == (2.0, 2.0)
    assert parse_chip_score("") == (0.0, 0.0)
```

- [ ] **Step 4: Run it to confirm it already passes (this is documenting existing behavior, not adding new behavior)**

Run: `python -m pytest tests/test_featurize.py -v -k parse_chip_score_handles_gpu`
Expected: PASS (no source change needed — `_CHIP_MODEL_RE` already matches `RTX\s?(\d{3,5})` and `M(\d+)`, `_TIER_WORDS` already has `ti` and `max`)

- [ ] **Step 5: Commit**

```bash
git add tests/test_featurize.py
git commit -m "test: pin that parse_chip_score already generalizes to GPU names

Ref #12"
```

---

## Task 2: `predictor.py` — compute real `gpu_score`/`gpu_tier` instead of hardcoded zero

**Files:**
- Modify: `src/omm/predictor.py:104-140` (function `build_prediction_features`)
- Modify: `tests/test_feature_parity.py:26-70` (fixtures `_hardware()` and `_row()`)

**Interfaces:**
- Consumes: `omm.featurize.parse_chip_score` (already imported in `predictor.py:22`).
- Produces: `build_prediction_features(hw, candidate, *, engine="ollama", runtime=None) -> list[float]` — signature unchanged, only the `gpu_score`/`gpu_tier` values passed to `build_features` change from literal `0.0` to computed values.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_feature_parity.py` (new test function, after `test_prediction_features_match_privacy_minimized_training_row`):

```python
def test_gpu_score_and_tier_are_derived_from_gpu_name_not_hardcoded_zero():
    hw = _hardware()  # gpu_name="NVIDIA RTX 4090" already set in this fixture
    candidate = {
        "name": "model-7B-Q4.gguf",
        "filename": "model-7B-Q4.gguf",
        "repo_id": "org/model-7B",
        "size_bytes": 4 * 1024**3,
    }
    features = build_prediction_features(hw, candidate, runtime=_runtime())
    gpu_score_index = FEATURE_ORDER.index("gpu_score")
    gpu_tier_index = FEATURE_ORDER.index("gpu_tier")
    assert features[gpu_score_index] == 4090.0
    assert features[gpu_tier_index] == 0.0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_feature_parity.py -v -k gpu_score_and_tier_are_derived`
Expected: FAIL — `assert 0.0 == 4090.0`

- [ ] **Step 3: Implement**

In `src/omm/predictor.py`, change:

```python
    cpu_score, cpu_tier = parse_chip_score(hw.cpu or "")
    return build_features(
        ram_gb=hw.ram_total_gb,
        vram_gb=hw.vram_total_gb if has_gpu else 0.0,
        unified_memory=hw.unified_memory,
        param_count_b=parameters,
        quant_bits=quant_bits,
        cpu_score=cpu_score,
        cpu_tier=cpu_tier,
        gpu_score=0.0,
        gpu_tier=0.0,
```

to:

```python
    cpu_score, cpu_tier = parse_chip_score(hw.cpu or "")
    gpu_score, gpu_tier = parse_chip_score(hw.gpu_name or "")
    return build_features(
        ram_gb=hw.ram_total_gb,
        vram_gb=hw.vram_total_gb if has_gpu else 0.0,
        unified_memory=hw.unified_memory,
        param_count_b=parameters,
        quant_bits=quant_bits,
        cpu_score=cpu_score,
        cpu_tier=cpu_tier,
        gpu_score=gpu_score,
        gpu_tier=gpu_tier,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_feature_parity.py -v`
Expected: PASS (all tests in the file, not just the new one — this file's whole job is checking predictor/trainer parity, so a regression anywhere else would show up here)

- [ ] **Step 5: Commit**

```bash
git add src/omm/predictor.py tests/test_feature_parity.py
git commit -m "feat: derive gpu_score/gpu_tier from hardware.gpu_name

Same parse_chip_score() call already used for cpu_score - it already
generalizes to GPU names, it just wasn't being called with one.

Ref #12"
```

---

## Task 3: `database.rules.json` — add the v8 validation branch

**Files:**
- Modify: `database.rules.json` (via the script below, not by hand — the `.validate` value is one ~5,500-character string and hand-editing it is how past incidents happened)

**Interfaces:**
- Produces: Firebase now accepts `benchmark_version: 8` events shaped like v7 but with `cpu_score`/`cpu_tier` (numbers) instead of `cpu_model`/`cpu_arch`-as-identity in the success branch's required-field list, plus new optional top-level `cpu_score`/`cpu_tier`/`gpu_score`/`gpu_tier` field validators.

- [ ] **Step 1: Run this transformation script** (already verified against the current file in the design session — it finds the v7 branch by its `benchmark_version == 7` marker, duplicates it with `== 7` -> `== 8` and `'cpu_model', 'cpu_arch',` -> `'cpu_score', 'cpu_tier', 'cpu_arch',`, and appends it as a third `||` branch):

```bash
python3 <<'EOF'
import json

path = "database.rules.json"
data = json.load(open(path))
event = data["rules"]["telemetry"]["$event"]
v = event[".validate"]

marker = "newData.child('benchmark_version').val() == 7"
idx = v.index(marker)
sep = " || ("
sep_idx = v.rfind(sep, 0, idx)
start = sep_idx + len(sep) - 1
depth = 0
end = None
for j in range(start, len(v)):
    if v[j] == "(":
        depth += 1
    elif v[j] == ")":
        depth -= 1
        if depth == 0:
            end = j
            break
assert end == len(v) - 1, "v7 branch is not the tail of the validate string - investigate before continuing"
v7_branch = v[start:end + 1]
assert v7_branch.count("== 7") == 1
assert v7_branch.count("'cpu_model', 'cpu_arch',") == 1

v8_branch = v7_branch.replace("== 7", "== 8").replace(
    "'cpu_model', 'cpu_arch',", "'cpu_score', 'cpu_tier', 'cpu_arch',"
)
new_validate = v + " || " + v8_branch
assert new_validate.count("(") == new_validate.count(")")
event[".validate"] = new_validate

# New optional top-level field validators, inserted right after cpu_logical_cores.
new_fields = {
    "cpu_score": {
        ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0 && newData.val() <= 99999)"
    },
    "cpu_tier": {
        ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0 && newData.val() <= 10)"
    },
    "gpu_score": {
        ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0 && newData.val() <= 99999)"
    },
    "gpu_tier": {
        ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0 && newData.val() <= 10)"
    },
}
rebuilt = {}
for key, value in event.items():
    rebuilt[key] = value
    if key == "cpu_logical_cores":
        rebuilt.update(new_fields)
data["rules"]["telemetry"]["$event"] = rebuilt

# Bump the benchmark_version bound from 7 to 8.
bv_validate = rebuilt["benchmark_version"][".validate"]
assert "newData.val() <= 7" in bv_validate
rebuilt["benchmark_version"][".validate"] = bv_validate.replace("newData.val() <= 7", "newData.val() <= 8")

with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("database.rules.json updated")
EOF
```

- [ ] **Step 2: Verify the file is still valid JSON and inspect the diff**

Run: `python3 -c "import json; json.load(open('database.rules.json')); print('valid JSON')"`
Run: `git diff database.rules.json`

Expected: valid JSON confirmed; diff shows the `.validate` string grew by one ` || (...)` branch, `benchmark_version`'s bound changed from 7 to 8, and four new field blocks (`cpu_score`, `cpu_tier`, `gpu_score`, `gpu_tier`) appear after `cpu_logical_cores`. Read through the new branch once by eye to confirm it reads `== 8` and `'cpu_score', 'cpu_tier', 'cpu_arch',` — don't just trust the script silently.

- [ ] **Step 3: Commit** (Task 4 will add the emulator test that actually exercises this — commit now so the rules change and its test are reviewable as separate, bisectable steps)

```bash
git add database.rules.json
git commit -m "feat: add benchmark_version 8 to Firebase rules

Structurally identical to v7 except the success branch now requires
cpu_score/cpu_tier (numbers) instead of cpu_model (raw string), and
cpu_score/cpu_tier/gpu_score/gpu_tier are new optional top-level fields.

Ref #12"
```

---

## Task 4: `scripts/test_firebase_rules.mjs` — v8 emulator test cases

**Files:**
- Modify: `scripts/test_firebase_rules.mjs` (append after the existing v7 block, which ends around the performance_unfit assertions — search for `v7PerformanceUnfit` to find the end of the v7 section)

**Interfaces:**
- Consumes: the `request(path, method, body)` helper already defined at the top of the file.
- Produces: nothing consumed by later tasks — this is a leaf verification task for Task 3's rules change.

- [ ] **Step 1: Add the v8 test cases** — append this block after the last `v7PerformanceUnfit`-related assertion in the file:

```js
// --- v8: cpu_score/cpu_tier replace cpu_model, gpu_score/gpu_tier added ----

const v8Success = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "small:latest",
  engine: "ollama",
  benchmark_version: 8,
  recorded_at: "2026-07-30T00:00:00+00:00",
  outcome: "success",
  tokens_per_sec: 20.5,
  parameter_count_b: 7,
  active_parameter_count_b: 7,
  quant_bits: 4,
  engine_version: "0.32.1",
  client_version: "0.1.70",
  runtime_profile: "explicit_ollama_options",
  context_length: 4096,
  gpu_offload_percent: 100,
  cpu_threads: 8,
  num_batch: 512,
  sample_count: 3,
  tokens_per_sec_min: 19.5,
  tokens_per_sec_max: 21.5,
  cpu_score: 7950,
  cpu_tier: 1,
  cpu_arch: "x86_64",
  cpu_physical_cores: 6,
  cpu_logical_cores: 12,
  gpu_score: 4090,
  gpu_tier: 0,
};
const v8SuccessCreated = await request("telemetry", "POST", v8Success);
assert.equal(v8SuccessCreated.ok, true, `valid v8 success event was rejected (${v8SuccessCreated.status})`);

const v8SuccessWithoutGpu = await request("telemetry", "POST", {
  ...v8Success,
  gpu_score: undefined,
  gpu_tier: undefined,
  vram_gb: undefined,
});
assert.equal(
  v8SuccessWithoutGpu.ok,
  true,
  "v8 success rejected an event with no GPU detected (gpu_score/gpu_tier/vram_gb all absent)",
);

const v8SuccessMissingCpuScore = await request("telemetry", "POST", {
  ...v8Success,
  cpu_score: undefined,
});
assert.equal(
  v8SuccessMissingCpuScore.ok,
  false,
  "v8 success accepted a missing cpu_score - it is required, unlike gpu_score",
);

const v8SuccessWithRawCpuModelRejected = await request("telemetry", "POST", {
  ...v8Success,
  cpu_model: "AMD Ryzen 5 5600X 6-Core Processor",
});
assert.equal(
  v8SuccessWithRawCpuModelRejected.ok,
  false,
  "v8 accepted a raw cpu_model field - v8 must never carry it, that's the whole point",
);

const v8CpuScoreOutOfRange = await request("telemetry", "POST", {
  ...v8Success,
  cpu_score: 100_000,
});
assert.equal(v8CpuScoreOutOfRange.ok, false, "v8 accepted a cpu_score above the 99999 bound");

const v8CpuTierOutOfRange = await request("telemetry", "POST", {
  ...v8Success,
  cpu_tier: 11,
});
assert.equal(v8CpuTierOutOfRange.ok, false, "v8 accepted a cpu_tier above the 10 bound");

const v8ModelUnfit = {
  ram_gb: 24,
  vram_gb: 6,
  unified_memory: false,
  model_installed: "too-big:latest",
  engine: "ollama",
  benchmark_version: 8,
  recorded_at: "2026-07-30T00:00:00+00:00",
  outcome: "model_unfit",
  failure_reason: "out_of_memory",
};
const v8ModelUnfitCreated = await request("telemetry", "POST", v8ModelUnfit);
assert.equal(v8ModelUnfitCreated.ok, true, `valid v8 model_unfit event was rejected (${v8ModelUnfitCreated.status})`);

const v8Transient = {
  ram_gb: 24,
  unified_memory: false,
  model_installed: "small:latest",
  engine: "ollama",
  benchmark_version: 8,
  recorded_at: "2026-07-30T00:00:00+00:00",
  outcome: "transient_error",
  failure_reason: "ollama_unavailable",
};
const v8TransientCreated = await request("telemetry", "POST", v8Transient);
assert.equal(v8TransientCreated.ok, true, `valid v8 transient_error event was rejected (${v8TransientCreated.status})`);
```

- [ ] **Step 2: Run the emulator test suite locally**

Run: `npx --yes firebase-tools@15.24.0 emulators:exec --only database --project demo-localfit "node scripts/test_firebase_rules.mjs"`

Requires Node and Java 21 (`java -version` to check; this is the same command CI runs in `.github/workflows/ci.yml`'s `firebase-rules` job). Expected: exits 0, no assertion errors printed.

- [ ] **Step 3: Commit**

```bash
git add scripts/test_firebase_rules.mjs
git commit -m "test: add v8 Firebase rules emulator cases

Ref #12"
```

---

## Task 5: `src/localfit_server/app.py` — accept the new fields

**Files:**
- Modify: `src/localfit_server/app.py:47-50`
- Test: `tests/test_localfit_server_app.py` (check with `ls` first; if it doesn't exist, check `tests/` for the actual name by running `grep -rl "BenchmarkEvent" tests/`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `BenchmarkEvent` now accepts `cpu_score: float | None`, `cpu_tier: float | None`, `gpu_score: float | None`, `gpu_tier: float | None` without raising "extra fields not permitted".

- [ ] **Step 1: Find the existing test file for this model**

Run: `grep -rl "BenchmarkEvent" /Users/shinmingyu/Project/Localfit/tests/`

Use whichever file that returns for Step 2 below (if none exists, create `tests/test_localfit_server_app.py` with `from localfit_server.app import BenchmarkEvent` at the top).

- [ ] **Step 2: Write the failing test** — add to that file:

```python
def test_benchmark_event_accepts_v8_chip_score_fields():
    event = BenchmarkEvent(
        ram_gb=16,
        unified_memory=False,
        model_installed="small:latest",
        engine="ollama",
        benchmark_version=8,
        recorded_at="2026-07-30T00:00:00+00:00",
        tokens_per_sec=20.0,
        cpu_score=7950.0,
        cpu_tier=1.0,
        gpu_score=4090.0,
        gpu_tier=0.0,
    )
    assert event.cpu_score == 7950.0
    assert event.gpu_tier == 0.0
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/test_localfit_server_app.py -v -k v8_chip_score`
Expected: FAIL with a pydantic `ValidationError` ("Extra inputs are not permitted" for `cpu_score`)

- [ ] **Step 4: Implement** — in `src/localfit_server/app.py`, change:

```python
    cpu_model: str | None = Field(default=None, min_length=1, max_length=256)
    cpu_arch: str | None = Field(default=None, min_length=1, max_length=64)
    cpu_physical_cores: int | None = Field(default=None, ge=1, le=1024)
    cpu_logical_cores: int | None = Field(default=None, ge=1, le=1024)
```

to:

```python
    cpu_model: str | None = Field(default=None, min_length=1, max_length=256)
    cpu_arch: str | None = Field(default=None, min_length=1, max_length=64)
    cpu_physical_cores: int | None = Field(default=None, ge=1, le=1024)
    cpu_logical_cores: int | None = Field(default=None, ge=1, le=1024)
    cpu_score: float | None = Field(default=None, ge=0, le=99_999)
    cpu_tier: float | None = Field(default=None, ge=0, le=10)
    gpu_score: float | None = Field(default=None, ge=0, le=99_999)
    gpu_tier: float | None = Field(default=None, ge=0, le=10)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_localfit_server_app.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/localfit_server/app.py tests/test_localfit_server_app.py
git commit -m "feat: accept v8 cpu_score/cpu_tier/gpu_score/gpu_tier fields

Ref #12"
```

---

## Task 6: `src/omm/cli.py` — stop sending raw CPU name, start sending GPU score

**Files:**
- Modify: `src/omm/cli.py:60-67` (add `parse_chip_score` import)
- Modify: `src/omm/cli.py:2656-2660` (console disclosure string)
- Modify: `src/omm/cli.py:2823-2848` (`_report_telemetry` success promotion)
- Modify: `src/omm/cli.py:2907-2909` (`_report_failure_telemetry`, cpu metadata attach)
- Modify: `src/omm/cli.py:2893` (`_report_failure_telemetry`, `benchmark_version` literal)
- Modify: `src/omm/cli.py:3010-3032` (`_complete_cpu_metadata`)
- Test: `tests/test_cli_benchmark.py`

**Interfaces:**
- Consumes: `omm.featurize.parse_chip_score`.
- Produces: `_complete_cpu_metadata(info: HardwareInfo) -> dict[str, str | int | float] | None` now returns `{"cpu_score":, "cpu_tier":, "cpu_arch":, "cpu_physical_cores":, "cpu_logical_cores":}` (no `cpu_model`). New `_complete_gpu_metadata(info: HardwareInfo) -> dict[str, float] | None` returns `{"gpu_score":, "gpu_tier":}` or `None` when `info.gpu_name` is falsy. Both `_report_telemetry` and `_report_failure_telemetry` now emit `benchmark_version: 8`.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_cli_benchmark.py`. These call `_report_telemetry`/`_report_failure_telemetry` directly (same pattern as the existing golden contract test `test_report_telemetry_v7_success_event_feeds_speed_regression_and_positive_fit_label` in `tests/test_training_data.py:854` — read it first) rather than driving the full `benchmark` CLI command, because the full-CLI fixture (`_full_report()` in this file) deliberately omits `runtime`/`environment.engine_version`, so it never reaches the v6/v7/v8 promotion branch at all; that's an existing, unrelated gap, not something to fix here.

```python
def test_report_telemetry_v8_success_sends_chip_scores_not_raw_names(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware_with_chip_metadata)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    cli._report_telemetry(
        "model-7B-Q4.gguf", "org/model", 42.5,
        size_bytes=4 * 1024**3, sample_count=3, speed_min=40.0, speed_max=45.0,
        model_metadata={"parameter_size": "7B", "quantization_level": "Q4_K_M"},
        runtime={
            "runtime_profile": "explicit_ollama_options", "context_length": 4096,
            "gpu_offload_percent": 100, "cpu_threads": 8, "num_batch": 512,
        },
        engine_version="0.32.1",
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["cpu_score"] == 5600.0
    assert event["cpu_tier"] == 0.0
    assert event["gpu_score"] == 4090.0
    assert event["gpu_tier"] == 0.0
    assert "cpu_model" not in event
    assert "gpu_name" not in event


def test_report_failure_telemetry_v8_sends_chip_scores_not_raw_names(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware_with_chip_metadata)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    cli._report_failure_telemetry(
        {"tag": "too-big:latest", "outcome": "model_unfit", "failure_reason": "out_of_memory"},
        {},
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["cpu_score"] == 5600.0
    assert event["gpu_score"] == 4090.0
    assert "cpu_model" not in event
```

Add the fixture these two tests share right after `_hardware()` at the top of the file:

```python
def _hardware_with_chip_metadata() -> HardwareInfo:
    return HardwareInfo(
        os_name="Linux",
        os_version="",
        cpu="AMD Ryzen 5 5600X",
        ram_total_gb=16,
        ram_available_gb=12,
        unified_memory=False,
        gpu_name="NVIDIA GeForce RTX 4090",
        vram_total_gb=24,
        vram_free_gb=20,
        cpu_arch="x86_64",
        cpu_physical_cores=6,
        cpu_logical_cores=12,
    )
```

`parse_chip_score("AMD Ryzen 5 5600X")` matches `Ryzen\s*\d\s*(\d{3,5})` -> `(5600.0, 0.0)` (no tier word in that string). `parse_chip_score("NVIDIA GeForce RTX 4090")` -> `(4090.0, 0.0)`.

- [ ] **Step 2: Run to verify both fail**

Run: `python -m pytest tests/test_cli_benchmark.py -v -k "v8_success_sends_chip_scores or v8_sends_chip_scores"`
Expected: FAIL (`assert 4 == 8` on `benchmark_version`, or `KeyError: 'cpu_score'`)

- [ ] **Step 3: Add the import** — in `src/omm/cli.py`, change:

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

to:

```python
from omm.featurize import (
    candidate_active_parameter_count_billions,
    candidate_parameter_count_billions,
    candidate_quant_bits,
    is_mmproj_filename,
    parse_chip_score,
    parse_param_count_billions,
    parse_quant_bits,
)
```

- [ ] **Step 4: Rewrite `_complete_cpu_metadata` and add `_complete_gpu_metadata`** — change:

```python
def _complete_cpu_metadata(info: HardwareInfo) -> dict[str, str | int] | None:
    """Return direct-metadata (v6/v7) CPU data only when it is useful for training."""
    model = getattr(info, "cpu", None)
    arch = getattr(info, "cpu_arch", None)
    physical = getattr(info, "cpu_physical_cores", None)
    logical = getattr(info, "cpu_logical_cores", None)
    if not isinstance(model, str) or not isinstance(arch, str):
        return None
    model, arch = model.strip(), arch.strip()
    if (
        not model or not arch or len(model) > 256 or len(arch) > 64
        or model.lower() == arch.lower()
        or not isinstance(physical, int) or not isinstance(logical, int)
        or not 1 <= physical <= logical <= 1024
    ):
        return None
    return {
        "cpu_model": model,
        "cpu_arch": arch,
        "cpu_physical_cores": physical,
        "cpu_logical_cores": logical,
    }
```

to:

```python
def _complete_cpu_metadata(info: HardwareInfo) -> dict[str, str | int | float] | None:
    """Return direct-metadata (v8) CPU data only when it is useful for
    training. Never includes the raw CPU model name - only a locally
    computed ordinal score/tier from the same parser GPU names use (see
    docs/telemetry-v8.md)."""
    model = getattr(info, "cpu", None)
    arch = getattr(info, "cpu_arch", None)
    physical = getattr(info, "cpu_physical_cores", None)
    logical = getattr(info, "cpu_logical_cores", None)
    if not isinstance(model, str) or not isinstance(arch, str):
        return None
    model, arch = model.strip(), arch.strip()
    if (
        not model or not arch or len(model) > 256 or len(arch) > 64
        or model.lower() == arch.lower()
        or not isinstance(physical, int) or not isinstance(logical, int)
        or not 1 <= physical <= logical <= 1024
    ):
        return None
    cpu_score, cpu_tier = parse_chip_score(model)
    return {
        "cpu_score": cpu_score,
        "cpu_tier": cpu_tier,
        "cpu_arch": arch,
        "cpu_physical_cores": physical,
        "cpu_logical_cores": logical,
    }


def _complete_gpu_metadata(info: HardwareInfo) -> dict[str, float] | None:
    """Return locally-computed v8 GPU chip score data, or None when no GPU
    was detected at all. Never includes the raw GPU name (see
    docs/telemetry-v8.md) - only the two numbers `parse_chip_score` derives
    from it."""
    name = getattr(info, "gpu_name", None)
    if not isinstance(name, str) or not name.strip():
        return None
    gpu_score, gpu_tier = parse_chip_score(name)
    return {"gpu_score": gpu_score, "gpu_tier": gpu_tier}
```

- [ ] **Step 5: Wire both into `_report_telemetry`** — change:

```python
    complete_runtime = _complete_runtime(runtime)
    complete_cpu = _complete_cpu_metadata(info)
    client_version = _client_version()
    if (
        parameter_count is not None and active_parameter_count is not None and quant_bits is not None
        and complete_runtime is not None and complete_cpu is not None
        and isinstance(engine_version, str) and engine_version
        and client_version is not None and sample_count >= 3
    ):
        # v7: same direct-metadata contract as the old v6 promotion, plus an
        # explicit outcome so this measurement is unambiguously distinct
        # from a v7 model_unfit/transient_error failure event (never sent
        # from this function - see _report_failure_telemetry). Do not send
        # v6 from new code: v6 stays a read-only, backward-compatible
        # schema for historical data already in Firebase.
        event.update(
            parameter_count_b=parameter_count,
            active_parameter_count_b=active_parameter_count,
            quant_bits=quant_bits,
            engine_version=engine_version,
            client_version=client_version,
            benchmark_version=7,
            outcome="success",
            **complete_runtime,
            **complete_cpu,
        )
```

to:

```python
    complete_runtime = _complete_runtime(runtime)
    complete_cpu = _complete_cpu_metadata(info)
    complete_gpu = _complete_gpu_metadata(info)
    client_version = _client_version()
    if (
        parameter_count is not None and active_parameter_count is not None and quant_bits is not None
        and complete_runtime is not None and complete_cpu is not None
        and isinstance(engine_version, str) and engine_version
        and client_version is not None and sample_count >= 3
    ):
        # v8: same direct-metadata contract as v7, except cpu_score/cpu_tier
        # (locally computed, never the raw name) replace cpu_model, and
        # gpu_score/gpu_tier (same parser, run on the GPU name instead) are
        # attached whenever a GPU was detected at all. Do not send v6/v7
        # from new code: both stay read-only, backward-compatible schemas
        # for historical data already in Firebase.
        event.update(
            parameter_count_b=parameter_count,
            active_parameter_count_b=active_parameter_count,
            quant_bits=quant_bits,
            engine_version=engine_version,
            client_version=client_version,
            benchmark_version=8,
            outcome="success",
            **complete_runtime,
            **complete_cpu,
        )
        if complete_gpu:
            event.update(complete_gpu)
```

- [ ] **Step 6: Wire both into `_report_failure_telemetry`** — change:

```python
    event: dict = {
        "ram_gb": round(info.ram_total_gb, 1),
        "vram_gb": round(info.vram_total_gb, 1) if info.vram_total_gb is not None else None,
        "unified_memory": info.unified_memory,
        "model_installed": _safe_model_filename(tag) or str(tag)[:512],
        "engine": "ollama",
        "benchmark_version": 7,
        "outcome": outcome,
        "failure_reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
```

to:

```python
    event: dict = {
        "ram_gb": round(info.ram_total_gb, 1),
        "vram_gb": round(info.vram_total_gb, 1) if info.vram_total_gb is not None else None,
        "unified_memory": info.unified_memory,
        "model_installed": _safe_model_filename(tag) or str(tag)[:512],
        "engine": "ollama",
        "benchmark_version": 8,
        "outcome": outcome,
        "failure_reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
```

and change:

```python
    complete_cpu = _complete_cpu_metadata(info)
    if complete_cpu:
        event.update(complete_cpu)
```

to:

```python
    complete_cpu = _complete_cpu_metadata(info)
    if complete_cpu:
        event.update(complete_cpu)
    complete_gpu = _complete_gpu_metadata(info)
    if complete_gpu:
        event.update(complete_gpu)
```

- [ ] **Step 7: Update the console disclosure string** — change:

```python
        console.print(
            "[dim]No generated text is stored. v6/v7 telemetry includes CPU model, "
            "architecture, and core counts; it excludes GPU names. "
            "aggregate numbers may be shared below. Not a leaderboard.[/dim]"
        )
```

to:

```python
        console.print(
            "[dim]No generated text is stored. v8 telemetry includes a CPU/GPU "
            "generation score (never the model name), plus CPU architecture and "
            "core counts. aggregate numbers may be shared below. Not a "
            "leaderboard.[/dim]"
        )
```

- [ ] **Step 8: Run the new tests, then the whole file**

Run: `python -m pytest tests/test_cli_benchmark.py -v`
Expected: all PASS. If `test_benchmark_summary_reports_mixed_outcomes_and_uploads_all_of_them` or `test_benchmark_reports_and_uploads_performance_unfit_outcome` fail on `assert ... == 7`, that's expected and fixed in Task 7 below — note the failures and continue; do not treat them as this task's regression.

- [ ] **Step 9: Commit**

```bash
git add src/omm/cli.py tests/test_cli_benchmark.py
git commit -m "feat: send v8 telemetry - cpu_score/gpu_score instead of raw names

_report_telemetry and _report_failure_telemetry both now emit
benchmark_version 8. cpu_model (raw string) is gone; cpu_score/cpu_tier
(from parse_chip_score, same as the CPU-side scoring already used for
local recommend) take its place. gpu_score/gpu_tier are new, attached
whenever hardware.gpu_name is non-empty.

Ref #12"
```

---

## Task 7: Fix the two pre-existing tests that hardcoded `benchmark_version == 7`

**Files:**
- Modify: `tests/test_cli_benchmark.py:355` (inside `test_benchmark_summary_reports_mixed_outcomes_and_uploads_all_of_them`)
- Modify: `tests/test_cli_benchmark.py:363` (same test)
- Modify: `tests/test_cli_benchmark.py:504` (inside `test_benchmark_reports_and_uploads_performance_unfit_outcome`)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing consumed by later tasks — this is cleanup for Task 6's version bump.

- [ ] **Step 1: Find every remaining `== 7` assertion tied to benchmark_version**

Run: `grep -n "benchmark_version.*== 7\|== 7.*benchmark_version" /Users/shinmingyu/Project/Localfit/tests/test_cli_benchmark.py`

- [ ] **Step 2: Change each match from `== 7` to `== 8`**

Read the surrounding ~10 lines of each match first (`sed -n '345,365p' tests/test_cli_benchmark.py` and `sed -n '495,510p' tests/test_cli_benchmark.py`) to confirm each is genuinely asserting the uploaded event's schema version (not an unrelated `7`), then edit each `assert ...["benchmark_version"] == 7` to `assert ...["benchmark_version"] == 8`.

- [ ] **Step 3: Run the full file**

Run: `python -m pytest tests/test_cli_benchmark.py -v`
Expected: all PASS, including the two tests just edited.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cli_benchmark.py
git commit -m "test: update benchmark_version assertions from 7 to 8

Ref #12"
```

---

## Task 8: `scripts/train_model.py` — read v8 rows directly, no raw name to parse

**Files:**
- Modify: `scripts/train_model.py:232` (`benchmark_version not in (...)` acceptance check)
- Modify: `scripts/train_model.py:241` (`is_direct` definition)
- Modify: `scripts/train_model.py:263` (`require_speed and benchmark_version in (...)`)
- Modify: `scripts/train_model.py:338-341` (`cpu_model`/`parse_chip_score` block)
- Modify: `scripts/train_model.py:342-358` (`build_features` call)
- Modify: `scripts/train_model.py:370` (`_real_row_to_sample`'s `== 7` outcome gate)
- Modify: `scripts/train_model.py:403` (`_real_row_to_fit_sample`'s `== 7` outcome gate)
- Modify: `scripts/train_model.py:441-473` (`real_rows_to_training_data_with_audit`'s per-version grouping + audit dict)
- Test: `tests/test_training_data.py` (new v8 tests, plus updating the existing `test_report_telemetry_v7_success_event_feeds_speed_regression_and_positive_fit_label` end-to-end contract test at line 854 to v8)

**Interfaces:**
- Consumes: `omm.featurize.parse_chip_score` (already imported).
- Produces: `_extract_features_and_reason` now accepts `benchmark_version == 8` rows, reading `cpu_score`/`cpu_tier`/`gpu_score`/`gpu_tier` directly off the row instead of parsing `cpu_model` (v8 rows have no `cpu_model` at all). v6/v7 behavior is byte-for-byte unchanged.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_training_data.py`, right after `test_v6_rejects_missing_or_invalid_direct_metadata_without_name_fallback` (read that test and its neighbor `_v6_row` helper first with `sed -n '150,200p' tests/test_training_data.py` to match the existing `_row()` helper's conventions exactly):

```python
def _v8_row(speed: float, **overrides) -> dict:
    return _row(
        speed,
        benchmark_version=8,
        outcome="success",
        parameter_count_b=7.0,
        active_parameter_count_b=3.0,
        quant_bits=4.0,
        engine_version="1.0",
        client_version="1.0",
        runtime_profile="throughput",
        cpu_score=7950.0,
        cpu_tier=1.0,
        cpu_arch="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        gpu_score=4090.0,
        gpu_tier=0.0,
        sample_count=3,
        tokens_per_sec_min=speed - 1,
        tokens_per_sec_max=speed + 1,
        **overrides,
    )


def test_v8_uses_direct_cpu_and_gpu_score_without_a_raw_name():
    X, y = train_model.real_rows_to_training_data([_v8_row(20)])

    assert len(X) == 1
    assert y == [20]


def test_v8_rejects_missing_cpu_score():
    row = _v8_row(20, cpu_score=None)

    _, reason = train_model._real_row_to_sample(row)

    assert reason == "missing_cpu_metadata"


def test_v8_gpu_score_defaults_to_zero_when_no_gpu_was_detected():
    with_gpu, reason1 = train_model._real_row_to_sample(_v8_row(20))
    without_gpu, reason2 = train_model._real_row_to_sample(
        _v8_row(20, gpu_score=None, gpu_tier=None, vram_gb=None)
    )

    assert reason1 is None and reason2 is None
    gpu_score_index = train_model.FEATURE_ORDER.index("gpu_score")
    assert with_gpu[0][gpu_score_index] == 4090.0
    assert without_gpu[0][gpu_score_index] == 0.0


def test_v8_success_outcome_required_like_v7():
    row = _v8_row(20, outcome="bogus")

    _, reason = train_model._real_row_to_sample(row)

    assert reason == "invalid_outcome"
```

`train_model.FEATURE_ORDER` works as written: `scripts/train_model.py:36` already does `from omm.featurize import (..., FEATURE_ORDER, ...)`, and `from X import Y` makes `Y` an attribute of the importing module, so `train_model.FEATURE_ORDER` resolves fine from `tests/test_training_data.py`'s `from scripts import train_model`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_training_data.py -v -k "test_v8_"`
Expected: FAIL (`benchmark_version not in (...)` currently rejects 8 with `"unsupported_schema"`)

- [ ] **Step 3: Extend the accepted-version and `is_direct` checks** — in `scripts/train_model.py`, change:

```python
    benchmark_version = row.get("benchmark_version")
    if benchmark_version not in (None, 1, 2, 3, 4, 5, 6, 7):
        return None, None, "unsupported_schema"

    tokens_per_sec = _bounded_number(row.get("tokens_per_sec"), 0.0, 10_000.0)
    ram_gb = _bounded_number(row.get("ram_gb"), 1.0, 1024.0)
    vram_gb = _bounded_number(row.get("vram_gb"), 0.0, 512.0)
    gpu_tflops = _bounded_number(row.get("gpu_tflops"), 0.0, 1000.0)
    # "direct" = the explicit-metadata schema (v6 and its v7 successor),
    # as opposed to the legacy name-parsing fallback used by v1-v5.
    is_direct = benchmark_version in (6, 7)
```

to:

```python
    benchmark_version = row.get("benchmark_version")
    if benchmark_version not in (None, 1, 2, 3, 4, 5, 6, 7, 8):
        return None, None, "unsupported_schema"

    tokens_per_sec = _bounded_number(row.get("tokens_per_sec"), 0.0, 10_000.0)
    ram_gb = _bounded_number(row.get("ram_gb"), 1.0, 1024.0)
    vram_gb = _bounded_number(row.get("vram_gb"), 0.0, 512.0)
    gpu_tflops = _bounded_number(row.get("gpu_tflops"), 0.0, 1000.0)
    # "direct" = the explicit-metadata schema (v6, v7, and v8), as opposed
    # to the legacy name-parsing fallback used by v1-v5.
    is_direct = benchmark_version in (6, 7, 8)
```

- [ ] **Step 4: Extend the sample-summary version check** — change:

```python
    if require_speed and benchmark_version in (4, 5, 6, 7):
```

to:

```python
    if require_speed and benchmark_version in (4, 5, 6, 7, 8):
```

- [ ] **Step 5: Replace the CPU parsing block with a version-gated branch, and read GPU score too** — change:

```python
    cpu_model = row.get("cpu_model") if is_direct else ""
    if is_direct and (not isinstance(cpu_model, str) or not cpu_model.strip()):
        return None, None, "missing_cpu_metadata"
    cpu_score, cpu_tier = parse_chip_score(cpu_model if isinstance(cpu_model, str) else "")
    features = build_features(
        ram_gb=ram_gb,
        vram_gb=vram_gb,
        unified_memory=bool(row.get("unified_memory")),
        param_count_b=param_count_b,
        quant_bits=quant_bits,
        model_size_gb=model_size_gb,
        gpu_tflops=gpu_tflops or 0.0,
        cpu_score=cpu_score,
        cpu_tier=cpu_tier,
        context_length=context_length,
        gpu_offload_ratio=gpu_offload_percent / 100.0,
        cpu_threads=cpu_threads,
        num_batch=num_batch,
        active_param_count_b=active_param_count_b,
        engine=engine,
    )
    return features, (tokens_per_sec if require_speed else None), None
```

to:

```python
    if benchmark_version == 8:
        cpu_score = _direct_bounded_number(row.get("cpu_score"), 0.0, 99_999.0)
        cpu_tier = _direct_bounded_number(row.get("cpu_tier"), 0.0, 10.0)
        if cpu_score is None or cpu_tier is None:
            return None, None, "missing_cpu_metadata"
        gpu_score = _direct_bounded_number(row.get("gpu_score"), 0.0, 99_999.0) or 0.0
        gpu_tier = _direct_bounded_number(row.get("gpu_tier"), 0.0, 10.0) or 0.0
    else:
        cpu_model = row.get("cpu_model") if is_direct else ""
        if is_direct and (not isinstance(cpu_model, str) or not cpu_model.strip()):
            return None, None, "missing_cpu_metadata"
        cpu_score, cpu_tier = parse_chip_score(cpu_model if isinstance(cpu_model, str) else "")
        gpu_score, gpu_tier = 0.0, 0.0
    features = build_features(
        ram_gb=ram_gb,
        vram_gb=vram_gb,
        unified_memory=bool(row.get("unified_memory")),
        param_count_b=param_count_b,
        quant_bits=quant_bits,
        model_size_gb=model_size_gb,
        gpu_tflops=gpu_tflops or 0.0,
        cpu_score=cpu_score,
        cpu_tier=cpu_tier,
        gpu_score=gpu_score,
        gpu_tier=gpu_tier,
        context_length=context_length,
        gpu_offload_ratio=gpu_offload_percent / 100.0,
        cpu_threads=cpu_threads,
        num_batch=num_batch,
        active_param_count_b=active_param_count_b,
        engine=engine,
    )
    return features, (tokens_per_sec if require_speed else None), None
```

- [ ] **Step 6: Extend the v7-outcome gates to v8** — in `_real_row_to_sample`, change:

```python
    if isinstance(row, dict) and row.get("benchmark_version") == 7:
```

to:

```python
    if isinstance(row, dict) and row.get("benchmark_version") in (7, 8):
```

and in `_real_row_to_fit_sample`, change:

```python
    if row.get("benchmark_version") == 7:
```

to:

```python
    if row.get("benchmark_version") in (7, 8):
```

- [ ] **Step 7: Add v8 to the audit dict's per-version tracking** — in `real_rows_to_training_data_with_audit`, change:

```python
    direct_v6_groups: set[tuple[float, ...]] = set()
    direct_v7_groups: set[tuple[float, ...]] = set()
```

to:

```python
    direct_v6_groups: set[tuple[float, ...]] = set()
    direct_v7_groups: set[tuple[float, ...]] = set()
    direct_v8_groups: set[tuple[float, ...]] = set()
```

change:

```python
        benchmark_version = row.get("benchmark_version")
        if benchmark_version == 6:
            # Reaching this point means _real_row_to_sample accepted the
            # explicit v6 model, runtime, CPU, and sample metadata.
            direct_v6_groups.add(group_key)
        elif benchmark_version == 7:
            # Same explicit-metadata bar, via the v7 outcome="success" path.
            direct_v7_groups.add(group_key)
```

to:

```python
        benchmark_version = row.get("benchmark_version")
        if benchmark_version == 6:
            # Reaching this point means _real_row_to_sample accepted the
            # explicit v6 model, runtime, CPU, and sample metadata.
            direct_v6_groups.add(group_key)
        elif benchmark_version == 7:
            # Same explicit-metadata bar, via the v7 outcome="success" path.
            direct_v7_groups.add(group_key)
        elif benchmark_version == 8:
            # Same bar again, via v8's cpu_score/gpu_score in place of
            # cpu_model.
            direct_v8_groups.add(group_key)
```

and change:

```python
        "direct_v6_unique_configurations": len(direct_v6_groups),
        "direct_v7_unique_configurations": len(direct_v7_groups),
        # Union, not sum: a configuration benchmarked under both v6 and v7
        # (same hardware/model/runtime feature vector, just a newer client)
        # is one real training example, not two. This is the count the
        # quality gate's min_unique_configurations threshold should compare
        # against - see validate_dataset(). The per-version counts above
        # stay purely diagnostic.
        "direct_unique_configurations": len(direct_v6_groups | direct_v7_groups),
```

to:

```python
        "direct_v6_unique_configurations": len(direct_v6_groups),
        "direct_v7_unique_configurations": len(direct_v7_groups),
        "direct_v8_unique_configurations": len(direct_v8_groups),
        # Union, not sum: a configuration benchmarked under v6, v7, and v8
        # (same hardware/model/runtime feature vector, just a newer client)
        # is one real training example, not three. This is the count the
        # quality gate's min_unique_configurations threshold should compare
        # against - see validate_dataset(). The per-version counts above
        # stay purely diagnostic.
        "direct_unique_configurations": len(direct_v6_groups | direct_v7_groups | direct_v8_groups),
```

- [ ] **Step 8: Update the existing end-to-end contract test to v8** — `tests/test_training_data.py:854`'s `test_report_telemetry_v7_success_event_feeds_speed_regression_and_positive_fit_label` calls the real `cli._report_telemetry` and feeds its output straight into `train_model`'s two dataset builders. Since Task 6 made `_report_telemetry` always emit `benchmark_version: 8` now, this test currently fails (it was left alone through Tasks 6-7 on purpose, since it needs *this* task's `train_model.py` changes to pass again). Rename it and update its assertions - change:

```python
def test_report_telemetry_v7_success_event_feeds_speed_regression_and_positive_fit_label(monkeypatch):
    """End-to-end contract check between omm.cli and scripts.train_model:
    the exact event `_report_telemetry` sends for a successful benchmark
    must be consumable by both training datasets. This is exactly the kind
    of client/schema mismatch that once let v7 success events silently
    stay on v6 with no test catching it."""
    from omm import cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "scan_hardware",
        lambda: SimpleNamespace(
            ram_total_gb=16.0, vram_total_gb=8.0, unified_memory=False, gpu_tflops=20.0,
            cpu="Test CPU", cpu_arch="x86_64", cpu_physical_cores=4, cpu_logical_cores=8,
            gpu_name="Test GPU",
        ),
    )
    sent = []
    monkeypatch.setattr(
        cli_mod.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli_mod._report_telemetry(
        "model-7B-Q4.gguf", "org/model", 42.5,
        size_bytes=4 * 1024**3, sample_count=3, speed_min=40.0, speed_max=45.0,
        model_metadata={"parameter_size": "7B", "quantization_level": "Q4_K_M"},
        runtime={
            "runtime_profile": "explicit_ollama_options", "context_length": 4096,
            "gpu_offload_percent": 100, "cpu_threads": 8, "num_batch": 512,
        },
        engine_version="0.32.1", model_filename="model-7B-Q4.gguf", model_digest="sha256:" + "a" * 64,
    )

    event = sent[0]
    assert event["benchmark_version"] == 7
    assert event["outcome"] == "success"
    assert "failure_reason" not in event

    speed_X, speed_y, speed_audit = train_model.real_rows_to_training_data_with_audit([event])
    assert speed_y == [42.5]
    assert speed_audit["direct_v7_unique_configurations"] == 1
    assert speed_audit["rejections"] == {}

    fit_X, fit_y, fit_audit = train_model.real_rows_to_fit_training_data_with_audit([event])
    assert fit_y == [True]
    assert fit_audit["positive_examples"] == 1
    assert fit_audit["negative_examples"] == 0
```

to (the only changes: the docstring/name say v8, `"Test CPU"` no longer matches nothing useful so it's swapped for a parseable name to prove the score comes through, `benchmark_version` assertion is 8, and the audit key is `direct_v8_unique_configurations`):

```python
def test_report_telemetry_v8_success_event_feeds_speed_regression_and_positive_fit_label(monkeypatch):
    """End-to-end contract check between omm.cli and scripts.train_model:
    the exact event `_report_telemetry` sends for a successful benchmark
    must be consumable by both training datasets. This is exactly the kind
    of client/schema mismatch that once let v7 success events silently
    stay on v6 with no test catching it."""
    from omm import cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "scan_hardware",
        lambda: SimpleNamespace(
            ram_total_gb=16.0, vram_total_gb=8.0, unified_memory=False, gpu_tflops=20.0,
            cpu="AMD Ryzen 5 5600X", cpu_arch="x86_64", cpu_physical_cores=4, cpu_logical_cores=8,
            gpu_name="NVIDIA RTX 4090",
        ),
    )
    sent = []
    monkeypatch.setattr(
        cli_mod.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli_mod._report_telemetry(
        "model-7B-Q4.gguf", "org/model", 42.5,
        size_bytes=4 * 1024**3, sample_count=3, speed_min=40.0, speed_max=45.0,
        model_metadata={"parameter_size": "7B", "quantization_level": "Q4_K_M"},
        runtime={
            "runtime_profile": "explicit_ollama_options", "context_length": 4096,
            "gpu_offload_percent": 100, "cpu_threads": 8, "num_batch": 512,
        },
        engine_version="0.32.1", model_filename="model-7B-Q4.gguf", model_digest="sha256:" + "a" * 64,
    )

    event = sent[0]
    assert event["benchmark_version"] == 8
    assert event["outcome"] == "success"
    assert event["cpu_score"] == 5600.0
    assert event["gpu_score"] == 4090.0
    assert "failure_reason" not in event
    assert "cpu_model" not in event

    speed_X, speed_y, speed_audit = train_model.real_rows_to_training_data_with_audit([event])
    assert speed_y == [42.5]
    assert speed_audit["direct_v8_unique_configurations"] == 1
    assert speed_audit["rejections"] == {}

    fit_X, fit_y, fit_audit = train_model.real_rows_to_fit_training_data_with_audit([event])
    assert fit_y == [True]
    assert fit_audit["positive_examples"] == 1
    assert fit_audit["negative_examples"] == 0
```

- [ ] **Step 9: Run the new tests, then the whole file**

Run: `python -m pytest tests/test_training_data.py -v -k "test_v8_ or v8_success_event_feeds"`
Expected: all PASS.

Run: `python -m pytest tests/test_training_data.py -v`
Expected: all PASS, including every pre-existing v1-v7 test unchanged.

- [ ] **Step 10: Commit**

```bash
git add scripts/train_model.py tests/test_training_data.py
git commit -m "feat: train_model.py reads v8 cpu_score/gpu_score directly

v8 rows carry no cpu_model to parse - cpu_score/cpu_tier are read
straight off the row, same as every other direct-metadata field. v6/v7
rows are unaffected: they still go through parse_chip_score(cpu_model).

Ref #12"
```

---

## Task 9: `tests/test_feature_parity.py` — bring the canonical parity fixture up to v8

**Files:**
- Modify: `tests/test_feature_parity.py:26-70` (`_hardware()` and `_row()`)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing consumed elsewhere — this closes the loop opened in Task 2, making the file's core parity test (`test_prediction_features_match_privacy_minimized_training_row`) exercise the *current* schema (v8) end to end, the same way it presumably already got bumped from v5 to v6 previously.

- [ ] **Step 1: Confirm the current test still passes before touching it** (baseline)

Run: `python -m pytest tests/test_feature_parity.py -v`
Expected: PASS (this was already true after Task 2 — Task 2's new test added GPU coverage but didn't touch `_row()`, so the v6 `_row()` fixture still has no `gpu_score`/`gpu_tier` and the trainer still defaults those to 0.0 for `benchmark_version: 6`, matching what `build_prediction_features` would need to match *if* `_hardware()`'s gpu had been zero — but `_hardware()` already sets `gpu_name="NVIDIA RTX 4090"`. Actually check this now instead of assuming:)

Run: `python -m pytest tests/test_feature_parity.py::test_prediction_features_match_privacy_minimized_training_row -v`

If this FAILS after Task 2 (because `build_prediction_features` now always computes a real `gpu_score` from `_hardware().gpu_name`, while the v6 `_row()` has no `gpu_score` and the trainer defaults it to 0.0), that confirms the fixture needs bumping to v8 — proceed to Step 2. If it somehow still passes, the two values coincidentally matched; still proceed to Step 2 so the test exercises the current schema rather than a coincidence.

- [ ] **Step 2: Bump `_row()` to v8 shape** — change:

```python
def _row() -> dict:
    return {
        "engine": "ollama",
        "benchmark_version": 6,
        "tokens_per_sec": 20,
        "sample_count": 3,
        "tokens_per_sec_min": 20,
        "tokens_per_sec_max": 20,
        "ram_gb": 16,
        "vram_gb": 8,
        "gpu_tflops": 20,
        "unified_memory": False,
        "model_installed": "model-7B-Q4.gguf",
        "model_repo_id": "org/model-7B",
        "model_size_bytes": 4 * 1024**3,
        "parameter_count_b": 7.0,
        "active_parameter_count_b": 7.0,
        "quant_bits": 4.0,
        "engine_version": "0.12.0",
        "client_version": "0.1.44",
        "runtime_profile": "explicit_ollama_options",
        "context_length": 4096,
        "gpu_offload_percent": 100,
        "cpu_threads": 8,
        "num_batch": 512,
        "cpu_model": "AMD Ryzen 9 7950X3D",
        "cpu_arch": "x86_64",
        "cpu_physical_cores": 16,
        "cpu_logical_cores": 32,
    }
```

to:

```python
def _row() -> dict:
    return {
        "engine": "ollama",
        "benchmark_version": 8,
        "outcome": "success",
        "tokens_per_sec": 20,
        "sample_count": 3,
        "tokens_per_sec_min": 20,
        "tokens_per_sec_max": 20,
        "ram_gb": 16,
        "vram_gb": 8,
        "gpu_tflops": 20,
        "unified_memory": False,
        "model_installed": "model-7B-Q4.gguf",
        "model_repo_id": "org/model-7B",
        "model_size_bytes": 4 * 1024**3,
        "parameter_count_b": 7.0,
        "active_parameter_count_b": 7.0,
        "quant_bits": 4.0,
        "engine_version": "0.12.0",
        "client_version": "0.1.44",
        "runtime_profile": "explicit_ollama_options",
        "context_length": 4096,
        "gpu_offload_percent": 100,
        "cpu_threads": 8,
        "num_batch": 512,
        # Matches parse_chip_score("AMD Ryzen 9 7950X3D") == (7950.0, 1.0)
        # and parse_chip_score("NVIDIA RTX 4090") == (4090.0, 0.0) -
        # _hardware()'s cpu/gpu_name below, run through the same parser
        # `_report_telemetry` uses client-side.
        "cpu_score": 7950.0,
        "cpu_tier": 1.0,
        "cpu_arch": "x86_64",
        "cpu_physical_cores": 16,
        "cpu_logical_cores": 32,
        "gpu_score": 4090.0,
        "gpu_tier": 0.0,
    }
```

(`_hardware()` at the top of this file already has `cpu="AMD Ryzen 9 7950X3D"` and `gpu_name="NVIDIA RTX 4090"` - leave it unchanged.)

- [ ] **Step 3: Run the full file**

Run: `python -m pytest tests/test_feature_parity.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_feature_parity.py
git commit -m "test: bump feature-parity fixture from v6 to v8

Keeps this file testing parity against the current schema, same as
when it presumably moved from v5 to v6 previously.

Ref #12"
```

---

## Task 10: `docs/telemetry-v8.md` — document the delta

**Files:**
- Create: `docs/telemetry-v8.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by code — this is documentation, but it's the file `_complete_cpu_metadata`'s and `_complete_gpu_metadata`'s docstrings (Task 6) point readers to, so it must exist and match what was actually implemented.

- [ ] **Step 1: Write the file**

```markdown
# Telemetry v8: chip score instead of raw CPU name, plus GPU chip score

## What changed from v7

v8 is structurally identical to v7 (see `docs/telemetry-v7.md` for the
`outcome` enum, failure-event shape, and the two-dataset training split -
none of that changed) except for how CPU/GPU identity is represented:

- **`cpu_model` (raw string) is gone.** In its place: `cpu_score` and
  `cpu_tier`, two numbers computed locally by
  `omm.featurize.parse_chip_score()` - the same best-effort model-number +
  tier-word parser that already scored CPUs for the on-device recommend
  engine before this change. `cpu_arch`, `cpu_physical_cores`, and
  `cpu_logical_cores` are unaffected (they were never raw identity strings).
- **`gpu_score`/`gpu_tier` are new.** Same parser, run on
  `hardware.gpu_name` instead of `hardware.cpu`. The raw GPU name is still
  never uploaded - only these two numbers, and only when a GPU was
  detected at all (`gpu_score`/`gpu_tier` are both absent, not zero, on a
  CPU-only machine).

## Why a new version instead of changing v7 in place

`cpu_model` was a required field in v7's success branch
(`database.rules.json`). Dropping it is a breaking schema change, so it
gets a new `benchmark_version` rather than mutating v7 - v6 and v7 rows
already in Firebase keep their raw `cpu_model` forever (historical,
read-only), exactly like v6 kept its shape when v7 launched.

## Why an ordinal score instead of real GPU TFLOPS

A static `{gpu_name: tflops}` lookup table was considered and rejected: it
needs manual upkeep every GPU generation with no auto-update path, and
"TFLOPS" is itself ambiguous (FP32 vs FP16 vs Tensor). `parse_chip_score`
is coarse (a regex model-number extraction + tier-word lookup, not a
physical measurement) but self-updating for any chip name matching the
existing patterns, and it's the same precision class the CPU side already
shipped in production. The regressor's real capacity/throughput signal is
`tokens_per_sec` + `vram_gb`; `gpu_score`/`gpu_tier` is a secondary feature
among 18 (`omm.featurize.FEATURE_ORDER`), same role `cpu_score`/`cpu_tier`
already plays. `scripts/model_quality_gate.py` catches a net-negative
feature before any retrained model ships.

Full design rationale: `docs/superpowers/specs/2026-07-30-telemetry-chip-score-v8-design.md`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/telemetry-v8.md
git commit -m "docs: add telemetry-v8.md

Ref #12"
```

---

## Task 11: Full suite + manual Firebase publish reminder

**Files:** none (verification only)

- [ ] **Step 1: Run the entire Python test suite**

Run: `python -m pytest -q`
Expected: all PASS, zero failures, zero unexpected skips beyond the pre-existing `pytest.importorskip("sklearn")` guard.

- [ ] **Step 2: Run the Firebase rules emulator suite one more time end to end**

Run: `npx --yes firebase-tools@15.24.0 emulators:exec --only database --project demo-localfit "node scripts/test_firebase_rules.mjs"`
Expected: exits 0.

- [ ] **Step 3: Stop and flag the manual step - do not attempt to automate it**

Print this reminder verbatim to whoever is running this plan:

> `database.rules.json` in the repo is not what Firebase enforces. After this
> branch merges, open the Firebase console → Realtime Database → Rules, paste
> in the new contents of `database.rules.json`, and click Publish. Until that
> happens, every client that upgrades and starts sending `benchmark_version:
> 8` will have its events **rejected** by the still-live v7-only rules (v8
> isn't in the old rules' `benchmark_version` bound), and `omm` will print
> "Telemetry not sent (will retry next time you run omm)" - it fails safe,
> nothing crashes, but no v8 data reaches Firebase until Publish happens.

- [ ] **Step 4: No commit for this task** - it's verification only. If Step 1 or Step 2 found a failure, go back to the task that owns the failing file and fix it there (don't patch it from this task).
