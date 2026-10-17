# Arena vote upload channel (sub-project B) design

## Context

Sub-project B of the arena roadmap (issue #408). Sub-project A landed
`omm arena` — blind local model battles writing one row per vote to
`~/.omm/arena/votes.jsonl`, with no network access at all
(`docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md`, branch
`feat/arena-battle-cli`).

This document specifies **B only**: a fourth opt-in outbound channel that
ships those votes to the existing Cloudflare Worker PoW gateway and into a
new Firebase RTDB node. It does not aggregate, rank, or display anything —
that is C (#409) and D (#410).

## Goal

Get enough vote data off individual machines and into one place that
sub-project C can fit a Bradley-Terry model over it, without ever sending
the text a user typed and without any channel becoming non-opt-in.

## Decisions fixed before implementation

Each of these was decided with the user on 2026-09-26. They are not open
questions and the implementation must not reopen them.

1. **The user's prompt text is never uploaded.** No `prompt` field, and no
   field derived from it — no hash, no length bucket, no category. The raw
   prompt stays in the local `votes.jsonl` and goes nowhere else. This is
   the one field in the arena schema that can carry private or
   company-internal content, and no existing omm channel uploads free user
   input (`telemetry` sends hardware and speed numbers, `usage` sends
   command tallies, `error_reports` sends exception types).

   Consequence, accepted: the server cannot tell what a battle was about,
   so C can only produce one overall leaderboard, not per-category ones. If
   per-category ranking is wanted later, it arrives as its own issue that
   adds an explicit category question to the CLI — not as a guess baked
   into this schema now.

2. **Uploads are queued locally and flushed on the next `omm` run.** No
   network call happens during a battle session. `cli.py:_root` already
   brackets every invocation with a flush pass over
   `telemetry`/`error_report`/`usage`; votes join it.

3. **Model identity reuses the `telemetry` channel's field names** —
   `model_provider`, `model_repo_id`, `model_filename`, `model_digest`
   (sha256), `quant_bits` — per side. `database.rules.json` and
   `validate.ts` already define and validate these names for the telemetry
   node, so the validators are a copy rather than a new invention. The
   digest is what makes a row aggregatable across machines even when a user
   renamed the file; `quant_bits` is carried because Q4 and Q8 of one repo
   are different contestants.

4. **Rows carry `client_id`** — the existing random per-install id from
   `~/.omm/client-id` (`config.client_id()`), the same value `usage` sends.
   Without it a single machine can manufacture a leaderboard by voting the
   same pair a few hundred times and C has no way to notice. omm's
   telemetry dataset is already dominated by one contributor machine
   (a known problem), and for votes that failure mode is worse than for
   speed measurements. The cost is that one machine's votes are linkable to
   each other — with no prompt text attached, that reveals preference
   patterns, not content.

5. **A new RTDB node `votes`, private:** `.read: false`, `.write: false`.
   Not world-readable like `telemetry`, because of decision 4 — there is no
   reason to let anyone collect one device's voting history. D's public
   leaderboard reads a C-produced Ed25519-signed artifact, not RTDB, so
   public read access buys nothing.

6. **Consent is asked at the end of an arena session**, `y/n/a`, the way
   the `benchmark` channel already works. The channel is **not** added to
   the `omm setup` data-sharing step: asking someone who has never run
   `omm arena` whether to share battle votes has no context. Policy
   defaults to `"ask"`.

7. **Rows are enqueued after consent, not at vote time.** A queue never
   holds data the user has not agreed to send. A session killed by Ctrl+C
   or a crash therefore contributes nothing to the upload queue; its votes
   are still in the local `votes.jsonl`, which is the user's own record.
   When the policy is already `"enabled"`, enqueueing happens without a
   prompt.

   This moves the consent gate to **enqueue** time rather than flush time.
   Everything in the queue is, by construction, already consented — which
   is what makes a one-time `y` work at all: it queues this session's rows
   while leaving the policy `"ask"`, and a flush that demanded
   `policy() == "enabled"` would strand exactly those rows forever.
   `flush_pending` therefore sends whatever is queued, with one exception:
   if the policy has since become `"disabled"`, it discards the queue
   instead of sending it — a user who turns the channel off means the data
   they consented to earlier should not go either.

8. **The server stores what the client sent, unnormalized.** Two machines
   can report the same model under different filenames (case differences, a
   user-renamed GGUF). Merging those is C's job and is done on
   `model_digest`, which is exact; `model_filename` is display only.
   Normalizing at ingest would destroy the original and leave C unable to
   change its mind later.

## Upload payload

One vote row is one POST. These fields are the whole payload; a key not in
this list is rejected by both the Worker validator and the RTDB rules.

```
schema_version         1
battle_id              uuid4, the value sub-project A already generated;
                       the server's dedup key
client_id              32 hex chars, config.client_id()
client_version         omm version string
recorded_at            ISO 8601 UTC, sub-project A's `timestamp`
engine                 "ollama" | "lmstudio" (a session never mixes engines)
winner                 "a" | "b" | "both_bad"
pinned                 bool, whether `--keep` was active for the round

per side, suffixed _a and _b:
model_provider         string, omitted when the registry has none
model_repo_id          string, omitted when the registry has none
model_filename         string, registry filename
model_digest           64 hex chars (sha256), omitted when unknown
quant_bits             number, omitted when unknown
elapsed                float seconds, wall clock including the model load
tokens                 int
tokens_per_second      float, decode-only; omitted when unmeasurable
memory_gb              float GiB; omitted for LM Studio or on read failure
watt                   float; always omitted in sub-project B
```

**No hardware fields.** Unlike a `telemetry` row, a vote row carries no
`ram_gb`, no `cpu_score`, no GPU information. A's design fixed C's
efficiency axis as *ratios* — tokens/sec divided by the resource actually
used — rather than percentiles within a hardware class, and both numbers
that ratio needs (`tokens_per_second`, `memory_gb`) are already in the row.
C's quality axis uses only `winner`. Nothing else needs a machine spec.

**`watt_a`/`watt_b` are accepted but never sent.** The validator and the
RTDB rules allow them as absent-or-number (0–2000). Power measurement
lands with C, which is its first consumer; leaving the field open now means
C does not need a rules redeploy to start populating it.

**Field omission, not nulls.** A value the client does not have is left out
of the payload entirely rather than sent as `null`, matching `usage.py`'s
`_post_to`, which already strips `None` before signing.

## Architecture

### `src/omm/arena_upload.py` (new, ~300 lines)

Modeled on `usage.py`. All four paths resolve `config.OMM_HOME` at call
time, never as an import-time constant.

```
~/.omm/arena/votes.jsonl           sub-project A's local record; this module
                                   only ever reads it, never writes it
~/.omm/arena/votes-pending.jsonl   the upload queue
~/.omm/arena/votes-backoff.json    retry suppression after a failure
~/.omm/arena/votes-upload.log      local-only attempt log, 500 lines max
```

Public surface:

```python
policy(config_data: dict | None = None) -> str   # "enabled" | "disabled" | "ask"
enqueue(vote_rows: list[dict]) -> int            # local rows -> payloads, queued
                                                 # (callers enqueue only what the
                                                 # user consented to; see 7)
pending_count() -> int
discard_pending() -> int
flush_pending(force: bool = False) -> int        # rows sent
```

`enqueue` reads the registry to fill the model-identity fields
(`model_provider`, `model_repo_id`, `model_digest`, `quant_bits`) — the
filename is already in A's row — and makes no network call.

### Data flow

1. **During a battle** — nothing. `omm arena` stays exactly as offline as
   sub-project A shipped it.
2. **At session end** — if any votes were recorded and the policy is
   `"ask"`, prompt `y/n/a`. `y` queues this session's rows and leaves the
   policy alone; `a` queues them and saves the policy as `"enabled"`; `n`
   queues nothing. When the policy is already `"enabled"`, queue without
   prompting. When it is `"disabled"`, do nothing and say nothing.
3. **On the next `omm` run** — `cli.py:_root`'s existing flush section
   gains one `arena_upload.flush_pending()` call. It returns immediately on
   an empty queue or an active backoff, and discards the queue without
   sending if the policy is now `"disabled"` (see decision 7). Otherwise it
   solves the PoW and POSTs one row at a time, removing only the rows that
   were actually sent, so a row queued by another session mid-flight
   survives (`telemetry._remove_sent_snapshot_entries`).
4. **On failure** — a 6-hour backoff, queue untouched, retried on a later
   run.

### Configuration

`config.py`'s `DEFAULT_CONFIG` gains `arena_vote_send_policy: "ask"`,
validated to the same three values the other policies use. The interactive
`omm setting upload` picker (`cli.py:_upload_channel_menu`) goes from three
channels to four, and `omm setting upload votes --enable/--disable` is
added alongside `benchmark`/`usage`/`crash`.

`config.client_id()`'s docstring says its only caller is `omm.usage`; it
gains a second caller and the docstring must say so.

### Concurrency

`flush_pending` takes the flush lock with `timeout=0`, as `usage.py` does:
two `omm` processes racing must not both post the same queue, and the loser
gives up instantly rather than stalling a user-facing command.

## Worker, rules, validator

**RTDB node** — `database.rules.json` gains a `votes` block with
`.read: false`, `.write: false`, per-field `.validate` entries for every
field above, and `$other: {".validate": false}`. `.write: false` is the
point: clients cannot write to RTDB at all; only the Worker can, with a
rules-bypassing service token. This file is the documented source of truth;
its emulator test proves only that the direct client path is closed.

**Worker route** — `cf-worker/src/index.ts`'s path dispatch (near
`index.ts:115`) gains a fourth entry, `/votes` → node `votes`. PoW
verification, rate limiting, and the service-token write are shared with
the existing three routes and need no new code.

**Validator** — `cf-worker/src/validate.ts` gains `VOTE_FIELDS` and
`validateVoteEvent`. This is what actually enforces the schema at runtime.
It checks:

- `schema_version === 1`
- `client_id` matches `/^[0-9a-f]{8,64}$/` (the `usage` pattern)
- `battle_id` is a uuid4
- `winner` is one of `a`, `b`, `both_bad`
- `engine` is `ollama` or `lmstudio`
- `model_digest_a`/`_b`, when present, match `/^[0-9a-f]{64}$/`
- `model_filename_a`/`_b` pass the existing `safeStr` helper (no path
  separators, no control characters)
- numeric ranges: `elapsed` 0–3600, `tokens` 0–1000000,
  `tokens_per_second` 0–100000, `memory_gb` 0–1024, `watt` 0–2000
- **a `prompt` key is rejected by name.** `KNOWN_FIELDS` and
  `$other: false` already reject it, but this one case gets an explicit
  named check and its own test: it is the core privacy decision of this
  sub-project and must be verifiable by eye.
- **side symmetry** — if any `_a` field is present its `_b` counterpart must
  be too. A half-sided row is not a battle and cannot be aggregated.

**CI gap** — the required checks never run `cf-worker`'s own `npm ci` /
`npm test`. Run `cd cf-worker && npm ci && npm test && npx tsc -p
tsconfig.json` by hand before merging.

**`PRIVACY.md`** gains a section for the fourth channel, stating explicitly
that the prompt text is not transmitted. The `omm setting upload` copy,
`validate.ts`, `database.rules.json`, and `PRIVACY.md` must all say the
same thing.

## Error handling

**An upload problem must never damage a battle.** Every public function in
`arena_upload` swallows its own exceptions, the same contract `runlog.py`
and `usage.py` hold. The vote is already in `votes.jsonl`; uploading is the
optional part.

- `enqueue` failure — log and move on. Nothing about a disk error justifies
  interrupting the session the user just finished.
- `flush_pending` failure — 6-hour backoff, queue kept, retried later.
- Queue cap 5000 rows (`usage._PENDING_MAX`); oldest dropped first.
- A corrupt pending file is moved aside with
  `atomic.backup_corrupt_file` and the queue starts empty.
- Rows appended during a slow PoW-signed POST survive, via the
  snapshot-diff removal above.

## Testing

`tests/test_arena_upload.py`:

- With policy `"disabled"`, `flush_pending` performs **no HTTP at all** and
  the queue is gone afterwards — proven by stubbing `requests.post` to
  raise.
- With policy `"ask"` and nothing consented, the queue is empty and
  `flush_pending` performs no HTTP.
- Consent `n` leaves the queue empty and the file absent; `y` queues this
  session and leaves the policy `"ask"`, and the **next** `flush_pending`
  does send those rows (the one-time-consent path, which a
  policy-gated flush would have stranded); `a` saves the policy as
  `"enabled"`.
- **The payload has no `prompt` key** — asserted directly against a
  conversion of a full sub-project A row.
- `model_digest`/`quant_bits` are filled from the registry, and omitted
  (the key absent, not `None`) when the registry has no value.
- HTTP 500 sets the backoff and keeps the queue; the next call sends
  nothing because the backoff is active.
- A corrupt pending file does not crash.

`tests/test_cli_arena_upload.py`:

- The session-end prompt appears only when rows were recorded and the
  policy is `"ask"`.
- An `enqueue` failure (injected disk error) does not change `omm arena`'s
  exit code or lose the local vote row.

`cf-worker` vitest:

- A valid row passes.
- A row containing `prompt` is rejected.
- `winner: "tie"` is rejected.
- A row with `_a` fields but no `_b` counterpart is rejected.
- An unknown key is rejected.
- `watt_a` absent passes; present and out of range is rejected.

Firebase emulator (`scripts/test_firebase_rules.mjs`):

- A direct client write to `votes` is denied.
- A direct client read of `votes` is denied.

## Explicit non-goals

- No server-side aggregation, rating, or leaderboard (C/D).
- No duplicate-vote or ballot-stuffing detection. `client_id` is carried so
  that C *can* do it; B does not.
- No power measurement (C).
- No change to sub-project A's CLI, its local `votes.jsonl` schema, or its
  blind-phase behavior.
- No per-category or prompt-derived data of any kind (decision 1).
- No self-hosted collector variant for this channel, matching `usage`.
