# Arena vote aggregation and composite score (sub-project C) design

## Context

Sub-project C of the arena roadmap (issue #409). A (#407) shipped `omm arena`,
which writes one row per vote to `~/.omm/arena/votes.jsonl`. B (#408) shipped
the opt-in upload channel that ships those rows through the Cloudflare Worker
PoW gateway into the private Firebase RTDB `votes` node
(`docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md`,
`docs/superpowers/specs/2026-09-26-arena-vote-upload-channel-design.md`).

This document specifies **C only**: a nightly pipeline that reads the collected
votes, fits a score, and commits one Ed25519-signed artifact into the
repository. It displays nothing. Sub-project D (#410) is the only consumer of
the artifact and owns every user-visible surface.

## Goal

Turn raw votes into a leaderboard artifact in which **a merely fast or small
model cannot outrank a genuinely better one**, and in which a single machine
voting repeatedly cannot set the ranking.

Two separate facts, never blended into one number:

- **Quality** — from votes, as confidence-interval tiers.
- **Efficiency** — from measurements, as a hardware-independent rating that
  only ever breaks ties *inside* one quality tier.

This mirrors what `omm compare` already does by keeping BEST FIT / FASTEST /
MEASURED QUALITY as distinct facts
(`docs/superpowers/specs/2026-09-17-model-compare-quality-design.md`).

## Decisions fixed before implementation

Decided with the user on 2026-09-26. The implementation must not reopen them.

1. **C ships no user-visible change.** After C lands, the only difference a
   user could observe is a new file in the repository. No CLI command, no flag,
   no output. `docs/commands.json` and `README.md` are untouched because the
   command surface does not change.

2. **Power (watt) instrumentation is out of scope.** The codebase has zero
   power measurement today. The `watt_a`/`watt_b` fields are already nullable
   and already accepted by the Worker validator and the RTDB rules, so values
   can be filled in later with no rule redeploy and no schema migration. The
   efficiency axis in C uses `tokens_per_second ÷ memory_gb` only. Filling the
   watt fields is a separate issue.

3. **Deploying B's Worker route and RTDB rules is out of scope.** They are not
   deployed yet, so the live corpus is empty. C is therefore developed and
   verified entirely against synthetic row fixtures and a local-file input
   path. Running it against the real node is a separate operational step.

4. **The efficiency axis is a within-battle paired ratio, not a raw average.**
   `tokens_per_second ÷ memory_gb` on its own measures the *machine*, not the
   model: the same model at 4 GB scores 30 on an RTX 4090 (120 tok/s) and 7.5
   on an M2 (30 tok/s). Both sides of one arena row are measured on the same
   machine in the same session, so the ratio between them cancels the machine
   entirely — 30/20 on the 4090 and 7.5/5.0 on the M2 are both 1.5. C fits
   per-model efficiency ratings from those log-ratios. Accepted cost:
   comparison only propagates through pairs that actually met, so models in
   different connected components are not comparable, and are reported as
   such rather than ranked against each other. The raw
   `tokens_per_second ÷ memory_gb` median is stored alongside as a reference
   number for D to show, never as the sort key.

5. **One model means one quantization.** Identity is `model_digest` (sha256).
   `Q4_K_M` and `Q8_0` of the same repo are separate leaderboard entries,
   because that is what a user actually installs, and exposing the quality cost
   of a quantization level is part of the point. A row missing a digest on a
   side falls back to that side's normalized `model_filename`.

6. **Abuse defense is a per-pair, per-client weight cap.** One
   `(client_id, unordered model pair)` group contributes at most
   `CLIENT_PAIR_VOTE_CAP` effective votes. Rows are never discarded; their
   weight is lowered. Both the Bradley-Terry fit and the efficiency least
   squares accept fractional weights natively. The precedent is real: the
   telemetry corpus is already overwhelmingly one machine
   (`project_omm_telemetry_diversity_crisis`).

7. **The aggregation runs as a nightly GitHub Actions job that commits a
   signed artifact**, following `train.yml` exactly — quality gate, then
   `scripts/sign_catalog.py sign`, then an auto-merging PR whose head is
   SSH-signed by the retrain bot key so the "Trusted PR head" check passes.

## Inherited constraints C must not fight

From B's ledger comment on #409:

- **No prompt text, no hash, no length, no category.** Only one overall
  leaderboard is possible. Per-category ranking needs a new issue that adds a
  category question to A's CLI first.
- **`memory_gb` is GiB (1024³)** on every path, including the LM Studio
  estimate fallback. No unit mixing.
- **`tokens_per_second_a`/`_b` are the decode-speed fields.** `elapsed_*` is
  wall clock including model load, so `tokens ÷ elapsed` is *not* decode speed
  and C must not use it for the efficiency axis.
- **Slot order is redrawn every round.** `model_a` does not mean a stable side.
  `winner` always refers to that row's own a/b, so per-row reads are safe;
  any assumption of side stability is wrong.
- **Identity fields are independently optional per side.** One side may carry a
  digest while the other does not. Only the measurements (`memory_gb`,
  `tokens_per_second`, `watt`) are validated as symmetric.
- **`client_id` is present on every row.**

## Where the code lives

The scoring math lives in `scripts/`, never in `src/omm/`.

The runtime package only ever *reads* a published artifact, exactly as
`predictor.py` reads `published/localfit-recommend-model.json` without
containing the training code. This is the same boundary that keeps
`scikit-learn` out of the runtime dependency set. Tests import script modules
directly, as `tests/test_model_quality_gate.py` already does with
`from scripts import model_quality_gate`.

No new dependency. The Bradley-Terry fit and the least squares are both short
iterative loops in pure Python; `numpy` is not needed and is not added.

| File | Purpose |
|---|---|
| `scripts/arena_score.py` (new) | Pure functions: row validation, weighting, BT fit, bootstrap CI, tiering, efficiency least squares. No I/O, no network. |
| `scripts/arena_quality_gate.py` (new) | Decides whether a computed artifact may be published. Mirrors `model_quality_gate.py`. |
| `scripts/aggregate_arena_votes.py` (new) | CLI: fetch or load rows → score → gate → write artifact. Mirrors `train_model.py`'s shape. |
| `.github/workflows/arena-aggregate.yml` (new) | Nightly cron, gate, sign, auto-merging PR. |
| `published/arena-leaderboard.json` + `.manifest.json` (generated) | The artifact. Never hand-edited. |

## Data flow

```
RTDB votes node (private, .read:false)
  │  authenticated GET, ARENA_MAX_ROWS cap
  ▼
raw rows  ──► validate_rows()      drop malformed / self-battles
  │
  ├─► weight_rows()               (client_id, pair) cap → per-row weight
  │
  ├─► fit_quality()               weighted BT (MM) + phantom prior
  │     └─► bootstrap_intervals() seeded resample over battles → 95% CI
  │           └─► assign_tiers()  CI-overlap grouping against the tier leader
  │
  ├─► both_bad_rates()            per-model weighted rate → warning flag
  │
  └─► fit_efficiency()            log-ratio weighted least squares per
                                  connected component + raw median
  ▼
artifact dict ──► arena_quality_gate ──► published/arena-leaderboard.json
                                            └─► sign_catalog.py sign
```

## Scoring

### Row validation

The Worker validator and the RTDB rules already constrain what can be written,
but the artifact must not trust the corpus. `validate_rows` drops a row when:

- `winner` is not one of `a`, `b`, `both_bad`; `engine` is not `ollama` or
  `lmstudio`; `schema_version` != 1.
- either side has no usable identity (no digest and no non-empty filename).
- both sides resolve to the **same model key** (a self-battle contributes
  nothing to a pairwise fit).
- `elapsed_*` or `tokens_*` is missing or outside the rules' own range.

Dropped-row counts are reported per reason so a corpus problem is visible
rather than silently absorbed.

Rows are sorted by `(recorded_at, battle_id)` before anything else. The RTDB
export is a dict whose iteration order is not meaningful, and the bootstrap
must be reproducible: the same corpus has to produce a byte-identical
artifact, or every nightly run commits noise.

### Weighting

```
group  = (client_id, frozenset({key_a, key_b}))
weight = min(1.0, CLIENT_PAIR_VOTE_CAP / size_of_group)
```

A client that voted 3 times on a pair keeps weight 1.0 per row. One that voted
1000 times on the same pair gets 0.01 per row — still 10 effective votes total,
the same as a client that voted exactly 10 times.

### Quality: weighted Bradley-Terry

`both_bad` rows are excluded from the fit. They are counted separately (below).

Let `w_ij` be the total weight of rows where `i` beat `j`, `W_i = Σ_j w_ij`,
and `n_ij = w_ij + w_ji`. The MM (minorization-maximization) update is

```
p_i  ←  W_i  /  Σ_{j≠i} ( n_ij / (p_i + p_j) )
```

iterated to convergence, then normalized to geometric mean 1. Reported
`strength` is `log(p_i)`.

**Phantom-opponent prior.** Each model additionally gets `BT_PRIOR_STRENGTH`
virtual wins and `BT_PRIOR_STRENGTH` virtual losses against a phantom opponent
of fixed strength 1. This does three things at once:

1. Guarantees a finite unique solution. Without it, a model that never lost
   diverges to +∞ and one that never won to −∞.
2. Connects the comparison graph, so the quality axis needs no
   connected-component handling at all (unlike efficiency).
3. Shrinks low-data models toward the middle, which is the behavior we want:
   an undefeated 3-battle model should not top the board.

### Quality: confidence intervals and tiers

CI comes from a seeded bootstrap: resample battles with replacement
`BOOTSTRAP_RESAMPLES` times, refit, and take the 2.5 / 97.5 percentiles of each
model's `strength`. Seed is a fixed constant, so the artifact is deterministic.

Tiers are assigned by walking models in descending strength:

- The first model opens tier 1 and becomes its **leader**.
- A model joins the current tier if its `ci_high >= leader.ci_low` (its
  interval overlaps the leader's).
- Otherwise it opens the next tier and becomes that tier's leader.

Comparing against the *tier leader* rather than the previous model is
deliberate. Pairwise chaining is not transitive — A overlaps B and B overlaps C
while A and C are disjoint — and would let one tier drift arbitrarily wide. The
leader anchor makes a tier mean "not distinguishable from this tier's best",
which is a statement that holds for every member. The number of tiers is not
fixed; it falls out of the data.

A model with fewer than `MIN_EFFECTIVE_BATTLES` effective battles is marked
`provisional`, gets no tier, and is listed separately. Its strength and CI are
still reported so D can show it as unranked rather than missing.

### Quality floor: both_bad rate

```
both_bad_rate_i = (weight of both_bad rows involving i) / (weight of all rows involving i)
```

`quality_warning` is true when the rate is at or above
`BOTH_BAD_WARNING_RATE` and the model has at least `MIN_EFFECTIVE_BATTLES`
effective battles. The flag is independent of tier: a model can lead its tier
and still carry the warning, because "wins its matchups" and "often fails
outright" are different facts.

### Efficiency: paired log-ratio least squares

A row is efficiency-eligible only when both sides have `tokens_per_second` and
`memory_gb` present and strictly positive, with `memory_gb >= MIN_MEMORY_GB`
(below that the value is not a plausible loaded-model footprint). `both_bad`
rows **are** eligible: the measurements are valid regardless of how the user
voted.

For each eligible row:

```
e_side = tokens_per_second_side / memory_gb_side     # tok/s per GiB
r      = ln(e_a) - ln(e_b)
```

Fit per-model ratings `f_i` minimizing `Σ_rows weight · (r − (f_a − f_b))²`.
Solved by weighted iterative averaging (Gauss-Seidel):

```
f_i ← ( Σ_{rows with i} weight · (f_other ± r) ) / ( Σ_{rows with i} weight )
```

with the sign chosen by which side `i` was on. Converges on a connected graph.

Because only differences are determined, each **connected component** of the
efficiency graph is solved independently and normalized to weighted mean 0
inside that component. Ratings from different components are not comparable,
and the artifact says so: every model carries its `component` id, and
components are reported with their size. D ranks within a component and, for
two models in the same quality tier but different components, falls back to
showing the raw median instead of claiming an ordering.

`raw_median_tok_s_per_gb` is the median of that model's own `e_side` values
across eligible rows — a reference number, never the sort key, for exactly the
machine-dependence reason in decision 4.

### Constants

Named in `scripts/arena_score.py`, all overridable by CLI flag for testing:

| Constant | Value | Why |
|---|---|---|
| `CLIENT_PAIR_VOTE_CAP` | 10 | Enough for honest repeated use, small enough that one machine cannot set a ranking. |
| `MIN_EFFECTIVE_BATTLES` | 20 | Below this a BT strength is mostly prior. |
| `BOTH_BAD_WARNING_RATE` | 0.30 | Roughly "fails a third of the time". |
| `BT_PRIOR_STRENGTH` | 0.5 | Jeffreys-like; finite MLE without dominating real data. |
| `BOOTSTRAP_RESAMPLES` | 200 | Stable 95% percentiles at this corpus size. |
| `BOOTSTRAP_SEED` | 20260926 | Fixed, so the artifact is reproducible. |
| `MIN_MEMORY_GB` | 0.05 | Rejects implausible footprints. |
| `ARENA_MAX_ROWS` | 100000 | Fetch cap, as `train_model.py` has `MAX_REAL_ROWS`. |

## Fetching the private votes node

This is the one place C cannot reuse existing code, and the reason is a
deliberate security decision in the existing code.

`train_model.py:fetch_real_rows` **intentionally refuses** to send
`LOCALFIT_ADMIN_TOKEN` to a Firebase host: the `telemetry` node is
world-readable, so attaching a credential to that third-party URL would leak it
for nothing. The `votes` node is the opposite — `.read: false`, so an
authenticated read is mandatory.

C therefore gets its own fetch in `aggregate_arena_votes.py`:

- URL from `--votes-url` / `LOCALFIT_VOTES_EXPORT_URL`, validated as an
  `https://` Firebase RTDB `.json` endpoint (loopback `http://` allowed, for
  emulator testing — see `reference_firebase_rtdb_emulator_auth_testing`).
- Credential from a **new** secret `LOCALFIT_VOTES_ADMIN_TOKEN`, sent as an
  `Authorization: Bearer` header — never as a query parameter, which would put
  the credential into request logs and CI output. A legacy Firebase database
  secret (`?auth=`) is deliberately not supported; the secret is expected to be
  a service-account OAuth2 access token. Absent credential is a hard error, not
  a silent empty corpus: an unauthenticated read of a private node returns 401,
  and treating that as "no votes yet" would publish an empty leaderboard.
- `orderBy="$key"` + `limitToLast=ARENA_MAX_ROWS`, as the telemetry fetch does.
  RTDB keys here are PoW digests, not timestamps, so this is a uniform sample
  and must not be described as "the newest rows".
- `--votes-file <path>` reads JSONL or Firebase-shaped JSON instead, which is
  how every test and every pre-deployment run works.

The RTDB rules need no change. The workflow reads with an admin credential,
which bypasses rules; `.read: false` stays as the documented source of truth.

## Artifact

`published/arena-leaderboard.json`, signed into
`published/arena-leaderboard.manifest.json` by the existing
`scripts/sign_catalog.py` with the existing `LOCALFIT_CATALOG_SIGNING_KEY`. No
new key: `config.py`'s `catalog_public_key` already verifies anything that key
signs, via `catalog.py:verify_signed_artifact`.

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-27T04:00:00+00:00",
  "corpus": {
    "rows_fetched": 1420,
    "rows_used": 1388,
    "rows_dropped": {"malformed": 12, "self_battle": 20},
    "effective_votes": 903.5,
    "client_count": 47,
    "largest_client_share": 0.18
  },
  "models": [
    {
      "key": "sha256:7f3c…",
      "display_filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
      "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
      "provider": "huggingface",
      "quant_bits": 4.0,
      "battles": 57,
      "effective_battles": 41.0,
      "provisional": false,
      "quality": {"strength": 0.42, "ci_low": 0.11, "ci_high": 0.73, "tier": 1},
      "both_bad_rate": 0.08,
      "quality_warning": false,
      "efficiency": {
        "rating": 0.31,
        "component": 0,
        "raw_median_tok_s_per_gb": 22.4,
        "sample": 31
      }
    }
  ],
  "tiers": [{"tier": 1, "model_keys": ["sha256:7f3c…"]}],
  "efficiency_components": [{"component": 0, "model_count": 18}],
  "provisional_models": ["sha256:0ab1…"]
}
```

Notes on the shape:

- `key` is `sha256:<digest>` or `filename:<normalized name>`, so D never has to
  guess which fallback produced an entry.
- `efficiency` is `null` when a model has no eligible rows.
- `quality.tier` is `null` exactly when `provisional` is true.
- `models` is sorted by tier, then efficiency rating within the tier, then key
  — a stable order, so a rerun on an unchanged corpus produces an identical
  file and no commit.

## Quality gate

`scripts/arena_quality_gate.py` refuses publication when:

| Condition | Threshold | Why |
|---|---|---|
| Too little data overall | effective votes < 200 | A leaderboard from a handful of battles is noise presented as fact. |
| One machine dominates | `largest_client_share` > 0.5 | The telemetry diversity failure, caught before publication. |
| Too few ranked models | < 5 non-provisional | Not a board. |
| Ranked models collapsed | > 30% fewer than the incumbent artifact | Catches a corpus or parsing regression. |

Gate status and reasons go to stdout and to `$GITHUB_OUTPUT`, as
`retrain_decision.py` does. A blocked run is a **success** that publishes
nothing, not a failed workflow — the same contract `train.yml` has with its own
gate. On the very first run there is no incumbent, so the collapse check is
skipped rather than treated as a 100% drop.

## Workflow

`.github/workflows/arena-aggregate.yml`:

- `schedule: "0 4 * * *"` — an hour after `train.yml`'s 03:00, so the two never
  contend for the same auto-merging PR path, plus `workflow_dispatch`.
- `concurrency: group: arena-aggregate, cancel-in-progress: false`, matching
  `train.yml`'s reason: a dispatched run must queue behind the cron run instead
  of racing it into a conflicted PR.
- Preflight that `LOCALFIT_VOTES_EXPORT_URL` and `LOCALFIT_VOTES_ADMIN_TOKEN`
  are both set, and copy the incumbent artifact aside for the gate.
- Run the aggregator; sign only when the gate passed and the artifact changed.
- Open an auto-merging PR whose head commit is SSH-signed with
  `LOCALFIT_RETRAIN_SSH_KEY` (a key in `src/omm/trust/allowed_signers`), so the
  "Trusted PR head" check passes. `git add` names only the two artifact paths —
  never `-A`.

This workflow only starts firing once the branch reaches `main`, which is the
end of the arena roadmap. That is expected, not a gap.

## Testing

Every test runs against synthetic rows. No network, no real corpus.

**`tests/test_arena_score.py`** — the math, in the properties that matter:

- **Machine-effect cancellation.** Two synthetic machines, one uniformly 4×
  faster, battling the same model pairs, produce *identical* efficiency
  ratings. This is the whole justification for decision 4; if this test does
  not hold, the design is wrong.
- **Raw median does not cancel it.** The same fixture yields visibly different
  `raw_median_tok_s_per_gb` values — proving the reference number is a
  reference number and the rating is what may be sorted on.
- BT: fully symmetric results give equal strengths; a dominance chain A>B>C
  yields that order; an undefeated model with 3 battles stays below a
  well-supported model with 200 (prior shrinkage).
- Weight cap: one client voting 1000× on one pair does not outrank the
  aggregate of many clients; its group's total effective weight is exactly
  `CLIENT_PAIR_VOTE_CAP`.
- Tiers: overlapping CIs group; a clearly separated model opens a new tier;
  the non-transitive A~B~C-but-A≁C fixture puts C outside A's tier (leader
  anchoring, not chaining).
- Efficiency components: two disjoint groups of models get different
  `component` ids, and neither's ratings are presented as comparable.
- `both_bad`: excluded from the BT fit, counted in the rate, and the warning
  fires only past both thresholds.
- Determinism: scoring the same rows twice, and scoring a shuffled copy,
  produce byte-identical artifacts.
- Row validation: one test per drop reason, each asserting the reported count.

**`tests/test_arena_quality_gate.py`** — one test per refusal condition, plus
the first-run no-incumbent path, plus a passing case.

**`tests/test_aggregate_arena_votes.py`** — the CLI: `--votes-file` JSONL and
Firebase-shaped input; a missing credential with a private URL is a hard error;
a non-Firebase or `http://` non-loopback URL is rejected; the artifact is
written atomically; a signature round-trip verifies through
`catalog.verify_signed_artifact`; a blocked gate exits 0 and writes nothing.

**`tests/test_arena_aggregate_workflow.py`** — workflow text, modeled on the
existing `tests/test_train_workflow.py`: actions pinned by commit SHA, the
concurrency group set with `cancel-in-progress: false`, `git add` naming only
the two artifact paths, signing gated on the quality gate having passed, and the
credential passed through the environment rather than interpolated into a URL.

## Explicit non-goals

- Any user-visible output. D (#410) owns all of it.
- Power measurement (separate issue).
- Deploying B's Worker route or RTDB rules (separate operational step).
- Per-category leaderboards — structurally impossible without a prompt category
  field, which would need a new issue against A's CLI.
- Feeding arena data into recommend-model training. That is E (#411).
- Any change to `src/omm/`, the CLI surface, `docs/commands.json`, `README.md`,
  `PRIVACY.md` (no new data is collected), or `database.rules.json`.
