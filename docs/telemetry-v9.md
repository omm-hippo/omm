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
- `measurement_quality`: `clean`, `pressured`, `unstable`, or `loaded`
- `ram_available_before_gb`, `ram_available_min_gb`, `ram_available_after_gb`
- `memory_pressure_observed`
- `tokens_per_sec_mad_ratio`
- `host_cpu_load_percent` (optional; see below)
- `memory_estimate_source`, `memory_estimate_confidence`
- `estimated_mapped_weights_gb`, `estimated_committed_ram_gb`,
  `estimated_required_vram_gb`

Three speed runs are required. Any observed low-memory sample labels a
completed result `pressured`. Sustained emergency pressure cancels an
OMM-owned load, so no speed event is uploaded for that aborted attempt.
Otherwise a MAD/median above 0.15 is `unstable`; a background CPU load at or
above 25% is `loaded`; anything left is `clean`.

The speed regressor consumes only `clean` v9 rows. Pressured, unstable, and
loaded rows remain auditable and may still provide a positive model-fit
observation, but cannot distort throughput training. Firebase rules, the
self-hosted collector, training importer, and quality gate enforce the same
contract.

## Host background load

The MAD ratio and `memory_pressure_observed` both answer questions about the
run's own samples: how much they disagreed with each other, and whether
available RAM dipped. Neither sees a steady, memory-light background load -
a compile, a video, another agent - which depresses every sample by roughly
the same amount. Such a run is internally tight and RAM never moves, so v9
originally labelled it `clean` and fed it to the speed regressor as an honest
measurement of a slower machine (issue #32).

`host_cpu_load_percent` records system-wide CPU utilization sampled before
the daemon starts and the model loads, while omm itself is idle, so the
number describes other programs. At or above `hardware.BUSY_CPU_PERCENT`
(25%) the row is labelled `loaded`.

Precedence is `pressured` > `unstable` > `loaded` > `clean`. The two original
signals are direct observations of this run's own data, while host load
explains a run that otherwise looks fine; ordering them first means `loaded`
only ever refines what v9 would already have called `clean`, and never masks
a defect the older signals caught. The Firebase rules, `localfit_server`, and
the training importer enforce that order in both directions, so a row whose
label disagrees with its own signals is rejected rather than stored.

The field is optional, and the version was deliberately not bumped past 9.
Clients that predate it - and clients whose sampler could not read a value -
send v9 rows with no such key, and those are validated by the two original
signals exactly as before. An absent reading therefore means *unknown*, never
*idle*: a `clean` row without the field is as ambiguous as every v9 row was
before, while a `clean` row carrying a reading below 25% is positively
attested as quiet.

Because the per-event rules reject any undeclared key, the rules must be
published before any client sends `host_cpu_load_percent`, not after.
