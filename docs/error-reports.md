# Error reports: what they contain, and what they never contain

`omm contribute` runs unattended on machines nobody is watching. When a
model fails to load, a daemon dies, or omm itself crashes, that information
scrolls past on one console and is gone. Error reports let you hand a small,
scrubbed description of such a failure back to the project.

They are **off unless you turn them on**. There is no "on by default", no
first-run default, and no wizard question that could be answered by
accident. This is the one setting in omm where the default differs from
telemetry's `ask`: telemetry uploads anonymous numbers, error reports carry
text, and text needs a stricter door.

```
omm setting error-reports --ask       # ask once per `omm contribute` run
omm setting error-reports --enable    # always send, no prompt
omm setting error-reports --disable   # never send (also drops anything queued)
omm contribute --report-errors        # this run only; your saved setting is unchanged
```

## Where they go, and why not the telemetry node

Benchmark telemetry reaches the `/telemetry` node of the project's Firebase
Realtime Database through a proof-of-work-gated gateway. The node is
**publicly readable** - that is how the
recommendation model gets retrained from community data, and it is safe
precisely because a v8 row is nothing but anonymous numbers
(`docs/telemetry-v8.md`).

Error text is a different risk class. Even after scrubbing, an error message
is free-form text written by some third-party library, and the honest
assumption is that one of them will eventually put something unfortunate in
one. So error reports go to a **separate, write-only** node,
`/error_reports`. Clients submit to the gateway's `/error-report` route,
which binds proof-of-work to the exact payload, rejects replays, validates
the field allow-list, and performs a create-only database write. Direct
Firebase client writes and all reads are denied (`database.rules.json`).

For the hosted telemetry gateway, omm selects its paired `/error-report`
route. For a self-hosted collector, the endpoint is derived from the existing
`telemetry_endpoint` by replacing the last path segment (`.../telemetry` ->
`.../error_reports`). There is no second URL to configure. Legacy direct
Firebase writes are not attempted, and no supported endpoint means error
reporting is unavailable.

## What is sent

| Field | Example | Notes |
| --- | --- | --- |
| `schema_version` | `1` | Bumped if this shape ever changes. |
| `error_type` | `QualityEvaluationError` | The exception class name only. |
| `error_message` | `Ollama /api/generate request failed` | Scrubbed, capped at 2000 characters. |
| `trigger` | `install_quality_eval` | One of three fixed values (below). |
| `subcommand` | `search` | Crash reports only, and only a name omm itself registers. |
| `client_version` | `0.1.23` | The omm version. |
| `os_name` / `os_version` | `Windows` / `11` | `platform.system()` / `platform.release()`. |
| `cpu_arch` | `x86_64` | Architecture, never the CPU's marketing name. |
| `cpu_score` / `cpu_tier` | `5600` / `0` | The same anonymous chip scores v8 telemetry sends, from the same parser. |
| `gpu_score` / `gpu_tier` | `4090` / `0` | Absent entirely on a machine with no detected GPU. |
| `catalog_ref` | `unsloth/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M.gguf` | Catalog coordinates - `repo_id:filename`, never a local path. |
| `engine` | `ollama` | Which runner was in use. |
| `recorded_at` | `2026-08-19T12:00:00+00:00` | UTC timestamp. |

All of `cpu_arch`, `cpu_score`, `cpu_tier`, `gpu_score`, and `gpu_tier` are
optional, and are filled in only when the failing command had already
scanned the hardware for its own purposes (`omm contribute` always has). omm
never starts a hardware scan just to decorate a report - that scan takes
seconds, and no one should wait for it after a crash.

The gateway rejects any field not on that list, so a future bug that starts
attaching something new fails at the server instead of quietly collecting it.

## What is never sent

- **Your username, and any absolute path.** `/Users/<name>/...`,
  `/home/<name>/...`, and `C:\Users\<name>\...` are rewritten to `~` by
  `error_report.scrub_paths()` before a message is queued, previewed, or
  uploaded. Model references are catalog coordinates, never the file on
  your disk.
- **Tracebacks.** The traceback of a crash stays in your terminal, where
  you need it. It is the single richest source of local paths and library
  internals, and it is not worth the exposure; the exception type, message,
  and which subcommand crashed are what triage actually starts from.
- **Your command line.** Not the arguments, not the flags - only the
  subcommand name, and only when it matches a command omm registers, so a
  search query or a URL can never be echoed back as a "subcommand".
- **Environment variables**, including `OMM_HOME`.
- **IP addresses or hostnames** collected by omm. (As with any HTTP
  request, the receiving server sees the connection's source address; omm
  does not put it in the payload.)
- **Generated model text, prompts, or quality-pack answers.** Same rule
  telemetry has always followed.
- **Raw CPU/GPU names.** Only the ordinal scores, exactly as in v8
  telemetry.

## The three places a report comes from

1. `install_quality_eval` - `omm contribute` caught a
   `QualityEvaluationError` for one candidate and moved on to the next.
   This is the common "this GGUF never worked on this machine" case.
2. `daemon_restart_giveup` - the engine daemon crashed mid-benchmark and
   could not be restarted, so the candidate was abandoned.
3. `crash` - an unhandled exception escaped any omm command, not just
   `contribute`. The traceback still prints exactly as before; the report is
   a side effect, never a replacement for it.

Triggers 1 and 2 exist only in `omm contribute`, because "catch and keep
going" is the unattended loop's control flow. Trigger 3 is the top-level
handler that already wraps every command.

## When a report is actually sent

Never from the failing code path. A report is written to a local queue
(`~/.omm/error_reports_pending.json`) and sent later:

- `always` - the queue is flushed at the start of the next omm command.
- `ask` - the queue waits for the next `omm contribute`, which asks once,
  before the loop starts, showing you the exact JSON it would send. A crash
  never triggers a prompt of its own; being asked a question immediately
  after omm blew up is not a good moment for anyone.
- `never` - nothing is queued at all, and `omm setting error-reports
  --disable` deletes whatever was already queued.

`omm contribute --report-errors` grants consent for one run without saving
anything. If you have explicitly turned error reports off, the flag is
ignored with a message pointing at `omm setting error-reports --ask` - an
opt-out is not something a runtime flag gets to override.

Every decision this makes - queued, skipped, sent, failed - is appended to
`~/.omm/error_reports.log` on your machine, so "did omm send something?" is
always answerable locally.
