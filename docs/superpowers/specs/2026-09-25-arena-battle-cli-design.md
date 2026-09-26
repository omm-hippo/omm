# Local model arena / battle CLI (sub-project A) design

## Context and roadmap

This is the first of five sub-projects toward an LM Arena-style feature for
`omm`: blind, side-by-side model comparison with human votes, eventually
aggregated server-side into a composite leaderboard that weighs both vote
outcomes and hardware-normalized performance (speed/power/memory), and later
fed back into the recommendation model's training data.

The five sub-projects, in dependency order:

- **A (this doc)** — local battle CLI: pairing, blind sequential generation,
  vote capture, local-only vote log. No network calls beyond what the chosen
  engine already needs to run a local model.
- **B** — vote upload channel: a fourth opt-in outbound channel (alongside
  `telemetry`/`usage`/`error_reports`) that ships `votes.jsonl` rows to the
  Cloudflare Worker gateway into a new Firebase RTDB node, reusing the
  existing PoW-gateway pattern (`cf-worker/src/validate.ts`,
  `database.rules.json`).
- **C** — server-side aggregation: a composite score combining Bradley-Terry
  vote outcomes with hardware-normalized speed/power/memory measurements.
  Full implementation spec still lands when C's turn comes, but the shape
  is fixed now because it drives what A/B must not lose:

  - **Quality axis** (votes only): standard Bradley-Terry MLE fit over
    `winner in {a, b}` outcomes; `both_bad` excluded from the pairwise fit
    but tracked per-model as a separate rate, surfaced as a quality-floor
    warning badge regardless of tier. Models are grouped into tiers by
    overlapping confidence intervals (low vote counts stay wide and
    provisional) rather than shown as one falsely-precise scalar.
  - **Efficiency axis** (telemetry, hardware-normalized as *ratios*, not
    bucket percentiles): tokens/sec ÷ measured memory (GB), and
    tokens/sec ÷ watts when available. Dividing by the resource actually
    used, rather than binning by hardware class, is what makes "faster
    because of a bigger GPU" distinct from "genuinely more efficient per
    resource." Memory: Ollama's existing `/api/ps` response already
    reports `size`/`size_vram` for a loaded model, so a measured (not
    file-size-predicted) figure is close to free to add for Ollama;
    LM Studio's API doesn't expose this today, so its rows fall back to
    `memory_required_gb`-based estimate with a lower-confidence flag until
    that gap is closed. Power (watts): genuinely new instrumentation,
    **best-effort only** — `nvidia-smi --query-gpu=power.draw` for
    NVIDIA, `rocm-smi` for AMD when present, null on Apple Silicon
    (`powermetrics` needs sudo, unusable from unattended telemetry) and on
    Windows CPU-only inference (no standard reader). A row with null watts
    still contributes its tokens/sec÷GB term; the efficiency axis never
    requires watts to exist.
  - **Leaderboard display**: never collapses to one blended number. Primary
    sort is quality tier; efficiency score only breaks ties *within* a
    tier. This mirrors `compare`'s existing principle of keeping BEST
    FIT/FASTEST/MEASURED QUALITY as separate facts rather than one
    opaque score, and is what stops a merely-fast model from outranking a
    clearly-better one.
- **D** — public leaderboard: `omm arena --leaderboard` fetching a signed
  artifact (same Ed25519 trust pattern as the recommendation catalog) plus a
  public web page.
- **E** — feeding vote/bench data into `mltree.py` / `train.yml` as a new
  training feature or label.

Each of B–E gets its own brainstorm/spec cycle once the one before it has
landed. This document specifies **A only**.

## Goal

Let a user compare two installed local models on the same prompt, blind,
without deciding in advance which one is "supposed" to be better, and record
that judgment locally in a schema that sub-project B can upload as-is.

## Product contract

- Command name is `omm arena` (confirmed 2026-09-26). `compare` stays
  reserved for the existing catalog-comparison command at `cli.py:4333`
  and must not be reused.
- Works only on models already installed and linked into a runnable engine
  (Ollama or LM Studio) via the existing hub+link registry
  (`~/.omm/models.json`). Never installs, downloads, or ranks catalog
  entries — that is what `compare`/`recommend` already do.
- Requires at least 2 distinct eligible installed models; otherwise exits
  with a clear error before doing anything else.
- A session runs one or more **rounds**. Each round is exactly one prompt,
  one blind pair, one vote. After a vote (or a failed round), the user is
  asked whether to continue; declining ends the session.
- Positional model arguments (`omm arena MODEL1 MODEL2`) seed only the
  first round's pair. A separate flag, `--keep` (confirmed 2026-09-26;
  `pin` stays reserved for model-version pinning, issue #295), is what
  makes a pair (whether positional or randomly drawn) persist for every
  round of the session. Without that flag, every round — including the
  first if no models were given — draws a fresh random pair from eligible
  installed models. This rule is the same whether or not positional models
  were given; the flag is the only thing that changes persistence.
- A session uses one engine for every round (confirmed 2026-09-26).
  `engine_a`/`engine_b` stay separate fields in the vote schema so
  cross-engine pairing needs no schema migration later.
- `watt_a`/`watt_b` are written as `null` in sub-project A (confirmed
  2026-09-26); the `nvidia-smi`/`rocm-smi` reader lands with sub-project C,
  which is the first consumer. `memory_gb_*` is measured for Ollama via
  `/api/ps` (`quality.loaded_model_memory_gb`, preferring `size_vram` and
  falling back to `size` for CPU-only inference).
