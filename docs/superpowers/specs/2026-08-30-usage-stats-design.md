# Anonymous usage statistics — design

Date: 2026-08-30
Status: implemented on `beta` (plan: `docs/superpowers/plans/2026-08-30-usage-stats.md`)
Branch target: `beta`
Prior art: this is "sub-project 2" from `2026-08-30-local-run-logging-design.md`.

## Goal

Let the maintainers answer, at a coarse population level:

- How many installs exist, on which `omm` version, from which install source.
- What hardware `omm` runs on (feeds the recommend model, which already needs this shape).
- Which commands people run, and which ones fail (find broken flows).

Strictly opt-in. Off by default. One clearly-worded consent step in `omm setup`.

## Non-goals

- Per-event real-time delivery. Batched, ~once/day.
- Any PII: no model names, search queries, repo ids, file paths, IP retention, hostname, username.
- A server-side dashboard or aggregation pipeline. Query Firebase directly for now.
- Reading the local run log (`runlog.py`). Usage events are **purpose-built** and constructed explicitly — never a filtered dump of another file. The two subsystems stay independent.
- Deploying the Worker (`wrangler deploy`) — code + tests only.

## What is collected

One **snapshot** per flush (daily), plus a small **command tally** since the last flush.

### Snapshot (identity + environment)

| field | example | source |
|---|---|---|
| `schema_version` | `1` | constant |
| `client_id` | `9f2c…` (uuid4 hex) | `~/.omm/client-id`, generated once |
| `client_version` | `"0.3.11"` | `package_metadata.version()` |
| `install_source` | `"pipx"` \| `"homebrew"` \| `"npm"` \| `"pypi"` \| `"git"` \| `"unknown"` | `package_metadata.install_source()` |
| `os_name` | `"Darwin"` | `platform.system()` |
| `os_version` | `"14.5"` | `platform.release()` truncated |
| `cpu_arch` | `"arm64"` | `platform.machine()` |
| `ram_gb_bucket` | `"16-32"` | bucketed from `hardware` (never the exact number) |
| `vram_gb_bucket` | `"8-12"` \| `"none"` | bucketed |
| `gpu_vendor` | `"apple"` \| `"nvidia"` \| `"amd"` \| `"intel"` \| `"none"` | coarse classification of `gpu_name` — never the model string |
| `recorded_at` | ISO-8601 UTC | `datetime.now` |
| `update_channel` | `"stable"` \| `"beta"` | config |

Buckets: RAM `<8, 8-16, 16-32, 32-64, 64-128, 128+`; VRAM `none, <4, 4-8, 8-12, 12-16, 16-24, 24+`.

### Command tally

`commands`: an object mapping `"<command> <outcome>"` → count, e.g.

```json
{ "commands": { "install ok": 3, "install failed": 1, "search ok": 5, "recommend ok": 1 } }
```

- `<command>` is a registered subcommand name only (same resolver as `runlog.subcommand_of`), or `"bare"` / `"unknown"`.
- `<outcome>` is `ok` / `failed` / `usage-error` / `interrupted` (the same four `runlog.finish` uses).
- Plus `errors`: `"<command> <error_class>"` → count, where `error_class` is the exception class name only (`DownloadError`, `LinkError`, …), never the message. Present only for `failed` outcomes.

The tally is capped: at most 100 distinct keys per flush; an over-cap key is dropped (not truncated). A single command run contributes exactly one `commands` increment and at most one `errors` increment.

## Architecture

Mirrors `omm/error_report.py` deliberately — same queue-and-flush shape, so there is one pattern to reason about. New file `omm/usage.py`.

### Client id — `omm/config.py`

```python
CLIENT_ID_PATH = OMM_HOME / "client-id"

def client_id() -> str:
    """Stable random per-install id (uuid4 hex). Created on first read.
    Lives in its own file, never config.json — config is copied/shared;
    this must not travel with it. Best-effort: a read/write failure
    returns a fresh ephemeral id rather than raising."""
```

Reset: `omm setting upload usage --reset-id` deletes the file; the next flush makes a new one, breaking linkage to prior rows.

### Collection — `omm/usage.py`

- `record_run(subcommand: str, outcome: str, error_class: str | None) -> None`
  Called once from `cli.main()`'s `finally`, right after `runlog.finish(...)`. Appends `{"c": subcommand, "o": outcome, "e": error_class}` to `~/.omm/usage-pending.json` (atomic, `locked`, capped at 5000 rows). Swallows all errors. **No network.** No-op when policy is not `enabled`.
