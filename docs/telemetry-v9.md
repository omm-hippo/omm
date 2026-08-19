# Telemetry v9: fixed contribution profile and measurement quality

v9 is emitted only by successful `omm contribute` benchmarks. Ordinary
installs and benchmarks remain v8. A contribution uses `num_ctx=1024` and
`num_batch=128`; CPU thread count and GPU placement remain hardware-specific,
but GPU placement is derived from total capacity rather than momentary free
VRAM.

## Memory model

The pre-load gate no longer treats a GGUF file as anonymous committed RAM or
uses `file_size * 1.2`. It records three distinct estimates:

- memory-mapped weight working set;
- committed host RAM for KV cache, compute buffer, and runtime overhead;
- dedicated VRAM required by the selected offload profile.

Architecture dimensions come from the bounded GGUF header when available.
Before download, OMM requests at most 16 MiB with HTTP Range and stops reading
at that bound even if a provider ignores Range. After download, the local
header is parsed again. Missing metadata uses an explicitly labelled
`profile_fallback` estimate.

Only committed buffers are an OOM gate. Current shortage is deferred and
retried at most three times; a requirement exceeding physical capacity is
blocked. Mapped weights are not a hard gate because llama.cpp/Ollama normally
memory-map them. Paging risk is assessed during and after measurement.

## Added success fields

- `measurement_profile`: `contribute-v1`
- `measurement_quality`: `clean`, `pressured`, or `unstable`
- `ram_available_before_gb`, `ram_available_min_gb`, `ram_available_after_gb`
- `memory_pressure_observed`
- `tokens_per_sec_mad_ratio`
- `memory_estimate_source`, `memory_estimate_confidence`
- `estimated_mapped_weights_gb`, `estimated_committed_ram_gb`,
  `estimated_required_vram_gb`

Three speed runs are required. A non-pressured result with MAD/median at most
0.15 is `clean`; a higher ratio is `unstable`. Any observed low-memory sample
labels a completed result `pressured`. Sustained emergency pressure cancels
an OMM-owned load, so no speed event is uploaded for that aborted attempt.

The speed regressor consumes only `clean` v9 rows. Pressured and unstable
rows remain auditable and may still provide a positive model-fit observation,
but cannot distort throughput training. Firebase rules, the self-hosted
collector, training importer, and quality gate enforce the same contract.

## What `measurement_quality` still does not describe

Both quality signals answer questions about the run's own samples. The MAD
ratio measures how much they disagreed with each other, and
`memory_pressure_observed` measures whether available RAM dipped. Neither
sees a steady, memory-light background load - a compile, a video, another
agent - which depresses every sample by roughly the same amount. Such a run
is internally tight and lands as `clean`, so it reaches the speed regressor
as an honest measurement of a slower machine (issue #32).

omm now samples system-wide CPU utilization before a benchmark starts, warns
when other programs are already busy, and withholds that measurement from the
local calibration factor. That guard is deliberately client-side and
local-only: the deployed rules bind `measurement_quality` to the other two
signals - `unstable` is valid only with a MAD ratio above 0.15, `pressured`
only with `memory_pressure_observed` true - so a load-depressed run cannot
be relabelled under either existing value, and a fourth value would need the
rules deployed ahead of any client that sends it. Carrying host load in
telemetry therefore remains open, and would want its own field rather than a
new enum value.
