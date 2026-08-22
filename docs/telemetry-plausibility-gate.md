# Telemetry plausibility gate

`database.rules.json` validates telemetry one field at a time: types, ranges,
`"$other": false` for unknown keys, and create-only writes. That is a complete
defense against malformed data and no defense at all against *fabricated*
data. Every field of a poisoned row can sit inside its documented range while
the combination is physically impossible - a `tokens_per_sec` no machine could
reach for the `parameter_count_b`/`quant_bits`/hardware the same row reports.

Flooding defenses do not close this gap either. omm is open source, so an
attacker runs the real client with fabricated numbers; App Check or
proof-of-work only proves a real client produced the write, not that the
measurement happened.

The rules language cannot express a cross-field statistical check, so the
defense lives in the nightly training preprocessing
(`scripts/train_model.py`), where the whole corpus is available at once. It
has two stages, and neither drops anything silently.

## Stage 1: physical ceiling (per row)

Single-stream decode reads the model's active weights once per generated
token, so throughput is memory-bandwidth bound:

```
tokens_per_sec <= memory_bandwidth_gb_per_s / active_weight_gb
```

`active_weight_gb` is `active_parameter_count_b * quant_bits / 8`, falling
back to the total parameter count and then to the file size, so mixture-of-
experts rows are bounded by the weights actually read rather than by the
model's nominal size. The bandwidth term is the peak of the fastest hardware
anyone could plausibly be running omm on, chosen per decode path:

| decode path | constant | value |
| --- | --- | --- |
| weights read over GPU/unified memory | `PEAK_GPU_MEMORY_BANDWIDTH_GB_PER_S` | 4000 |
| weights read over system RAM | `PEAK_CPU_MEMORY_BANDWIDTH_GB_PER_S` | 800 |

Real engines achieve well under peak, and the check only needs an upper
bound, so both constants are deliberately generous: honest telemetry from
hardware newer than this document still passes. Rows failing it are rejected
with reason `implausible_speed_for_hardware`.

`cpu_score`/`gpu_score` cannot scale this ceiling. Despite the names they are
not performance scores - `omm.featurize.parse_chip_score()` returns the chip's
model *number* (`"RTX 4090"` -> 4090.0, `"Apple M2 Pro"` -> 2.0), which is not
comparable across vendors. They are therefore used categorically only, to
decide whether a GPU decode path is claimed at all.

## Stage 2: statistical outliers (per configuration)

The ceiling catches the impossible; the distribution catches the merely
absurd. The statistic is *implied memory bandwidth*,
`tokens_per_sec * active_weight_gb`, not raw speed: a 0.5B model is
legitimately an order of magnitude faster than a 70B one on identical
hardware, so an IQR over raw speeds would just rediscover model size and
delete honest small-model rows.

- **Population**: per-configuration medians, after the existing duplicate
  collapse. Every distinct configuration gets one vote, so a burst of
  fabricated uploads cannot drag the quartiles it is about to be measured
  against.
- **Buckets**: hardware identity (`ram_gb`, `vram_gb`, `unified_memory`,
  `cpu_score`, `cpu_tier`, `gpu_score`, `gpu_tier`). Achieved bandwidth on a
  discrete GPU and on a laptop CPU differ by two orders of magnitude; one
  global distribution would treat every fast machine as an outlier.
- **Fence**: Tukey with a multiplier of 3.0 (the "far out" fence), not the
  usual 1.5. The corpus is small, skewed, and deliberately diverse, so
  dropping an honest exotic configuration costs more than letting a mild
  exaggeration through.
- **Small samples**: a bucket with fewer than `MIN_OUTLIER_SAMPLE_SIZE` (8)
  configurations has no meaningful quartiles, so its members are judged
  against the global pool instead - otherwise inventing a novel hardware
  identity would be enough to skip the check. If the global pool is also
  under 8, filtering is skipped entirely and reported as skipped. A
  degenerate (zero-width) IQR is treated the same way.

Rejection reasons are `statistical_speed_outlier_high` and
`statistical_speed_outlier_low`.

## Reporting

Both stages count as data-quality rejections in the existing audit, so they
appear in `telemetry_audit["rejections"]` (and therefore in the trained
artifact and the quality report), and they count toward
`validate_dataset()`'s rejection-rate limit. That is deliberate: a
large-scale poisoning attempt pushes the rate past the limit and keeps the
published model unchanged, instead of retraining on whatever survived the
filter.

`telemetry_audit["speed_outliers"]` records how stage 2 decided:

```json
{
  "statistic": "implied_memory_bandwidth_gb_per_s",
  "fence_multiplier": 3.0,
  "minimum_sample_size": 8,
  "hardware_buckets": 4,
  "buckets_evaluated": 1,
  "buckets_pooled_globally": 3,
  "global_pool_applied": true,
  "skipped": false,
  "dropped_configurations": 2,
  "dropped_rows": 5
}
```

`train_model.main()` also prints a one-line summary of both stages, so a
nightly run that drops data says so in the CI log rather than truncating
quietly.