- `_aggregate(rows) -> dict` — fold pending rows into the `commands` / `errors` tally with the 100-key cap.
- `build_payload() -> dict` — snapshot + aggregated tally. Used by both the sender and the `omm setting upload usage` preview.
- `flush_pending() -> bool` — if policy `enabled` and ≥ `_FLUSH_INTERVAL` (24h, tracked in `~/.omm/usage-state.json`) since the last successful send: build payload, PoW-sign, POST to `usage_endpoint`, and on 2xx clear the pending rows + stamp the state file. One POST per call. Backoff on failure mirrors `error_report` (`usage-backoff.json`). Swallows all errors, returns whether it sent.
- `_post(payload)` — `from omm.telemetry import _solve_proof_of_work`; POST `{event_json, timestamp, nonce}` exactly like `telemetry._post_event`'s gateway branch.
- `log_attempt(...)` → `~/.omm/usage.log` (JSONL, capped — same helper shape as telemetry).

Config key `usage_stats_policy`: `None` (unset ⇒ effective **never**, opt-in) or `"enabled"`. Stored `None` vs explicit — matches `error_report_send_policy` semantics so a runtime override can't flip an explicit opt-out. There is no `"ask"`: a daily background batch has no interaction point.

### Wiring — `omm/cli.py`

- Root callback (`_root`), in the same block that calls `telemetry.flush_pending()` / `error_report.flush_pending()` (guarded by `ctx.invoked_subcommand != "setting"`): add `usage.flush_pending()`. No new network path — that block already does network.
- `main()` `finally`: after `runlog.finish(exit_code, outcome)`, call
  `usage.record_run(runlog.subcommand_of(sys.argv[1:]), outcome, _exc_class_name)`
  where `_exc_class_name` is set in the `except Exception as e` branch to `type(e).__name__` (else `None`).

### Endpoint — `omm/config.py`

```python
USAGE_GATEWAY_ENDPOINT = "https://omm-telemetry-gateway.seong381400.workers.dev/usage"
DEFAULT_CONFIG["usage_stats_policy"] = None
```

Not derived from `telemetry_endpoint`. Usage always goes upstream; a self-hoster who clears `telemetry_endpoint` still sends nothing because the policy defaults off, and can leave it off. `_post` refuses any endpoint that is not `USAGE_GATEWAY_ENDPOINT` (no arbitrary destination for this stream).

## `omm setting upload` — command group

Replace the two flat commands `omm setting upload` and `omm setting error-reports` with a group:

```
omm setting telemetry --endpoint ...              # unchanged — destination for self-hosted benchmark/crash
omm setting upload                                # no subcommand: print all three policies
omm setting upload benchmark --enable/--disable/--ask
omm setting upload usage     --enable/--disable
omm setting upload crash     --enable/--disable/--ask   [--reset-id lives on `usage`, not here]
```

- `benchmark` = today's `configure_upload` body verbatim (config key `telemetry_send_policy`).
- `crash` = today's `configure_error_reports` body verbatim (config key `error_report_send_policy`). The word shrinks `error-reports` → `crash`; the module `error_report.py`, the config key, and the RTDB node are unchanged.
- `usage` = new. `--enable` sets `usage_stats_policy="enabled"`; `--disable` sets `None` **and** discards `usage-pending.json`. Bare `omm setting upload usage` prints: policy, `client_id()`, and the exact `build_payload()` that would next be sent (always a dry-run — no separate flag). `--reset-id` deletes `CLIENT_ID_PATH`.
- Per CLAUDE.md "fully delete the old command — no hidden back-compat aliases": `omm setting error-reports` and the old `omm setting upload` (as a leaf) stop existing. `_merge_config` already preserves the underlying config keys, so saved user choices survive the command rename.
- Update `_HELP_ALL_GROUPS`, the `omm setup` closing text, and `run_wizard`'s trailing note to the new paths.

Nesting is `app → setting_app → upload_app` (a third `typer.Typer()` with a callback that prints the summary when invoked bare). This is the deepest nesting in the CLI; `omm setting upload usage` still reads cleanly.

## `omm setup` consent step

New step in `onboarding.run_wizard`, after the engine checklist, before `run_completion_step`. Its own function `run_data_sharing_step(console) -> None`.

```
┌─ Help improve omm ─────────────────────────────────────────────┐
│ omm can send anonymous usage data so we know which versions    │
│ and hardware to support, and which commands are breaking.      │
│                                                                │
│ If you say yes, each day omm sends ONE batch containing:       │
│   • a random id (not tied to you — reset with                  │
│     `omm setting upload usage --reset-id`)                     │
│   • omm version, install method, OS, CPU architecture          │
│   • RAM / VRAM size range, GPU vendor                          │
│   • which commands you ran and whether they succeeded          │
│ It never sends: model names, search terms, file paths,         │
│ your IP, or hostname.                                          │
│                                                                │
│ It also turns on crash reports (asked before each send).       │
│                                                                │
│ Default is OFF. Change any time: `omm setting upload`.         │
│ Details: https://github.com/omm-hippo/omm/blob/main/PRIVACY.md │
└────────────────────────────────────────────────────────────────┘
Send anonymous usage data + crash reports?  [y/N]:
```

