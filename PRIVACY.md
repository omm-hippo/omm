# Privacy

`omm` manages models and calls local AI runners on your machine. Searching
model providers, downloading models or runners, refreshing recommendation
data, and checking for updates use network requests independently of the
four data-sharing channels below. Disabling uploads does not disable those
requests.

Benchmark telemetry, usage statistics, crash reports, and battle votes are each
**off or "ask" by default**, controlled separately, and can be turned off at any
time.

Turn off all four data-sharing channels:

```sh
omm setting upload benchmark --disable
omm setting upload usage --disable
omm setting upload crash --disable
omm setting upload votes --disable
omm setting telemetry --endpoint none
```

All four channels share one gateway (a Cloudflare Worker) that accepts writes
only after a small proof-of-work and forwards them to a Firebase Realtime
Database. Uploads do not require a user account. The gateway code does not add
client IP addresses to these records; network requests still expose an IP
address to the destination and its infrastructure.

---

## 1. Benchmark telemetry — `omm setting upload benchmark`

**When:** after `omm install`'s optional speed check, `omm benchmark`, and during
`omm contribute`, subject to the benchmark-upload policy.
**Default:** ask each time.
**Purpose:** train the `omm recommend` model that predicts tokens/sec for a
given model on given hardware.

**Sent:** model identifiers and available source/file metadata (including
repository id, filename, size, or digest), measured tokens/sec, quality
summaries, model parameter counts and quantisation, engine/version, and
hardware characteristics. Current v8/v9 records use locally computed CPU/GPU
scores and tiers instead of raw CPU/GPU model names, alongside architecture,
core counts, RAM/VRAM, and the unified-memory flag. Where available, v9 adds
memory estimates, observed memory pressure, host CPU load, and measurement
quality. Ollama runtime settings are included when known; LM Studio records
omit settings that omm cannot observe. Older stored records can still contain
the CPU model field used before v8.

The full allow-list is enforced by
[`cf-worker/src/validate.ts`](cf-worker/src/validate.ts), with the corresponding
schema in [`database.rules.json`](database.rules.json) under `telemetry`.
**This node is publicly readable** — it is the training data. Benchmark
records can identify the model you tested; the separate usage channel below
does not send model names.

**Never sent:** file paths, usernames, your search queries, or any generated
model text.

---

## 2. Anonymous usage statistics — `omm setting upload usage`

**When:** at most once per day, as a single background batch on your next `omm`
command.
**Default:** off. Enabled only by an explicit "yes" during `omm setup`, the
one-time prompt shown after an `omm update` that crosses the version this was
added in, or `omm setting upload usage --enable`. A "no" at any of those is
remembered — you are not asked again.
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

**Sent:** the exception type, a scrubbed exception message, which
subcommand was running, the `omm` version, OS name/version, CPU architecture,
and coarse CPU/GPU benchmark tiers. The full allow-list and the scrubbing rules
are in [`docs/crash-reports.md`](docs/crash-reports.md).

**Removed from the message before it is queued, previewed, or sent:**
your home directory (`/Users/<name>`, `/home/<name>`, `C:\Users\<name>` become
`~`; folders below it stay visible), the directories of any other absolute
path (only the file name is kept), and credential-shaped text: URL
user/password (`https://user:pass@`), `Authorization:` header values,
`Bearer ...` tokens, values of `token=`, `access_token=`, `api_key=`,
`password=`, `secret=`, `signature=` and similar fields, and Hugging Face
(`hf_...`), GitHub (`ghp_...`, `github_pat_...`), `sk-...`, AWS access key and
JWT-shaped tokens. These are pattern matches, not a guarantee: an exception
message is text from some library, and a secret in an unusual shape could
survive. That is why `ask` shows you the exact JSON before anything is sent.

**Never sent:** tracebacks, environment variables, the command line and its
arguments, anything you typed, or generated model text.

The `error_reports` Realtime Database node is **not** publicly readable.

---

## 4. Battle votes — `omm setting upload votes`

**When:** `omm arena` asks once, at the end of a battle session, whether to
upload that session's votes. Nothing is queued before you answer, and the
upload itself happens on your next `omm` command.
**Default:** ask.
**Purpose:** build a shared leaderboard of which local models people actually
prefer, alongside how fast and how large they were on real hardware.

