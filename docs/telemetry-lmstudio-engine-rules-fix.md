# Firebase RTDB rules: `engine` field must accept `"lmstudio"`

## What was found

`database.rules.json`'s `/telemetry/$event` validation hard-required
`engine == 'ollama'` in all four places the field is checked (the
`benchmark_version` 1-6, 7, and 8 branches of the row-shape `.validate`,
plus the per-field `"engine"` validator). Before this fix, every telemetry
row uploaded with `engine: "lmstudio"` (the value the new LM Studio
benchmark/contribute fallback sends - see issue #96) would be silently
rejected by Firebase - the exact same failure pattern as
[[project_omm_contribute_telemetry_model_provider_rules_bug]]: the client
believes the upload succeeded or fails soft, and the row never appears in
Firebase, with no error surfaced to the user.

Confirmed live: ran `omm benchmark` against a real headless LM Studio
instance during this feature's development and inspected the local
evidence JSON it produced - `environment.engine` was correctly `"lmstudio"`
end to end on the client side, which is exactly the value the pre-fix
rules would have rejected on upload.

## What changed

`database.rules.json` now accepts `engine == 'ollama' || engine ==
'lmstudio'` everywhere it previously required the literal `'ollama'`. No
other validation changed - LM Studio rows still go through the same
`benchmark_version` shape checks as Ollama rows.

## Known consequence: LM Studio rows will mostly land as `benchmark_version: 4`

`cli._report_telemetry` only upgrades a row to `benchmark_version: 8` (the
richest schema, with runtime/CPU/GPU detail) when `engine_version` is a
non-empty string and `runtime` (Ollama's `/api/ps`-measured
`gpu_offload_percent`/`context_length`/etc.) is present. For the LM Studio
engine, neither is reliably available yet:

- `LMStudioAdapter.health().version` returns `None` against a real LM
  Studio server (confirmed live, pre-existing gap, out of this feature's
  scope - tracked separately).
- `quality.evaluate_model`'s `runtime` field is always `None` for
  `engine="lmstudio"` by design (spec's Non-goals: no LM Studio equivalent
  of Ollama's `/api/ps` VRAM-residency measurement exists yet).

So LM Studio success rows will fall through to the sparser
`benchmark_version: 4` shape (still valid, still useful for
speed/quality/hardware-fit training data) until those two gaps are closed
in a follow-up.

## What still needs to happen (not done by this change)

This commit only changes the tracked rules file. Firebase's live rules are
a separate deployment step (`firebase deploy --only database`, or pasting
the file into the Realtime Database → Rules console) that needs someone
with access to the `localfit-8ab57` Firebase project to run **after this
change merges** - otherwise LM Studio telemetry keeps failing exactly as
before, silently. After deploying, verify with a real write:

```bash
curl -X POST 'https://localfit-8ab57-default-rtdb.firebaseio.com/telemetry.json' \
  -d '{"ram_gb": 16, "unified_memory": true, "model_installed": "test", "engine": "lmstudio", "benchmark_version": 4, "recorded_at": "2026-01-01T00:00:00Z", "tokens_per_sec": 10.0, "sample_count": 3, "tokens_per_sec_min": 9.0, "tokens_per_sec_max": 11.0}'
```

Expect a `{"name": "..."}` success response, not a permission-denied error.