- `questionary.confirm(..., default=False)` wrapped in `_add_escape_to_cancel`.
- **Yes** → `usage_stats_policy="enabled"` and `error_report_send_policy="ask"`.
- **No / Escape / Ctrl-C / not a TTY / `--yes`** → touch nothing (both stay at their opt-in-off defaults). The wizard must never enable either without an explicit interactive "yes".
- `omm setup` re-run: same step, prefilled with the current answer as `default`.
- The box text is the single source of truth for what's collected — keep it in sync with `build_payload()` (a test asserts every payload key is represented).

## Cloudflare Worker — `/usage`

Mirror the `/error-report` path exactly.

- `cf-worker/src/index.ts`: `url.pathname === "/usage" ? "usage" : …`; route to `validateUsageEvent`.
- `cf-worker/src/rtdb.ts`: widen the `node` union to `"telemetry" | "error_reports" | "usage"`.
- `cf-worker/src/validate.ts`: `USAGE_FIELDS` allow-list + `validateUsageEvent(event)`:
  - unknown key ⇒ reject.
  - required: `schema_version` (===1), `client_id` (`/^[0-9a-f]{8,64}$/`), `client_version`, `os_name`, `recorded_at`.
  - `install_source` ∈ the 6 literals; `gpu_vendor` ∈ the 5; buckets ∈ their enumerations.
  - `commands` / `errors`: object, ≤100 keys, each key `string` ≤80 chars matching `/^[a-z-]+ [a-z_-]+$/`, each value integer `1..100000`.
  - string length caps like `validateErrorReport`; reject `looksLikePathOrControlChars` on the free-ish strings.
- `cf-worker/test/`: a `usage.test.ts` mirroring `error-report.test.ts` (valid row 200, unknown field 400, bad bucket 400, oversized `commands` 400, PoW reuse 409).
- `database.rules.json`: a `usage` block — `.read: false`, `.write: false`, `$event` `.validate` requiring the same required children, per-field `.validate`, `$other: { ".validate": false }`.
- `scripts/test_firebase_rules.mjs`: add usage accept/reject cases.
- The existing PoW envelope, timestamp-skew check, size caps, and create-only `if-match: null_etag` write all apply unchanged.

## PRIVACY.md (repo root)

One page covering all three outbound channels:

| channel | when | consent | what | where |
|---|---|---|---|---|
| Benchmark telemetry | `omm contribute` / after install benchmark | `omm setting upload benchmark` (asks) | tokens/sec + hardware + model params | RTDB `/telemetry` (world-readable) |
| Crash reports | unhandled error | `omm setting upload crash` (off by default) | exception type + message (scrubbed) + coarse hardware | RTDB `/error_reports` (private) |
| Usage stats | daily batch | `omm setting upload usage` (off by default) | this doc's snapshot + command tally | RTDB `/usage` (private) |

Include the exact usage field list, the bucket definitions, the `client-id` explanation + reset, and "how to turn everything off". Link from README near the existing telemetry mention. Rename `docs/error-reports.md` → `docs/crash-reports.md` and fix inbound links (`onboarding.py`, README, any spec).

## Testing

- `tests/test_usage.py` — mirrors `tests/test_error_report.py`:
  - `record_run` is a no-op when policy unset; appends when `enabled`.
  - `build_payload` shape: all required keys, buckets are strings, no exact RAM number leaks, `commands` capped at 100 keys.
  - error_class is a class name, never a message (feed a `DownloadError("secret path /home/x")`, assert `secret` absent).
  - `flush_pending`: no-op before 24h; one POST after; clears pending + stamps state on 2xx; keeps pending on failure; respects backoff.
  - `_post` refuses a non-gateway endpoint.
  - client id: stable across calls, new after `--reset-id`, ephemeral (no raise) when the dir is unwritable.
  - non-TTY / unset policy ⇒ nothing sent even with pending rows.
- `tests/test_cli_setting_upload.py` — the group: bare prints 3 policies; `benchmark`/`crash` behave exactly as the deleted flat commands' tests did (port those assertions); `usage --enable/--disable`; `usage` bare prints the dry-run payload; `--reset-id`.
- `tests/test_onboarding*.py` — `run_data_sharing_step`: yes sets both keys; no/declined/non-TTY sets neither; re-run uses current value as default. A test asserting every `build_payload()` key appears in the consent box text.
- `cf-worker` vitest + `scripts/test_firebase_rules.mjs` as above.
- Full `pytest`, `npm --prefix packaging/npm/launcher test` unaffected.

## Migration / compatibility

- New config key defaults `None`; `_merge_config` needs no special case (absent ⇒ `None` ⇒ off).
- Deleting `omm setting error-reports` / old `omm setting upload` leaf: users who scripted them get a "no such command" — acceptable per the no-alias rule; call it out in the PR description and CHANGELOG.
- Already-installed clients keep working: they simply never call `/usage`. No server change breaks them (new node, new route).
- `benchmark_version` is untouched; `/usage` has its own `schema_version` starting at 1.

## Future (separate specs)

- Server-side rollups / a public "omm by the numbers" page.
- `omm setting upload` retention/interval knobs if 24h proves wrong.