**Sent,** one record per vote: which side won (`a`, `b`, or `both_bad`); for
each of the two models its provider, repository id, filename, GGUF SHA-256 and
quantization bit width, as far as omm knows them; each side's elapsed time,
token count, and — when the runner reported them — decode speed and resident
memory; this machine's **memory group** — the unified-memory size of an Apple
Silicon Mac, the GPU VRAM size of a PC, or the RAM size of a machine with no
GPU, as a marketed-size bucket such as `16GB` or `20–24GB` (never the exact
amount, the GPU or CPU model); a random per-install id (the same one usage
statistics use, `~/.omm/client-id`); the `omm` version; and the timestamp.

The memory group is what lets the leaderboard show how people with machines
like yours rated each model. A Linux machine whose GPU omm cannot detect sends
no group rather than a wrong one, and its votes count only toward the
all-machines ranking. Usage statistics (§2) already send a bucketed RAM/VRAM
size with the same per-install id, so this adds little that is new. If you
had already chosen "always" before the group was added, you are not asked
again; `omm setting upload votes --disable` turns the channel off.

**Never sent: the prompt you typed, and nothing derived from it** — no hash, no
length, no category. It stays in `~/.omm/arena/votes.jsonl` on your machine and
is not part of the payload; the gateway rejects any record that carries a
`prompt` field at all. Also never sent: the models' responses, model file paths,
the rest of your hardware specification, or anything about which models you have installed
beyond the two that fought.

The `votes` Realtime Database node is **not** publicly readable — unlike
benchmark telemetry, which is. A vote record carries the per-install id, so
letting anyone collect one machine's voting history has no upside.

---

## Local files (never uploaded)

`omm` keeps a local run log at `~/.omm/logs/` (one JSONL file per command plus a
human-readable `history.log`), readable with `omm log`. It is for your own
debugging and is never sent anywhere; the channels above do not read it.

`omm evaluate` runs model-generated Python only in Docker or Podman and never
uses the benchmark-upload channel. Its optional `--output` evidence contains
model/package identity, pack identity, task outcomes, and counts, but no
generated source or model response. `omm compare` is read-only and sends no
comparison or model-list data.

**Auto-import** (`omm setting auto-import enable`, off by default) watches local AI
app directories on this machine and adopts new models into the omm hub in the
background. It is a local filesystem automation only - nothing it does is uploaded
or sent anywhere.

`omm bug-report` builds a separate local diagnostic document from a new allow-list. By
default it includes OMM version, install source, a command name, exception
class, and diagnostic status/counts. It excludes usernames, home/personal
paths, tokens, environment variables, search text, command arguments,
generated text, and model names. Optional OS, upload-policy, check-name, and stage-count
groups are included only when selected with `--include`. The full JSON is
printed before `--save`; the command never uploads it, opens a GitHub issue,
or sends a message.

`--include stages` counts setup, recommendation, installation, download and link
outcomes in at most 50 recent local logs. Only fixed stage names, numeric counts
and fixed recovery instructions enter the report. A later success is labelled
as a later success, not proof that the same failed request recovered. These
local summaries are never read by the usage, crash or benchmark senders.
Usage tallies accept only registered command names and known outcomes; malformed
pending records become `unknown`. Only known exception identifiers appear;
other values are grouped as `OtherError`, and exception messages are excluded.

## Local GUI chat history

The local manager's chat screen sends questions and completed conversation turns
only to the selected loopback Ollama or LM Studio API. It does not use a cloud AI
fallback and does not upload prompts or responses through telemetry. UTF-8 text
history is stored under `OMM_HOME/web-chats`. Interrupted and failed partial
answers stay visible locally but are not used in later inference context.
The GUI provides JSON export and confirmed deletion of closed conversations.
Explicit data removal also removes `web-chats`; ordinary updates retain it.

### Shared AI in the local web manager

The **공유 AI** screen is an explicit connection to a private office or Tailscale
gateway, separate from local chat and `omm rag remote`. The local browser sends
the address and employee token to OMM's same-origin, loopback-only API. Only an
explicit connection check sends `GET /v1/models`; only sending a question sends
`POST /v1/chat/completions` with the selected model and recent conversation turns.
An entry in the model catalog is not proof that a model is running. OMM does not
call model installation, restart, unload, or cluster administration routes.

