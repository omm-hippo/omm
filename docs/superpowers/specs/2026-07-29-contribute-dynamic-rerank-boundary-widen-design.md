# `omm contribute` dynamic re-ranking + boundary widening design

## Problem

`omm contribute` already runs per-candidate auto-calibration
(`_maybe_auto_calibrate`, `cli.py:1218`, called from `_install_impl` at
`cli.py:1412`) after every successful benchmark — this was previously
misreported as install-only; it fires on both `install` and `contribute`
since they share `_install_impl`. But the updated calibration factor is not
put to use during the same run: `ContributionQueue._rebuild()`
(`contribute.py:83`) only runs at queue construction and after a successful
`refetch()`, so the phase A/B ordering computed at session start stays
fixed even as calibration accuracy improves benchmark-by-benchmark.

Separately, `ContributionQueue`'s Phase B is meant to alternate outward from
the hardware's fit/unfit boundary (`_below_pool` = weakest-still-viable,
`_above_pool` = least-bad-unviable, `contribute.py:9-14`). In practice
`_below_pool` is almost always already exhausted by the time Phase B
starts, because Phase A (`contribute.py:87-89`) already benchmarks *every*
viable candidate from the full ranked pool, not just a top-N slice. Since
the candidate pool itself is a fixed ~30-model HuggingFace/ModelScope
trending list (`scripts/fetch_candidates.py`), boundary exploration in
practice only ever happens on the unviable side, and only within whatever
model sizes happen to already be in that list — it doesn't systematically
narrow in on where a given machine's ceiling actually sits.

## Goals

1. Reflect newly-learned local calibration immediately in the remaining
   queue order during the same `contribute` run, not just on the next run
   or after a full-queue refetch.
2. Once the fixed candidate pool is exhausted, actively probe *closer* to
   this machine's actual fit/unfit boundary by trying sibling quantization
   files of the two boundary-adjacent repos already benchmarked — without
   introducing any new repos beyond the existing ~30-candidate pool.

## Solution

### 1. Re-rank after every benchmark

Call `self._rebuild()` from inside `ContributionQueue.mark_seen()`
(`contribute.py:96-97`), instead of only from `next_candidate()`'s refetch
path. `_rebuild()` already re-runs `predictor.rank_candidates()` (which
applies the current on-disk calibration factor via
`predict_speed_interval(..., apply_calibration=True)`) and already filters
out anything in `history_refs`, so no new ranking logic is needed — only
the call site changes.

Consequence (intended): a candidate that was viable under the old
calibration factor can flip to unviable after recalibration, or vice
versa, and will move between Phase A/B pools accordingly on the next
`_rebuild()`. This is correct — the point of recalibrating mid-session is
for judgments to get more accurate as more of this specific machine's
behavior is observed.

`mark_seen` is called from `cli.py:3132/3148/3167` after a benchmark
completes (success, skip-unfit, or give-up-after-failures) — all three
call sites get the rebuild for free.

### 2. Boundary widening via same-repo quant siblings ("Phase C")

Added to `ContributionQueue`, tried only after Phase A and Phase B
(`_below_pool`/`_above_pool`) are both exhausted and `refetch()` returns no
new data (i.e. as a new fallback inside `next_candidate()`, after the
existing `refetch` block, `contribute.py:120-127`).

**Scope**: exactly two repos — the last candidate that was benchmarked
viable (weakest-still-fits) and the first candidate benchmarked unviable
(least-bad-unfit). These are recorded as `self._boundary_below` /
`self._boundary_above` the moment each is drawn from `_below_pool` /
`_above_pool` respectively. No other repos are widened, and no repos
outside the original ~30-candidate pool are introduced.

**Mechanism**, for each of the two boundary repos:

1. `providers.<provider>.fetch_repo_files(repo_id)` → list of GGUF
   filenames in that repo (existing function, HF: `huggingface.py:16`, MS:
   `modelscope.py:55`).
2. For each filename not already in `history_refs`, parse its quant size
   with `featurize.parse_quant_bits(filename)` (existing function,
   `featurize.py:183`).
3. Sort unseen siblings by quant-bit distance from the file that was
   already benchmarked for that repo, closest first — so the search steps
   outward one quant level at a time rather than jumping straight to the
   most extreme file.
4. Resolve size via `hub.remote_file_size(provider, repo_id, filename)`
   (existing function, `hub.py:110`) and build a candidate dict
   (`repo_id`, `filename`, `provider`, `size_bytes`) compatible with what
   `_install_impl` already expects.
5. Feed these as a small queue, same as Phase A/B — the existing
   `predict_speed_interval` fit/unfit check inside `_install_impl`
   (`cli.py:1274-1297`) still applies per candidate, and
   benchmark/telemetry-upload/calibration all reuse the existing pipeline
   unchanged.

**Stopping condition**: a repo's Phase C queue ends when its sibling GGUF
list is exhausted (typically single digits per repo, so this is bounded
without a separate cap). When both boundary repos' Phase C queues are
exhausted, `next_candidate()` returns `None` and the run ends normally,
same as today.

**Error handling**: `fetch_repo_files`/`remote_file_size` have no
caching or retry today (confirmed — none exists anywhere in this path).
Phase C calls are best-effort: on `RequestException`/`ModelResolutionError`
for a boundary repo, skip Phase C for that repo only (log at debug level)
rather than aborting the run — matches the existing "never block the
loop" posture of `_maybe_auto_calibrate`.

## Out of scope

- No live HF/ModelScope search for entirely new repos beyond the existing
  ~30-candidate pool (considered, explicitly deferred by user).
- No change to `omm setting calibrate` (the separate manual single-model
  command, `cli.py:1919`) — untouched.
- No change to how `_maybe_auto_calibrate` itself computes the calibration
  factor (`calibration.py`) — only when the queue picks up the result.
- No change to `scripts/fetch_candidates.py` or the central decision-tree
  training pipeline — this is entirely client-side queue behavior.
- No new CLI flags — both changes are always-on for `omm contribute`.

## Testing

`contribute.py` is already pure/unit-testable (no Typer/console/network
deps by design, per its module docstring). Extend
`tests/test_contribute.py` (or equivalent) with:

- Re-rank: a candidate ranked unviable at construction becomes viable
  after `mark_seen` + a stubbed calibration factor change, and is served
  by a later `next_candidate()` call within the same session (no refetch
  involved).
- Phase C: given a stubbed `fetch_repo_files`/`remote_file_size` for the
  two boundary repos, `next_candidate()` yields their unseen sibling
  quants in closest-first order after Phase A/B exhaust, and stops
  (`None`) once both are drained.
- Phase C is skipped (not retried, not fatal) when
  `fetch_repo_files`/`remote_file_size` raises.
- No new repos outside the original candidate pool ever appear in Phase C
  output — guards against scope creep back toward live search.
