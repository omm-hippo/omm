# Telemetry v8: drop raw CPU name, add GPU chip score

## Problem

Firebase telemetry (`omm benchmark` / `omm contribute` uploads) currently sends
the raw CPU model string (`cpu_model`, e.g. "13th Gen Intel(R) Core(TM)
i7-13700K") on every v6/v7 success and best-effort failure event. GPU model
names are deliberately never sent (privacy: a raw device name string, combined
with RAM/VRAM, is close to a hardware fingerprint), but the training pipeline
therefore has zero GPU-generation signal at all - `gpu_score`/`gpu_tier`
(feature slots that already exist in `featurize.FEATURE_ORDER`) are hardcoded
to `0.0` in both `predictor.py` and `scripts/train_model.py`.

This asymmetry (CPU raw name allowed, GPU raw name blocked, GPU score never
populated) was flagged as an inconsistency worth fixing. It splits into two
independent changes bundled under one v8 schema bump because they touch the
same validation surface (Firebase rules, server model, client telemetry
builder) and are easier to review/ship together.

## Design

### 1. GPU chip score (new signal, no raw name)

`featurize.parse_chip_score(text) -> (score, tier)` already generalizes across
CPU and GPU naming ("Apple M2 Pro" -> `(2.0, 1.0)`, "RTX 4090" -> `(4090.0,
0.0)"). It is a best-effort ordinal parse (regex model number + tier-word
lookup), not a physical measurement - same precision class as the CPU score it
already powers in production. It is never invoked on `hardware.gpu_name`
today; that's the entire gap.

Fix: call it on `hardware.gpu_name` wherever it's already called on
`hardware.cpu`, and upload only the resulting two numbers - never the name
itself. This mirrors the CPU precedent exactly, so GPU privacy stays as strict
as it is today while gaining the same class of signal CPU already has.

Trade-off accepted: this is not real TFLOPS (a static per-chip lookup table
was considered and rejected - see below). It's a coarse, self-updating ordinal
proxy. The regressor's real capacity/throughput signal remains measured
`tokens_per_sec` + `vram_gb`; `gpu_score`/`gpu_tier` is a secondary feature
among 18, the same role `cpu_score`/`cpu_tier` already plays. A worse feature
is caught by `scripts/model_quality_gate.py` before any retrained model ships
- it doesn't reach users blind.

Known limitation carried over unchanged from the CPU implementation: a laptop
GPU/CPU sharing a desktop part's model number scores identically today (no
"laptop"/"mobile" tier deduction exists in `_TIER_WORDS`). Out of scope here -
not a regression, and no different from the existing CPU behavior.

**Rejected alternative:** a static `{gpu_name: tflops}` lookup table (per
vendor, with separate laptop/desktop keys) was the original idea. Rejected
because (a) it needs manual upkeep every GPU generation with no
auto-update path, (b) "TFLOPS" is itself ambiguous (FP32 vs FP16 vs Tensor),
and (c) it solves a problem the codebase already solved differently for CPU -
introducing a second, inconsistent pattern for the same kind of signal.

**Rejected alternative:** computing real TFLOPS via `pynvml` core-count +
clock-speed formula. Rejected because the needed pynvml API
(`nvmlDeviceGetNumGpuCores`) isn't consistently available across driver/binding
versions, has no equivalent on Apple Silicon at all, and produces a number
whose real-world meaning (dense FP32? boosted clock? which precision?) is
unclear enough to be worse than an honestly-labeled ordinal score.

### 2. Drop raw CPU name from telemetry (v8 schema)

`_complete_cpu_metadata()` (cli.py) currently returns `cpu_model` (raw string)
+ `cpu_arch` + core counts. New behavior: compute `cpu_score, cpu_tier =
parse_chip_score(cpu_model)` locally and return those instead of the raw
string. `cpu_arch` (e.g. "x86_64", "arm64" - a tiny fixed vocabulary, not an
identifying string) and the core counts are unaffected.

This is a breaking change to the v7 "direct metadata" contract (`cpu_model`
goes from required to gone), so it ships as a new `benchmark_version: 8`
rather than mutating v7 in place - same pattern the v6 -> v7 transition used.
v6 and v7 rows already in Firebase keep their raw `cpu_model` forever
(historical, read-only, per existing project policy); only new rows stop
sending it.

### Where this touches code

- `src/omm/featurize.py` - no change; `parse_chip_score` already handles GPU
  names, this task just calls it with one.
- `src/omm/predictor.py` - replace hardcoded `gpu_score=0.0, gpu_tier=0.0`
  with `parse_chip_score(hw.gpu_name or "")`.
- `src/omm/cli.py`:
  - `_complete_cpu_metadata()`: emit `cpu_score`/`cpu_tier` instead of
    `cpu_model`. Keep the existing "is this data actually usable" guards
    (non-empty, bounded length, physical <= logical cores).
  - new `_complete_gpu_metadata()`: same shape, returns
    `{"gpu_score":, "gpu_tier":}` or `None` when `hardware.gpu_name` is falsy
    (no GPU detected at all - distinct from "GPU detected but unparseable",
    which is a legitimate `(0.0, 0.0)`).
  - `_report_telemetry` (success path) and `_report_failure_telemetry`
    (best-effort failure path) both call `_complete_cpu_metadata` already;
    both start calling `_complete_gpu_metadata` too, and both bump their
    emitted `benchmark_version` from 7 to 8.
  - Console disclosure string (~line 2657) updated to describe v8 and mention
    GPU score is sent (not GPU name).
- `database.rules.json` - add a `benchmark_version == 8` validation branch,
  structurally identical to the existing v7 branch, except: `cpu_model`/
  `cpu_arch` requirement replaced by `cpu_score`/`cpu_tier`/`cpu_arch`
  (arch stays), and `gpu_score`/`gpu_tier` added as new optional top-level
  field validators (present only for v8, like `gpu_tflops` already is for any
  version). Bump `benchmark_version` field's max from 7 to 8.
  Bounds: `cpu_score`/`gpu_score` are numbers in `[0, 99999]` (5-digit model
  numbers like "4090" are the largest realistic match from
  `_CHIP_MODEL_RE`); `cpu_tier`/`gpu_tier` are numbers in `[0, 10]` (today's
  highest `_TIER_WORDS` value is 3.0 for "ultra" - headroom left for new tier
  words without another schema bump).
  **Requires manual Publish in the Firebase console after merge** - editing
  the JSON file in the repo does not change what's live (past incident:
  [[project_omm_contribute_telemetry_model_provider_rules_bug]]).
- `src/localfit_server/app.py` - add `cpu_score`/`cpu_tier`/`gpu_score`/
  `gpu_tier` optional fields to the Pydantic model; extend the
  `benchmark_version` bound and the version-gated required-field validators
  to cover `8`.
- `scripts/train_model.py` - extend the accepted `benchmark_version` set to
  include `8`; for v8 rows read `cpu_score`/`cpu_tier`/`gpu_score`/`gpu_tier`
  directly off the row instead of calling `parse_chip_score` server-side
  (there's no raw string left to parse); v6/v7 rows keep the current
  `parse_chip_score(cpu_model)` / hardcoded-zero-GPU behavior unchanged
  (historical compatibility).
- `docs/telemetry-v8.md` (new file) - documents the v8 delta from v7,
  referencing `docs/telemetry-v7.md` for the unchanged parts (failure-event
  shape, outcome lanes, etc).
- Tests: `scripts/test_firebase_rules.mjs` gets v8 success/model_unfit/
  performance_unfit/transient_error cases mirroring the existing v7 block;
  Python tests cover `_complete_cpu_metadata`'s new return shape, the new
  `_complete_gpu_metadata`, `predictor.py`'s GPU score wiring, and
  `train_model.py`'s v8 row handling.

### Out of scope

- Real TFLOPS numbers (`gpu_tflops` field itself stays unpopulated, as it is
  today - not part of this change).
- Laptop/mobile tier deduction in `_TIER_WORDS`.
- AMD/Intel discrete GPU detection (`hardware.py` doesn't detect these today;
  unaffected by this change either way).