The address, token, last catalog and last successfully used model are stored in
`OMM_HOME/shared-ai/connection.json` (mode 0600, directory 0700 on POSIX). The
token is never returned by the local API or stored in browser localStorage.
Chat text and bounded duplicate-request results stay in process/browser memory;
refreshing clears the visible conversation. These values are not added to run
logs, telemetry or crash reports. Removing the saved connection deletes the
local credentials; it does not revoke the server's token.

Gateway requests bypass proxy environment variables, reject redirects, validate
and pin private destination addresses, and keep normal HTTPS certificate
verification. No cloud fallback or automatic retry is used. Plain HTTP relies
on the office network or Tailscale for transport protection. OMM never enables
Tailscale or changes network/firewall settings from this screen.

### Local document questions and `omm rag`

The loopback web manager and `omm rag` store imported document text, content
hashes, collection names, a lexical search index, semantic vectors (embeddings),
questions, answers and quotations only in `OMM_HOME/documents`.
The web page reads only browser-selected files; a web request cannot name an
arbitrary server filesystem path. `omm rag add` reads only the files and folders
you name on the command line.

Extraction and lexical search make no network calls. Semantic search sends
document chunks (when indexing) and your question (when searching) only to the
loopback Ollama origin configured with `omm rag embedding-model set`
(default `http://127.0.0.1:11434`; non-loopback addresses are refused), and asks
Ollama to unload the embedding model afterwards. If Ollama or the embedding
model is unavailable, search falls back to lexical matching and says so; nothing
is sent elsewhere instead. Questions for an answer send the selected quotations
to the explicitly selected loopback local runner. Document text, questions,
quotations and answers are never sent to OMM telemetry, usage, crash or vote
uploads (an unexpected error inside `omm rag` is never queued as a crash
report), and are not written to run logs (`OMM_HOME/logs` records only the
local Ollama URL path and HTTP status). No cloud embedding API is used. Ollama
is a separate program: use a local model, not a runner configured to forward
requests to a cloud service.

Replacing or deleting a document (or a collection) removes its text, vectors and
saved document answers so old source quotations do not remain in the answer
cache. Original files are not changed. Explicit OMM data removal includes the
owned `documents` directory.

### Document search for coding agents (MCP)

`omm rag mcp` answers a coding agent's search requests over stdin/stdout only;
it opens no network port and makes no network calls. Questions and returned
quotations are not written to run logs, telemetry, usage counts or crash
reports; its stderr diagnostics are fixed status lines. The quotations it
returns go to the coding agent, which sends them to whatever model that agent
is configured to use: a cloud model receives them unless the agent is routed to
a local model. `omm rag connect` keeps a private copy of the agent's original
configuration file under `OMM_HOME/rag-connections`; that copy may contain the
agent's own settings and is not read by any upload channel. See
[document search for coding agents](docs/rag-mcp.md).

### Shared company documents on the cluster head

When an employee PC runs `omm rag remote set <head>`, `omm rag` collection, add, remove,
search, share and unshare commands send their data to the office cluster head instead of
storing it on that PC: uploaded files (whole file bytes, at most 6 MiB each), collection
and document names, questions, and share targets go to the head's gateway (port 52440,
HTTPS, the same channel as the cluster inference routes below). Original text, chunks and vectors are kept in the head's
`OMM_HOME/documents`, and embeddings run on the head's own loopback Ollama. Every other Mac
in the cluster keeps a full backup copy of that store in `OMM_HOME/cluster/rag-backup/`
(mode 0600), refreshed about once a minute over the gateway's HTTPS port, preferring the
Thunderbolt link. Only Macs holding the cluster name can fetch it, and `omm cluster leave`
deletes the copy. The copy is never searched or served to employees; it exists so that
`omm cluster handover` can move the documents to a new head. The employee
PC keeps only the head address and the employee token in `OMM_HOME/rag-remote.json`
(mode 0600 on macOS/Linux), plus the head's public CA certificate it pinned in
`OMM_HOME/rag-remote-ca.pem`; the token is never printed. These requests ignore
`HTTP(S)_PROXY`. `omm rag connect <harness> --remote` writes the token into the
harness's own configuration file as an `Authorization` header (tightened to mode 0600
on macOS/Linux) and into the private copy under `OMM_HOME/rag-connections`; do not
share those files. The gateway's request log records the path (for example
`/rag/v1/search`) but never collection or document names, questions, quotations or
document text, which travel only in the query string, headers or body. Who may see
which collection (owner, shares, teams) is stored next to the documents on the head.

