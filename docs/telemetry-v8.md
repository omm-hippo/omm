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