- Blind by default, no opt-out in this sub-project: responses are shown as
  "Response 1" / "Response 2" only. No model name, engine name, elapsed
  time, or token count is shown before the vote — timing alone can leak
  identity for models a user already knows well.
- Vote options: **A wins**, **B wins**, **both bad**. No tie option.
- Immediately after the vote, the round reveals: real model name, engine
  used, elapsed generation time, and measured tokens/sec for each side.
- Generation uses each model's own default sampling (no forced
  `temperature=0`/`seed=0`) — the point is comparing what a user would
  actually get in normal use, not reproducibility. If the model supports
  extended thinking, only the final answer is shown; the thinking trace is
  discarded before display, never persisted.

## Architecture

No new runtime dependency. Reuses:

- `quality.py`'s existing free-text generation path (`_generate` for
  Ollama, `_generate_lmstudio` for LM Studio) — already used by `omm
  evaluate` for real (non-probe) generation, unbounded by
  `engines/base.py`'s `ProbeRequest` 64-token/deterministic-sampling
  constraints.
- The same sequential load → generate → unload lifecycle `benchmark.py` /
  `quality.py` already use per engine, including their existing
  OOM/timeout/crash failure-reason classification
  (`engines/base.py:FailureReason`).
- `questionary` for prompt input and the continue/exit confirm, matching
  the existing `omm setting`-style interactive loop feel. No split-pane or
  streaming UI is needed: generation is sequential (one model fully
  finishes and unloads before the next loads), so both full responses are
  simply printed together once both are ready.

New pieces, all local:

- The `arena` CLI command itself (pairing, round loop, vote capture,
  reveal, local persistence).
- `~/.omm/arena/votes.jsonl` — one JSON object per line, local-only
  (respects `OMM_HOME` like every other on-disk state), never uploaded by
  this sub-project. Fields, chosen to be exactly what sub-project B would
  ship as-is:

  ```
  battle_id     string, uuid4
  timestamp     ISO 8601 UTC
  prompt        string, the user's raw prompt text
  model_a       string, model registry ref
  model_b       string, model registry ref
  engine_a      "ollama" | "lmstudio"
  engine_b      "ollama" | "lmstudio"
  elapsed_a     float, seconds
  elapsed_b     float, seconds
  tokens_a      int
  tokens_b      int
  memory_gb_a   float | null, measured (Ollama /api/ps size_vram) when
                available, null for LM Studio or on read failure
  memory_gb_b   float | null, same as memory_gb_a
  watt_a        float | null, best-effort (nvidia-smi/rocm-smi), null on
                Apple Silicon / Windows CPU-only / no supported reader
  watt_b        float | null, same as watt_a
  winner        "a" | "b" | "both_bad"
  pinned        bool, whether --pin was active this round
  ```

  `memory_gb_*`/`watt_*` exist purely so sub-project C's efficiency axis
  (tokens/sec ÷ measured memory, tokens/sec ÷ watts — see roadmap above)
  has the raw numbers it needs without a schema migration later. A itself
  makes no use of these fields beyond collecting and storing them; the
  post-vote reveal (see Round flow) may optionally surface them but that
  is a display detail, not a scoring one.

## Round flow

1. Resolve eligible models (installed + linked to a runnable engine). Fewer
   than 2 → error, exit.
2. Pick the round's pair: positional args (round 1 only, unless pinned) or
   a fresh random draw without replacement bias toward either slot.
3. Prompt for input text (single-line `questionary.text`).
4. Load model A → generate → unload. Load model B → generate → unload.
   Each side's elapsed time and token count are measured but not shown
   yet.
5. Print both responses blind, labeled "Response 1" / "Response 2" only.
6. Ask for a vote: A wins / B wins / both bad.
7. Reveal: real names, engines, elapsed time, tokens/sec for both sides.
8. Append a row to `votes.jsonl` (skipped if the round errored before a
   vote was cast).
9. Ask "continue?" — yes loops to step 2, no ends the session.

## Error handling

If either model fails to load or generate (OOM, timeout, engine
unreachable, crash) partway through a round, that round aborts without a
vote and without a `votes.jsonl` row. The failure is shown using the
engine's existing `FailureReason` message, then the session falls through
to the same "continue?" prompt as a completed round — one bad round does
not kill the whole session.

## Testing

- Pairing logic: random-without-replacement draw, `--pin` persistence
  across rounds, positional-args-seed-round-1-only behavior.
- Blind-phase output never contains a model name, engine name, or timing
  number before the vote is cast (this is a real regression risk worth a
  dedicated assertion, not just eyeballing).
- `votes.jsonl` schema: one well-formed JSON object per line, correct
  field set, `winner` constrained to the three allowed values,
  `memory_gb_*`/`watt_*` degrade to `null` (not a crash) when the engine
  or platform can't supply them.
- CLI plumbing with mocked engines, following the existing
  `test_cli_evaluate.py` / benchmark test pattern (mock
  `linker.resolve_lmstudio_model` etc. rather than relying on no real `lms`
  being on `PATH`).
- Failed-round path: one engine call raises `RuntimeAdapterError`, session
  continues, no vote row written for that round.

## Explicit non-goals for this sub-project

- No network upload of votes (sub-project B).
- No cross-user or cross-session aggregation, rating, or leaderboard
  (sub-projects C/D).
- No training-data integration (sub-project E).
- No streaming token-by-token display, no concurrent dual generation, no
  multi-turn conversation within a round (confirmed: one prompt per
  round).
- No tie vote option (confirmed).