The **내 문서** tab of `omm web` does the same through the loopback web server: the
browser sends the head address and the token once to `127.0.0.1` when you press
회사에 연결, and the server keeps the token only in `OMM_HOME/rag-remote.json`. No
page, API response, error message or job record under `OMM_HOME/web-jobs` contains it.
A file uploaded to the head passes from the browser to the local server and its worker
process only in memory (job records keep the file name, collection and head address,
not the content) and then goes to the head as above. On a cluster head, the tab's
employee-token panel uses the same `OMM_HOME/cluster/tokens.json` as
`omm cluster token`; a new token is shown once in the browser and only its digest is
stored.

## Local agent connection backups

`omm setting agent local` saves a private local backup of the selected
harness's routing settings under `OMM_HOME/agent-connections`. It may contain
existing authentication values from those settings. Backups are not read by
telemetry, crash reports, usage uploads or support bundles; do not share them
in an issue. Model capability checks contact only the selected loopback Ollama
endpoint and do not generate a prompt. A harness's own telemetry, tool network
access and project/organization overrides remain that harness's settings.
`omm setting agent cloud` removes the connection backup after restoring all
files successfully. See [agent connections](docs/agent-connections.md).

## Managed installation security checks

Runner install/update uses mandatory source/integrity/native checks. Their
timestamped outcomes are stored locally under `OMM_HOME/security-checks` and
shown in the connections view and engine status JSON. These records contain
engine/platform identifiers and named-check outcomes, not native error bodies,
document contents or file paths. OMM upload channels do not read them.
Native Gatekeeper/Defender/ClamAV checks remain subject to their own operating
system/vendor policies; OMM does not change those settings. See
[mandatory installation checks](docs/mandatory-install-safety.md) for scope and
failure behavior. A successful check is not a universal vulnerability guarantee.

## Office cluster (`omm cluster`)

`omm cluster` joins your own Macs into one exo cluster inside your office. It adds no
upload channel: prompts, responses, and model output stay on the cluster Macs and the
employee PCs that send them.

- **The gateway** (`omm cluster init`, head Mac, port 52440) checks a per-employee token,
  then forwards only the inference routes to exo on loopback (and answers the shared
  document routes `/mcp` and `/rag/v1/...` itself, see above). It logs only the method,
  path, status, token name, and duration of each request. It never logs a request or
  response body or the token itself. It speaks HTTPS only, with a certificate from a CA
  the head creates for itself (`OMM_HOME/cluster/tls/`, private keys mode 0600; the CA
  key never leaves the head). A plain HTTP request is answered with a pointer to https
  and closed without being read. The first `omm rag remote set` on an employee PC shows
  that CA's SHA-256 fingerprint; the employee compares it with what `omm cluster status`
  or `omm cluster token add` shows on the head, and the PC then trusts only that CA for
  the gateway. A different certificate is refused. Comparing the fingerprint is what
  stops a machine on the office network from posing as the head the first time.
- **Tokens** (`omm cluster token add`) are shown once. Only their SHA-256 digests are
  stored, in `OMM_HOME/cluster/tokens.json` (mode 0600). Revoking a token takes effect on
  the gateway's next request.
- **Firewall.** Each cluster Mac gets a pf anchor (`com.apple/omm-cluster`) and a boot-time
  loader (`/Library/LaunchDaemons/com.omm.cluster-pf.plist`). Together they keep exo's
  unauthenticated ports, and every TCP port from 49153 to 65535, unreachable except over
  loopback and Thunderbolt. That port range is closed to the office network for every
  app on the Mac, not only exo, so a cluster Mac should not also run servers employees
  reach on those ports. `omm cluster leave` removes both.
- **exo** downloads models from Hugging Face by itself. omm stages a copy of exo's runtime
  in `OMM_HOME/cluster/exo-runtime` and runs it as a LaunchAgent. omm does not read or
  send exo's model files.
