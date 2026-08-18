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

## The LM Studio lane: no runtime block, everything else required

v8's success branch was written around Ollama's `/api/ps` runtime snapshot
and required `runtime_profile`, `context_length`, `gpu_offload_percent`,
`cpu_threads`, and `num_batch` on every success row. LM Studio exposes no
`/api/ps` equivalent and ignores the Ollama runtime options omm computes,
so `quality.collect_evidence` returns `runtime: None` for
`engine="lmstudio"` by design. The single Ollama-shaped gate in
`cli._report_telemetry` therefore dropped every LM Studio benchmark back to
the `benchmark_version: 4` shape, discarding `parameter_count_b`,
`active_parameter_count_b`, `quant_bits`, `engine_version`,
`client_version`, and the whole `cpu_score`/`cpu_tier`/`cpu_arch`/core-count
block that LM Studio *can* report perfectly well.

v8's success branch now splits on `engine`:

- `engine == 'ollama'` - unchanged. The five runtime fields stay required
  and keep their range checks.
- `engine == 'lmstudio'` - the five runtime fields must be **absent**.
  Absence is enforced, not merely tolerated: a row carrying any of them is
  rejected, so an unknown runtime can never be laundered into a measured
  one. Everything else v8 asks for stays mandatory, including
  `engine_version`, `sample_count >= 3`, the speed min/median/max ordering,
  the parameter-count sanity check, and the CPU core-count check.

Consumers that need runtime detail should filter on the presence of
`runtime_profile` rather than on `benchmark_version`.

### Why LM Studio stops at v8 and never reaches v9

`benchmark_version: 9` is not "v8 plus memory data" - it is the
`contribute-v1` measurement profile (`docs/telemetry-v9.md`), which asserts
the run happened at exactly `context_length` 1024 and `num_batch` 128 and
that the memory estimate was computed for that same configuration. omm
applies that profile through Ollama's options; LM Studio ignores them and
reports nothing back, so an LM Studio v9 row could only claim a conformance
it never had. v9 stays `engine == 'ollama'` on purpose, and
`_report_telemetry` refuses to upgrade an LM Studio row past v8 even when a
memory measurement and estimate are both available.

### Remaining gap: `engine_version`

An LM Studio row still needs a non-empty `engine_version` to reach v8, and
the only honest source omm has for it is the `x-lm-studio-version` response
header (`LMStudioAdapter.health()`). Server builds that omit that header
leave `engine_version` as `None` and the row stays at `benchmark_version: 4`
- the same place it landed before this change, never a fabricated version
string.
