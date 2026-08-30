# Privacy

`omm` runs entirely on your machine. Nothing about your use of it leaves your
computer unless you turn on one of the three outbound channels below. Every one
is **off or "ask" by default**, each is controlled separately, and each can be
turned off at any time.

Turn everything off:

```sh
omm setting upload benchmark --disable
omm setting upload usage --disable
omm setting upload crash --disable
omm setting telemetry --endpoint none
```

All three channels share one gateway (a Cloudflare Worker) that accepts writes
only after a small proof-of-work and forwards them to a Firebase Realtime
Database. Requests are not authenticated and the gateway does not log client IP
addresses.

---

## 1. Benchmark telemetry — `omm setting upload benchmark`

**When:** after `omm install`'s optional speed check, and during `omm contribute`.
**Default:** ask each time.
**Purpose:** train the `omm recommend` model that predicts tokens/sec for a
given model on given hardware.

**Sent:** measured tokens/sec and the run parameters, model parameter counts and
quantisation, engine and engine version, and a hardware profile (CPU model and
core counts, a CPU benchmark score, RAM size, unified-memory flag). The full,
schema-enforced field list is `database.rules.json` under the `telemetry` node.
This node is world-readable — it is the training data.

**Never sent:** file paths, usernames, your search queries, or any generated
model text.

---

## 2. Anonymous usage statistics — `omm setting upload usage`

**When:** at most once per day, as a single background batch on your next `omm`
command.
**Default:** off. Enabled only by an explicit "yes" during `omm setup`, or
`omm setting upload usage --enable`.
**Purpose:** tell the maintainers which versions and hardware to support and
which commands are failing in the field.

**Sent — exactly these fields:**

| field | example | notes |
|---|---|---|
| `client_id` | `9f2c…` (32 hex chars) | A random id generated once and stored in `~/.omm/client-id`. Not derived from anything about you or your machine. Reset it — breaking any link to past rows — with `omm setting upload usage --reset-id`. |
| `client_version` | `0.3.11` | the installed `omm` version |
| `install_source` | `pipx` | one of `pipx`, `homebrew`, `npm`, `pypi`, `winget`, `git`, `unknown` |
| `os_name` / `os_version` | `Darwin` / `23.5.0` | |
| `cpu_arch` | `arm64` | |
| `ram_gb_bucket` | `16-32` | a **range**, never the exact amount: `<8`, `8-16`, `16-32`, `32-64`, `64-128`, `128+` |
| `vram_gb_bucket` | `8-12` | `none`, `<4`, `4-8`, `8-12`, `12-16`, `16-24`, `24+` |
| `gpu_vendor` | `apple` | vendor class only, never the GPU model: `apple`, `nvidia`, `amd`, `intel`, `other`, `none` |
| `update_channel` | `stable` | `stable` or `beta` |
| `recorded_at` | ISO-8601 UTC timestamp | |
| `commands` | `{"install ok": 3, "search ok": 5}` | a count of `<command> <outcome>` since the last batch. `<outcome>` is one of `ok`, `failed`, `usage-error`, `interrupted`. |
| `errors` | `{"install DownloadError": 1}` | for failed runs only: a count of `<command> <exception-class-name>`. The **class name only** — never the exception message. |

**Never sent:** model names, search terms, repo ids, file paths, your IP address,
hostname, username, environment variables, or any command arguments.

See the exact payload that would be sent next:

```sh
omm setting upload usage
```

The `usage` Realtime Database node is **not** publicly readable.

---

## 3. Crash reports — `omm setting upload crash`

**When:** after an unhandled error, sent (with a prompt) on your next
`omm contribute`.
**Default:** off.
**Purpose:** surface crashes that happen on machines the maintainers cannot see.

**Sent:** the exception type, a path-scrubbed exception message, which
subcommand was running, the `omm` version, OS name/version, CPU architecture,
and coarse CPU/GPU benchmark tiers. The full allow-list and the scrubbing rules
are in [`docs/crash-reports.md`](docs/crash-reports.md).

**Never sent:** tracebacks, absolute paths, usernames, environment variables,
the command line, or generated model text.

The `error_reports` Realtime Database node is **not** publicly readable.

---

## Local files (never uploaded)

`omm` keeps a local run log at `~/.omm/logs/` (one JSONL file per command plus a
human-readable `history.log`), readable with `omm log`. It is for your own
debugging and is never sent anywhere; the channels above do not read it.
