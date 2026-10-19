# Arena vote aggregation and composite score Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A nightly pipeline that reads collected arena votes, fits a Bradley-Terry quality tiering and a machine-independent efficiency rating, and commits one Ed25519-signed leaderboard artifact into `published/`.

**Architecture:** Three new scripts under `scripts/` — pure math (`arena_score.py`), a publication gate (`arena_quality_gate.py`), and a CLI that fetches, scores, gates and writes (`aggregate_arena_votes.py`) — plus a GitHub Actions workflow modeled exactly on `train.yml`. Nothing is added to `src/omm/`: the runtime only ever reads a published artifact, the same boundary that keeps `scikit-learn` out of the runtime dependency set.

**Tech Stack:** Python 3.10+ standard library only for the math (no `numpy`, no `scikit-learn`), `requests` for the authenticated RTDB read, the existing `scripts/sign_catalog.py` for Ed25519 signing, `omm.atomic.atomic_write_text` for writes, pytest.

**Spec:** `docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md`

## Global Constraints

- **No new runtime dependency, and no new dependency at all.** Pure standard library for the math. `numpy` and `scikit-learn` are forbidden in all three new scripts.
- **Nothing under `src/omm/` changes.** No CLI command, no flag, no output. `docs/commands.json`, `README.md`, `PRIVACY.md` and `database.rules.json` are untouched.
- **`memory_gb` is GiB (1024³) everywhere.** Never convert, never mix units.
- **Use `tokens_per_second_a`/`_b` for speed, never `tokens ÷ elapsed`.** `elapsed_*` is wall clock and includes model load.
- **Never assume `model_a` is a stable side.** Slot order is redrawn every round; read `winner` per row only.
- **Identity fields are independently optional per side**; only `memory_gb`, `tokens_per_second` and `watt` are validated symmetric.
- **The artifact must be deterministic.** The same corpus, in any input order, produces a byte-identical file. Fixed `BOOTSTRAP_SEED = 20260926`; rows sorted before use.
- **Commit style:** English Conventional Commits titles. Every commit ends with the two attribution lines shown in Task 1 Step 5.
- **`git add` names only the files that task touched.** Never `-A`, never `.` — other Claude sessions share this checkout.
- **Expect the patch version in `pyproject.toml` and `packaging/npm/launcher/package.json` to change on every commit.** `scripts/pre-commit` does that; it is not noise.
- **Run tests from the repo root** (`pythonpath="."`). Local runs have `FORCE_COLOR=3` set, which makes ~18-22 rich-related tests in the full suite fail for reasons unrelated to this work; the per-file runs in this plan are unaffected.

## Review Focus

Five conditions the spec implies but does not spell out, each of which would produce a wrong published leaderboard rather than a crash. The test for each is placed in the task that owns the code.

1. **The RTDB export arrives in one of three shapes** — `{"<pow-digest>": {row}, ...}`, `{"votes": {...}}`, or a bare list. Parsing only one of them yields an empty corpus that the gate reads as "no data yet" instead of a parsing bug. → Task 7.
2. **A corpus with no efficiency-eligible row at all** (every row from LM Studio, which has no memory API). Every model's `efficiency` must be `null` and the artifact must still publish, with no division by zero and no empty-median crash. → Task 4.
3. **A model whose every battle was `both_bad`** has zero BT wins *and* zero losses, so its fit is prior-only. It must get a finite strength, a `both_bad_rate` of 1.0, and the warning flag — not `NaN` and not a crash. → Task 2.
4. **A duplicate `battle_id`.** B's upload queue can re-send a row after a partial flush, and the RTDB key is a PoW digest, not the battle id, so duplicates are storable. Counting one vote twice is exactly the abuse the weight cap exists to stop. Rows must be deduplicated by `battle_id`, keeping the first occurrence in sorted order. → Task 1.
5. **Numbers that satisfy the RTDB rules but break the math** — `tokens_per_second: 0`, `memory_gb: 0`, or a non-finite float from a hand-crafted row. `ln(0)` is `-inf` and poisons every rating in the connected component. These rows must be dropped from the efficiency axis (not from the quality axis, where they are still valid votes). → Task 1.

---

### Task 1: Row validation, model identity, and weighting

**Files:**
- Create: `scripts/arena_score.py`
- Test: `tests/test_arena_score.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CLIENT_PAIR_VOTE_CAP = 10`, `MIN_EFFECTIVE_BATTLES = 20`, `BOTH_BAD_WARNING_RATE = 0.30`, `BT_PRIOR_STRENGTH = 0.5`, `BOOTSTRAP_RESAMPLES = 200`, `BOOTSTRAP_SEED = 20260926`, `MIN_MEMORY_GB = 0.05`, `MAX_ITERATIONS = 500`, `TOLERANCE = 1e-10`
  - `@dataclass(frozen=True) class Battle` with fields `battle_id: str`, `recorded_at: str`, `client_id: str`, `key_a: str`, `key_b: str`, `winner: str`, `efficiency_a: float | None`, `efficiency_b: float | None`
  - `model_key(row: dict, side: str) -> str | None`
  - `validate_rows(rows: list[dict]) -> tuple[list[Battle], dict[str, int]]`
  - `battle_weights(battles: list[Battle]) -> list[float]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arena_score.py`:

```python
"""Sub-project C scoring math (scripts/arena_score.py)."""

from __future__ import annotations

from scripts import arena_score


def _row(**overrides) -> dict:
    """A minimal valid vote row, shaped exactly like arena_upload's wire row."""
    row = {
        "schema_version": 1,
        "battle_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "abcdef12",
        "recorded_at": "2026-09-26T10:00:00+00:00",
        "engine": "ollama",
        "winner": "a",
        "model_filename_a": "alpha-q4_k_m.gguf",
        "model_filename_b": "beta-q4_k_m.gguf",
        "elapsed_a": 4.0,
        "elapsed_b": 5.0,
        "tokens_a": 100,
        "tokens_b": 120,
        "tokens_per_second_a": 40.0,
        "tokens_per_second_b": 20.0,
        "memory_gb_a": 4.0,
        "memory_gb_b": 4.0,
    }
    row.update(overrides)
    return row


def test_digest_wins_over_filename_for_identity():
    digest = "a" * 64
    assert arena_score.model_key(_row(model_digest_a=digest), "a") == f"sha256:{digest}"


def test_filename_is_the_fallback_and_is_normalized():
    key = arena_score.model_key(_row(model_filename_a="Alpha-Q4_K_M.GGUF"), "a")
    assert key == "filename:alpha-q4_k_m.gguf"


def test_a_side_with_no_usable_identity_drops_the_row():
    battles, dropped = arena_score.validate_rows([_row(model_filename_a="  ")])
    assert battles == []
    assert dropped["identity"] == 1


def test_a_self_battle_is_dropped():
    digest = "b" * 64
    battles, dropped = arena_score.validate_rows(
        [_row(model_digest_a=digest, model_digest_b=digest)]
    )
    assert battles == []
    assert dropped["self_battle"] == 1


def test_an_unknown_winner_is_dropped():
    battles, dropped = arena_score.validate_rows([_row(winner="tie")])
    assert battles == []
    assert dropped["winner"] == 1


def test_a_duplicate_battle_id_is_counted_once():
    # B's upload queue can re-send a row after a partial flush, and the RTDB
    # key is a PoW digest rather than the battle id, so duplicates are storable.
    battles, dropped = arena_score.validate_rows([_row(), _row()])
    assert len(battles) == 1
    assert dropped["duplicate_battle_id"] == 1


def test_rows_are_sorted_so_input_order_cannot_change_the_result():
    early = _row(battle_id="1" * 8 + "-1111-1111-1111-111111111111",
                 recorded_at="2026-09-26T09:00:00+00:00")
    late = _row(battle_id="2" * 8 + "-1111-1111-1111-111111111111",
                recorded_at="2026-09-26T11:00:00+00:00")
    forward, _ = arena_score.validate_rows([early, late])
    backward, _ = arena_score.validate_rows([late, early])
    assert [b.battle_id for b in forward] == [b.battle_id for b in backward]


def test_a_zero_speed_row_stays_a_vote_but_leaves_the_efficiency_axis():
    # ln(0) is -inf and would poison every rating in the component. The vote
    # itself is still valid.
    battles, dropped = arena_score.validate_rows([_row(tokens_per_second_a=0.0)])
    assert len(battles) == 1
    assert battles[0].efficiency_a is None
    assert battles[0].efficiency_b is None
    assert dropped == {}


def test_a_memory_reading_below_the_floor_leaves_the_efficiency_axis():
    battles, _ = arena_score.validate_rows([_row(memory_gb_a=0.0)])
    assert battles[0].efficiency_a is None


def test_efficiency_is_tokens_per_second_over_gibibytes():
    battles, _ = arena_score.validate_rows([_row()])
    assert battles[0].efficiency_a == 10.0  # 40 tok/s over 4 GiB
    assert battles[0].efficiency_b == 5.0


def test_one_client_repeating_one_pair_is_capped_at_ten_effective_votes():
    rows = [_row(battle_id=f"{index:08d}-1111-1111-1111-111111111111")
            for index in range(1000)]
    battles, _ = arena_score.validate_rows(rows)
    weights = arena_score.battle_weights(battles)
    assert len(weights) == 1000
    assert sum(weights) == 10.0


def test_a_client_under_the_cap_keeps_full_weight():
    rows = [_row(battle_id=f"{index:08d}-1111-1111-1111-111111111111")
            for index in range(3)]
    battles, _ = arena_score.validate_rows(rows)
    assert arena_score.battle_weights(battles) == [1.0, 1.0, 1.0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.arena_score'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/arena_score.py`:

```python
#!/usr/bin/env python3
"""Arena leaderboard scoring math (sub-project C, issue #409).

Pure functions over vote rows: validation, abuse weighting, a weighted
Bradley-Terry quality fit with confidence-interval tiers, and a
machine-independent efficiency rating fitted from within-battle log-ratios.

No I/O, no network, no dependency outside the standard library. The runtime
package never imports this module - it only reads the artifact this produces,
the same boundary that keeps scikit-learn out of omm's runtime dependencies.

Spec: docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

#: One (client_id, unordered model pair) group contributes at most this many
#: effective votes. Rows are never discarded, only down-weighted.
CLIENT_PAIR_VOTE_CAP = 10

#: Below this much effective evidence a Bradley-Terry strength is mostly the
#: prior, so the model is reported as provisional and gets no tier.
MIN_EFFECTIVE_BATTLES = 20

#: "Fails outright about a third of the time."
BOTH_BAD_WARNING_RATE = 0.30

#: Virtual wins and losses against a phantom opponent of fixed strength 1.
#: Keeps the MLE finite for an undefeated or never-winning model, connects the
#: comparison graph, and shrinks thin records toward the middle.
BT_PRIOR_STRENGTH = 0.5

BOOTSTRAP_RESAMPLES = 200

#: Fixed so an unchanged corpus produces a byte-identical artifact. A nightly
#: job that commits bootstrap noise is worse than no job.
BOOTSTRAP_SEED = 20260926

#: Below this a memory reading is not a plausible loaded-model footprint.
MIN_MEMORY_GB = 0.05

MAX_ITERATIONS = 500
TOLERANCE = 1e-10

WINNERS = frozenset({"a", "b", "both_bad"})
ENGINES = frozenset({"ollama", "lmstudio"})
SIDES = ("a", "b")

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# Mirrors the ranges database.rules.json already enforces. The artifact must
# not trust the corpus: the rules constrain what can be written today, not what
# is already stored or what a future rule change might let through.
_MAX_ELAPSED = 3600
_MAX_TOKENS = 1_000_000


@dataclass(frozen=True)
class Battle:
    """One validated vote. `efficiency_*` is None when the row cannot be used
    for the efficiency axis; the vote itself still counts for quality."""

    battle_id: str
    recorded_at: str
    client_id: str
    key_a: str
    key_b: str
    winner: str
    efficiency_a: float | None
    efficiency_b: float | None

    @property
    def pair(self) -> frozenset[str]:
        return frozenset({self.key_a, self.key_b})


def model_key(row: dict, side: str) -> str | None:
    """Stable identity for one side of a battle.

    sha256 when the row carries a digest, otherwise the normalized filename.
    The prefix is part of the key so a consumer never has to guess which
    fallback produced an entry.
    """
    digest = row.get(f"model_digest_{side}")
    if isinstance(digest, str) and _DIGEST_RE.match(digest):
        return f"sha256:{digest}"
    filename = row.get(f"model_filename_{side}")
    if isinstance(filename, str) and filename.strip():
        return f"filename:{filename.strip().lower()}"
    return None


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _bounded_number(value: object, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    return number


def _efficiency(row: dict, side: str) -> float | None:
    """tokens/sec per GiB of measured footprint, or None when unusable.

    Uses tokens_per_second, never tokens/elapsed: elapsed is wall clock and
    includes the model load, so it is not decode speed.
    """
    speed = _positive_number(row.get(f"tokens_per_second_{side}"))
    memory = _positive_number(row.get(f"memory_gb_{side}"))
    if speed is None or memory is None or memory < MIN_MEMORY_GB:
        return None
    return speed / memory


def validate_rows(rows: list[dict]) -> tuple[list[Battle], dict[str, int]]:
    """Turn raw vote rows into Battles, reporting why each dropped row dropped.

    Rows are sorted by (recorded_at, battle_id) first: the RTDB export is a
    dict whose iteration order carries no meaning, and the bootstrap has to be
    reproducible.
    """
    dropped: Counter[str] = Counter()

    def sort_key(row: dict) -> tuple[str, str]:
        recorded_at = row.get("recorded_at")
        battle_id = row.get("battle_id")
        return (
            recorded_at if isinstance(recorded_at, str) else "",
            battle_id if isinstance(battle_id, str) else "",
        )

    battles: list[Battle] = []
    seen_battle_ids: set[str] = set()
    for row in sorted(rows, key=sort_key):
        if row.get("schema_version") != 1:
            dropped["schema_version"] += 1
            continue
        winner = row.get("winner")
        if winner not in WINNERS:
            dropped["winner"] += 1
            continue
        if row.get("engine") not in ENGINES:
            dropped["engine"] += 1
            continue
        battle_id = row.get("battle_id")
        client_id = row.get("client_id")
        recorded_at = row.get("recorded_at")
        if not (
            isinstance(battle_id, str)
            and battle_id
            and isinstance(client_id, str)
            and client_id
            and isinstance(recorded_at, str)
            and recorded_at
        ):
            dropped["metadata"] += 1
            continue
        if battle_id in seen_battle_ids:
            # B's queue can re-send a row after a partial flush, and the RTDB
            # key is a PoW digest, not the battle id.
            dropped["duplicate_battle_id"] += 1
            continue
        key_a = model_key(row, "a")
        key_b = model_key(row, "b")
        if key_a is None or key_b is None:
            dropped["identity"] += 1
            continue
        if key_a == key_b:
            dropped["self_battle"] += 1
            continue
        if any(
            _bounded_number(row.get(f"elapsed_{side}"), _MAX_ELAPSED) is None
            or _bounded_number(row.get(f"tokens_{side}"), _MAX_TOKENS) is None
            for side in SIDES
        ):
            dropped["timing"] += 1
            continue

        efficiency_a = _efficiency(row, "a")
        efficiency_b = _efficiency(row, "b")
        if efficiency_a is None or efficiency_b is None:
            # The axis needs both sides: its whole point is the ratio between
            # two models measured on the same machine.
            efficiency_a = efficiency_b = None

        seen_battle_ids.add(battle_id)
        battles.append(
            Battle(
                battle_id=battle_id,
                recorded_at=recorded_at,
                client_id=client_id,
                key_a=key_a,
                key_b=key_b,
                winner=winner,
                efficiency_a=efficiency_a,
                efficiency_b=efficiency_b,
            )
        )
    return battles, dict(dropped)


def battle_weights(battles: list[Battle]) -> list[float]:
    """Per-row weight capping what one machine can say about one pair.

    A client that voted three times on a pair keeps full weight. One that voted
    a thousand times on the same pair still contributes CLIENT_PAIR_VOTE_CAP
    effective votes in total.
    """
    sizes = Counter((battle.client_id, battle.pair) for battle in battles)
    return [
        min(1.0, CLIENT_PAIR_VOTE_CAP / sizes[(battle.client_id, battle.pair)])
        for battle in battles
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/arena_score.py tests/test_arena_score.py
git commit -m "$(cat <<'EOF'
feat(arena): validate and weight collected votes for scoring

Row validation with per-reason drop counts, sha256-first model identity,
battle_id deduplication, and the per-client per-pair weight cap that stops
one machine setting the ranking.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 2: Weighted Bradley-Terry quality fit

**Files:**
- Modify: `scripts/arena_score.py` (append)
- Test: `tests/test_arena_score.py` (append)

**Interfaces:**
- Consumes: `Battle`, `battle_weights`, `BT_PRIOR_STRENGTH`, `MAX_ITERATIONS`, `TOLERANCE` from Task 1.
- Produces: `fit_quality(battles: list[Battle], weights: list[float], *, prior: float = BT_PRIOR_STRENGTH) -> dict[str, float]` returning `{model_key: log-strength}` for every key appearing in `battles`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arena_score.py`:

```python
def _battle(key_a: str, key_b: str, winner: str, index: int, client: str = "abcdef12"):
    return arena_score.Battle(
        battle_id=f"{index:08d}-1111-1111-1111-111111111111",
        recorded_at="2026-09-26T10:00:00+00:00",
        client_id=client,
        key_a=key_a,
        key_b=key_b,
        winner=winner,
        efficiency_a=None,
        efficiency_b=None,
    )


def _fit(battles):
    return arena_score.fit_quality(battles, arena_score.battle_weights(battles))


def test_symmetric_results_give_equal_strengths():
    battles = [_battle("A", "B", "a", 0), _battle("A", "B", "b", 1)]
    strengths = _fit(battles)
    assert strengths["A"] == pytest.approx(strengths["B"])


def test_a_dominance_chain_comes_out_in_order():
    battles = []
    index = 0
    for _ in range(20):
        battles.append(_battle("A", "B", "a", index)); index += 1
        battles.append(_battle("B", "C", "a", index)); index += 1
    strengths = _fit(battles)
    assert strengths["A"] > strengths["B"] > strengths["C"]


def test_the_prior_shrinks_a_thin_undefeated_record():
    thin = [_battle("A", "B", "a", index, client=f"{index:08x}") for index in range(3)]
    thick = [_battle("A", "B", "a", index, client=f"{index:08x}") for index in range(30)]
    assert _fit(thin)["A"] < _fit(thick)["A"]


def test_both_bad_rows_do_not_feed_the_fit():
    with_both_bad = [
        _battle("A", "B", "a", 0),
        _battle("A", "B", "both_bad", 1),
        _battle("A", "B", "both_bad", 2),
    ]
    without = [_battle("A", "B", "a", 0)]
    assert _fit(with_both_bad)["A"] == pytest.approx(_fit(without)["A"])


def test_a_model_with_only_both_bad_battles_still_gets_a_finite_strength():
    # Zero wins and zero losses: the fit is prior-only. It must not be NaN.
    battles = [_battle("A", "B", "both_bad", index) for index in range(5)]
    strengths = _fit(battles)
    assert math.isfinite(strengths["A"])
    assert math.isfinite(strengths["B"])
    assert strengths["A"] == pytest.approx(strengths["B"])


def test_an_undefeated_model_does_not_diverge():
    battles = [_battle("A", "B", "a", index, client=f"{index:08x}") for index in range(50)]
    strengths = _fit(battles)
    assert math.isfinite(strengths["A"])
    assert strengths["A"] > strengths["B"]


def test_weights_are_honored_by_the_fit():
    # One client hammering one pair must not beat many clients voting the other
    # way, even with far more rows.
    spam = [_battle("A", "B", "a", index, client="deadbeef") for index in range(1000)]
    honest = [
        _battle("A", "B", "b", 2000 + index, client=f"{index:08x}") for index in range(30)
    ]
    strengths = _fit(spam + honest)
    assert strengths["B"] > strengths["A"]
```

Add the imports the new tests need at the top of the file, next to the existing ones:

```python
import math

import pytest
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: the 7 new tests fail with `AttributeError: module 'scripts.arena_score' has no attribute 'fit_quality'`. The 12 tests from Task 1 still pass.

- [ ] **Step 3: Write the implementation**

Append to `scripts/arena_score.py`:

```python
def fit_quality(
    battles: list[Battle],
    weights: list[float],
    *,
    prior: float = BT_PRIOR_STRENGTH,
) -> dict[str, float]:
    """Weighted Bradley-Terry log-strengths, one per model key.

    Minorization-maximization iteration:

        p_i  <-  (W_i + prior) / ( sum_j n_ij/(p_i + p_j) + 2*prior/(p_i + 1) )

    where W_i is i's total weighted wins and n_ij the total weighted
    comparisons between i and j. `both_bad` rows are excluded from the fit and
    tracked separately by `both_bad_rates`.

    The trailing term is a phantom opponent of fixed strength 1 carrying
    `prior` wins and `prior` losses for every model. It keeps the MLE finite
    for an undefeated or never-winning model, connects the comparison graph so
    this axis needs no component handling, and shrinks thin records toward the
    middle. Because the phantom's strength is fixed, the scale is already
    identified - do not renormalize the result.
    """
    keys = sorted({key for battle in battles for key in (battle.key_a, battle.key_b)})
    if not keys:
        return {}

    wins: dict[str, float] = {key: 0.0 for key in keys}
    comparisons: dict[str, dict[str, float]] = {key: defaultdict(float) for key in keys}
    for battle, weight in zip(battles, weights):
        if battle.winner == "a":
            winner, loser = battle.key_a, battle.key_b
        elif battle.winner == "b":
            winner, loser = battle.key_b, battle.key_a
        else:
            continue
        wins[winner] += weight
        comparisons[winner][loser] += weight
        comparisons[loser][winner] += weight

    strengths = {key: 1.0 for key in keys}
    for _ in range(MAX_ITERATIONS):
        change = 0.0
        for key in keys:
            own = strengths[key]
            denominator = 2.0 * prior / (own + 1.0)
            for opponent, total in comparisons[key].items():
                denominator += total / (own + strengths[opponent])
            updated = (wins[key] + prior) / denominator
            change = max(change, abs(updated - own) / max(own, 1e-12))
            strengths[key] = updated
        if change < TOLERANCE:
            break
    return {key: math.log(value) for key, value in strengths.items()}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/arena_score.py tests/test_arena_score.py
git commit -m "$(cat <<'EOF'
feat(arena): fit weighted Bradley-Terry quality strengths

MM iteration with a fixed-strength phantom opponent, so an undefeated or
never-winning model gets a finite strength, the comparison graph is always
connected, and a thin record is shrunk toward the middle.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 3: Bootstrap intervals, tiers, both_bad rates

**Files:**
- Modify: `scripts/arena_score.py` (append)
- Test: `tests/test_arena_score.py` (append)

**Interfaces:**
- Consumes: `Battle`, `battle_weights`, `fit_quality`, `BOOTSTRAP_RESAMPLES`, `BOOTSTRAP_SEED`, `BOTH_BAD_WARNING_RATE` from Tasks 1-2.
- Produces:
  - `effective_battles(battles, weights) -> dict[str, float]`
  - `both_bad_rates(battles, weights) -> dict[str, float]`
  - `bootstrap_intervals(battles, *, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED) -> dict[str, tuple[float, float]]`
  - `assign_tiers(ranked: list[tuple[str, float, float, float]]) -> dict[str, int]` where each entry is `(key, strength, ci_low, ci_high)` sorted by descending strength.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arena_score.py`:

```python
def test_effective_battles_counts_weight_on_both_sides():
    battles = [_battle("A", "B", "a", 0), _battle("A", "C", "a", 1)]
    counts = arena_score.effective_battles(battles, arena_score.battle_weights(battles))
    assert counts == {"A": 2.0, "B": 1.0, "C": 1.0}


def test_both_bad_rate_is_weighted_and_per_model():
    battles = [
        _battle("A", "B", "both_bad", 0),
        _battle("A", "B", "a", 1),
        _battle("A", "C", "a", 2),
    ]
    rates = arena_score.both_bad_rates(battles, arena_score.battle_weights(battles))
    assert rates["A"] == pytest.approx(1 / 3)
    assert rates["B"] == pytest.approx(1 / 2)
    assert rates["C"] == pytest.approx(0.0)


def test_a_model_with_only_both_bad_battles_has_rate_one():
    battles = [_battle("A", "B", "both_bad", index) for index in range(4)]
    rates = arena_score.both_bad_rates(battles, arena_score.battle_weights(battles))
    assert rates["A"] == pytest.approx(1.0)
    assert rates["A"] >= arena_score.BOTH_BAD_WARNING_RATE


def test_bootstrap_intervals_bracket_the_point_estimate():
    battles = []
    for index in range(60):
        winner = "a" if index % 4 else "b"
        battles.append(_battle("A", "B", winner, index, client=f"{index:08x}"))
    strengths = _fit(battles)
    intervals = arena_score.bootstrap_intervals(battles, resamples=40)
    low, high = intervals["A"]
    assert low <= strengths["A"] <= high
    assert low < high


def test_bootstrap_intervals_are_reproducible():
    battles = [
        _battle("A", "B", "a" if index % 3 else "b", index, client=f"{index:08x}")
        for index in range(30)
    ]
    first = arena_score.bootstrap_intervals(battles, resamples=25)
    second = arena_score.bootstrap_intervals(battles, resamples=25)
    assert first == second


def test_overlapping_intervals_share_a_tier():
    ranked = [
        ("A", 1.0, 0.5, 1.5),
        ("B", 0.9, 0.4, 1.4),
    ]
    assert arena_score.assign_tiers(ranked) == {"A": 1, "B": 1}


def test_a_separated_model_opens_the_next_tier():
    ranked = [
        ("A", 1.0, 0.8, 1.2),
        ("B", 0.1, -0.1, 0.3),
    ]
    assert arena_score.assign_tiers(ranked) == {"A": 1, "B": 2}


def test_tiers_anchor_on_the_leader_not_the_previous_model():
    # A~B and B~C overlap pairwise, but C is disjoint from A. Chaining would
    # put all three in one tier and let it drift arbitrarily wide.
    ranked = [
        ("A", 1.0, 0.90, 1.10),
        ("B", 0.95, 0.85, 1.05),
        ("C", 0.80, 0.70, 0.89),
    ]
    assert arena_score.assign_tiers(ranked) == {"A": 1, "B": 1, "C": 2}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: the 8 new tests fail with `AttributeError` on `effective_battles`, `both_bad_rates`, `bootstrap_intervals`, `assign_tiers`.

- [ ] **Step 3: Write the implementation**

Append to `scripts/arena_score.py`, and add `import random` to the import block at the top of the file:

```python
def effective_battles(battles: list[Battle], weights: list[float]) -> dict[str, float]:
    """Total weight each model took part in, on either side."""
    totals: dict[str, float] = defaultdict(float)
    for battle, weight in zip(battles, weights):
        totals[battle.key_a] += weight
        totals[battle.key_b] += weight
    return dict(totals)


def both_bad_rates(battles: list[Battle], weights: list[float]) -> dict[str, float]:
    """Share of a model's weighted battles the user rejected outright.

    Reported next to the tier, never folded into it: "wins its matchups" and
    "often fails outright" are different facts about a model.
    """
    totals: dict[str, float] = defaultdict(float)
    rejected: dict[str, float] = defaultdict(float)
    for battle, weight in zip(battles, weights):
        for key in (battle.key_a, battle.key_b):
            totals[key] += weight
            if battle.winner == "both_bad":
                rejected[key] += weight
    return {
        key: (rejected[key] / total if total else 0.0) for key, total in totals.items()
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_intervals(
    battles: list[Battle],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, tuple[float, float]]:
    """95% percentile intervals on each model's log-strength.

    Resamples battles with replacement and refits, recomputing the abuse
    weights inside each resample so a resample that happens to repeat one
    client's rows is capped the same way the real corpus is.

    The seed is fixed: the nightly job commits this artifact, so an unchanged
    corpus has to produce an unchanged file.
    """
    keys = sorted({key for battle in battles for key in (battle.key_a, battle.key_b)})
    if not battles or not keys:
        return {}
    point = fit_quality(battles, battle_weights(battles))
    samples: dict[str, list[float]] = {key: [] for key in keys}
    generator = random.Random(seed)
    size = len(battles)
    for _ in range(resamples):
        picked = [battles[generator.randrange(size)] for _ in range(size)]
        fitted = fit_quality(picked, battle_weights(picked))
        for key, strength in fitted.items():
            samples[key].append(strength)
    intervals: dict[str, tuple[float, float]] = {}
    for key in keys:
        drawn = samples[key]
        if len(drawn) < 2:
            # The model appeared in almost no resample; there is nothing to
            # spread. Its effective battle count will mark it provisional.
            intervals[key] = (point[key], point[key])
            continue
        intervals[key] = (_percentile(drawn, 0.025), _percentile(drawn, 0.975))
    return intervals


def assign_tiers(ranked: list[tuple[str, float, float, float]]) -> dict[str, int]:
    """Group models whose interval overlaps their tier leader's.

    `ranked` is (key, strength, ci_low, ci_high), descending by strength.

    The anchor is the tier's leader, not the previous model. Interval overlap
    is not transitive - A overlaps B and B overlaps C while A and C are
    disjoint - so chaining would let one tier grow without bound. Anchoring on
    the leader makes a tier mean "not distinguishable from this tier's best",
    which holds for every member.
    """
    tiers: dict[str, int] = {}
    leader_low: float | None = None
    tier = 0
    for key, _strength, ci_low, ci_high in ranked:
        if leader_low is None or ci_high < leader_low:
            tier += 1
            leader_low = ci_low
        tiers[key] = tier
    return tiers
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: 27 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/arena_score.py tests/test_arena_score.py
git commit -m "$(cat <<'EOF'
feat(arena): add bootstrap intervals, tiers, and both_bad rates

Seeded bootstrap over battles for reproducible 95% intervals, tier grouping
anchored on each tier's leader rather than chained pairwise, and a weighted
per-model both_bad rate kept separate from the ranking.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 4: Machine-independent efficiency ratings

**Files:**
- Modify: `scripts/arena_score.py` (append)
- Test: `tests/test_arena_score.py` (append)

**Interfaces:**
- Consumes: `Battle`, `MAX_ITERATIONS`, `TOLERANCE` from Task 1.
- Produces: `fit_efficiency(battles: list[Battle], weights: list[float]) -> dict[str, dict]`, one entry per model with an eligible row: `{"rating": float, "component": int, "raw_median_tok_s_per_gb": float, "sample": int}`. Models with no eligible row are absent from the mapping.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arena_score.py`:

```python
def _measured(key_a, key_b, e_a, e_b, index, client="abcdef12", winner="a"):
    return arena_score.Battle(
        battle_id=f"{index:08d}-1111-1111-1111-111111111111",
        recorded_at="2026-09-26T10:00:00+00:00",
        client_id=client,
        key_a=key_a,
        key_b=key_b,
        winner=winner,
        efficiency_a=e_a,
        efficiency_b=e_b,
    )


def test_a_uniformly_faster_machine_does_not_change_the_ratings():
    # The whole justification for the paired design. One machine is 4x faster
    # across the board; the ratings must be identical.
    slow = [_measured("A", "B", 7.5, 5.0, 0, client="1111aaaa")]
    fast = [_measured("A", "B", 30.0, 20.0, 1, client="2222bbbb")]
    slow_fit = arena_score.fit_efficiency(slow, arena_score.battle_weights(slow))
    fast_fit = arena_score.fit_efficiency(fast, arena_score.battle_weights(fast))
    assert slow_fit["A"]["rating"] == pytest.approx(fast_fit["A"]["rating"])
    assert slow_fit["B"]["rating"] == pytest.approx(fast_fit["B"]["rating"])


def test_the_raw_median_does_not_cancel_the_machine():
    # Same fixture, proving the reference number is only a reference number.
    slow = [_measured("A", "B", 7.5, 5.0, 0)]
    fast = [_measured("A", "B", 30.0, 20.0, 1)]
    slow_fit = arena_score.fit_efficiency(slow, arena_score.battle_weights(slow))
    fast_fit = arena_score.fit_efficiency(fast, arena_score.battle_weights(fast))
    assert slow_fit["A"]["raw_median_tok_s_per_gb"] == pytest.approx(7.5)
    assert fast_fit["A"]["raw_median_tok_s_per_gb"] == pytest.approx(30.0)


def test_the_more_efficient_model_rates_higher():
    battles = [_measured("A", "B", 30.0, 20.0, index) for index in range(5)]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["rating"] > fitted["B"]["rating"]


def test_ratings_are_centered_inside_a_component():
    battles = [_measured("A", "B", 30.0, 20.0, index) for index in range(5)]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["rating"] + fitted["B"]["rating"] == pytest.approx(0.0)


def test_a_transitive_chain_recovers_the_ratio_it_never_measured():
    battles = [
        _measured("A", "B", 30.0, 20.0, 0),
        _measured("B", "C", 20.0, 10.0, 1),
    ]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    recovered = math.exp(fitted["A"]["rating"] - fitted["C"]["rating"])
    assert recovered == pytest.approx(3.0, rel=1e-6)


def test_disconnected_groups_get_different_components():
    battles = [
        _measured("A", "B", 30.0, 20.0, 0),
        _measured("C", "D", 30.0, 20.0, 1),
    ]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["component"] == fitted["B"]["component"]
    assert fitted["C"]["component"] == fitted["D"]["component"]
    assert fitted["A"]["component"] != fitted["C"]["component"]


def test_the_largest_component_is_component_zero():
    battles = [
        _measured("A", "B", 30.0, 20.0, 0),
        _measured("B", "C", 20.0, 10.0, 1),
        _measured("D", "E", 30.0, 20.0, 2),
    ]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["component"] == 0
    assert fitted["D"]["component"] == 1


def test_a_corpus_with_no_measurements_yields_no_ratings():
    # Every row from LM Studio, which exposes no memory API.
    battles = [_battle("A", "B", "a", index) for index in range(5)]
    assert arena_score.fit_efficiency(battles, arena_score.battle_weights(battles)) == {}


def test_both_bad_rows_still_carry_usable_measurements():
    battles = [_measured("A", "B", 30.0, 20.0, 0, winner="both_bad")]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["rating"] > fitted["B"]["rating"]


def test_the_sample_count_is_the_number_of_eligible_rows():
    battles = [_measured("A", "B", 30.0, 20.0, index) for index in range(3)]
    battles.append(_battle("A", "B", "a", 99))
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["sample"] == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: the 10 new tests fail with `AttributeError: ... has no attribute 'fit_efficiency'`.

- [ ] **Step 3: Write the implementation**

Append to `scripts/arena_score.py`, and add `import statistics` to the import block:

```python
def _components(adjacency: dict[str, list[tuple[str, float, float]]]) -> dict[str, int]:
    """Connected components of the efficiency graph, numbered by size.

    Component 0 is the largest (ties broken by lowest member key), so the
    consumer's "main board" is stable across runs.
    """
    seen: set[str] = set()
    groups: list[list[str]] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        group = []
        while stack:
            key = stack.pop()
            group.append(key)
            for neighbour, _ratio, _weight in adjacency[key]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        groups.append(sorted(group))
    groups.sort(key=lambda group: (-len(group), group[0]))
    return {key: index for index, group in enumerate(groups) for key in group}


def fit_efficiency(battles: list[Battle], weights: list[float]) -> dict[str, dict]:
    """Per-model efficiency ratings fitted from within-battle log-ratios.

    tokens/sec per GiB on its own measures the machine, not the model: the same
    model scores 30 on one GPU and 7.5 on another. Both sides of an arena row
    were measured on the same machine in the same session, so the ratio between
    them cancels the machine - 30/20 and 7.5/5.0 are both 1.5.

    For each eligible row, r = ln(e_a) - ln(e_b). Ratings f minimize
    sum(weight * (r - (f_a - f_b))^2), solved by weighted iterative averaging.
    Only differences are determined, so each connected component is centered on
    its own weighted mean and ratings from different components are not
    comparable - hence the reported `component`.
    """
    adjacency: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    observations: dict[str, list[float]] = defaultdict(list)
    mass: dict[str, float] = defaultdict(float)
    for battle, weight in zip(battles, weights):
        if battle.efficiency_a is None or battle.efficiency_b is None:
            continue
        ratio = math.log(battle.efficiency_a) - math.log(battle.efficiency_b)
        adjacency[battle.key_a].append((battle.key_b, ratio, weight))
        adjacency[battle.key_b].append((battle.key_a, -ratio, weight))
        observations[battle.key_a].append(battle.efficiency_a)
        observations[battle.key_b].append(battle.efficiency_b)
        mass[battle.key_a] += weight
        mass[battle.key_b] += weight
    if not adjacency:
        return {}

    keys = sorted(adjacency)
    ratings = {key: 0.0 for key in keys}
    for _ in range(MAX_ITERATIONS):
        change = 0.0
        for key in keys:
            numerator = 0.0
            denominator = 0.0
            for neighbour, ratio, weight in adjacency[key]:
                numerator += weight * (ratings[neighbour] + ratio)
                denominator += weight
            updated = numerator / denominator
            change = max(change, abs(updated - ratings[key]))
            ratings[key] = updated
        if change < TOLERANCE:
            break

    component_of = _components(adjacency)
    grouped: dict[int, list[str]] = defaultdict(list)
    for key in keys:
        grouped[component_of[key]].append(key)
    for members in grouped.values():
        total = sum(mass[key] for key in members)
        centre = (
            sum(ratings[key] * mass[key] for key in members) / total if total else 0.0
        )
        for key in members:
            ratings[key] -= centre

    return {
        key: {
            "rating": ratings[key],
            "component": component_of[key],
            "raw_median_tok_s_per_gb": statistics.median(observations[key]),
            "sample": len(observations[key]),
        }
        for key in keys
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: 37 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/arena_score.py tests/test_arena_score.py
git commit -m "$(cat <<'EOF'
feat(arena): fit machine-independent efficiency ratings

Within-battle log-ratios cancel the machine, so a model is not credited for
running on a bigger GPU. Ratings are fitted per connected component and
centered inside it; the raw tokens/sec per GiB median stays a reference
number, never the sort key.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 5: Artifact assembly

**Files:**
- Modify: `scripts/arena_score.py` (append)
- Test: `tests/test_arena_score.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces:
  - `ARTIFACT_SCHEMA_VERSION = 1`
  - `collect_metadata(rows: list[dict]) -> dict[str, dict]` mapping model key to `{"display_filename", "repo_id", "provider", "quant_bits"}`
  - `build_artifact(rows: list[dict], *, generated_at: str, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> dict`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arena_score.py`:

```python
def _corpus(count: int = 60) -> list[dict]:
    """A corpus large enough that both models clear MIN_EFFECTIVE_BATTLES."""
    rows = []
    for index in range(count):
        rows.append(
            _row(
                battle_id=f"{index:08d}-1111-1111-1111-111111111111",
                client_id=f"{index:08x}",
                winner="a" if index % 4 else "b",
            )
        )
    return rows


def test_the_artifact_reports_the_corpus_it_used():
    artifact = arena_score.build_artifact(
        _corpus(), generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert artifact["schema_version"] == arena_score.ARTIFACT_SCHEMA_VERSION
    assert artifact["generated_at"] == "2026-09-27T04:00:00+00:00"
    assert artifact["corpus"]["rows_fetched"] == 60
    assert artifact["corpus"]["rows_used"] == 60
    assert artifact["corpus"]["client_count"] == 60
    assert artifact["corpus"]["effective_votes"] == pytest.approx(60.0)


def test_drop_reasons_are_reported_not_absorbed():
    rows = _corpus(4) + [_row(battle_id="ffffffff-1111-1111-1111-111111111111",
                              winner="tie")]
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    assert artifact["corpus"]["rows_fetched"] == 5
    assert artifact["corpus"]["rows_used"] == 4
    assert artifact["corpus"]["rows_dropped"] == {"winner": 1}


def test_a_thin_model_is_provisional_and_untiered():
    artifact = arena_score.build_artifact(
        _corpus(4), generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    entry = artifact["models"][0]
    assert entry["provisional"] is True
    assert entry["quality"]["tier"] is None
    assert artifact["tiers"] == []
    assert sorted(artifact["provisional_models"]) == sorted(
        model["key"] for model in artifact["models"]
    )


def test_a_well_supported_model_gets_a_tier():
    artifact = arena_score.build_artifact(
        _corpus(), generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert artifact["provisional_models"] == []
    assert all(model["quality"]["tier"] is not None for model in artifact["models"])
    assert artifact["tiers"][0]["tier"] == 1


def test_metadata_travels_with_the_model():
    rows = _corpus()
    for row in rows:
        row["model_provider_a"] = "huggingface"
        row["model_repo_id_a"] = "Vendor/Alpha-GGUF"
        row["quant_bits_a"] = 4.0
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    alpha = next(m for m in artifact["models"] if m["key"] == "filename:alpha-q4_k_m.gguf")
    assert alpha["display_filename"] == "alpha-q4_k_m.gguf"
    assert alpha["repo_id"] == "Vendor/Alpha-GGUF"
    assert alpha["provider"] == "huggingface"
    assert alpha["quant_bits"] == 4.0


def test_the_artifact_is_byte_identical_for_a_shuffled_corpus():
    import json
    import random as stdlib_random

    rows = _corpus()
    shuffled = list(rows)
    stdlib_random.Random(7).shuffle(shuffled)
    first = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    second = arena_score.build_artifact(
        shuffled, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_efficiency_is_null_when_nothing_was_measured():
    rows = _corpus()
    for row in rows:
        del row["memory_gb_a"]
        del row["memory_gb_b"]
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert all(model["efficiency"] is None for model in artifact["models"])
    assert artifact["efficiency_components"] == []


def test_an_empty_corpus_produces_an_empty_but_valid_artifact():
    artifact = arena_score.build_artifact(
        [], generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    assert artifact["models"] == []
    assert artifact["tiers"] == []
    assert artifact["corpus"]["effective_votes"] == 0.0
    assert artifact["corpus"]["largest_client_share"] == 0.0


def test_the_largest_client_share_is_reported():
    rows = [
        _row(battle_id=f"{index:08d}-1111-1111-1111-111111111111", client_id="deadbeef")
        for index in range(30)
    ]
    rows += [
        _row(battle_id=f"{100 + index:08d}-1111-1111-1111-111111111111",
             client_id=f"{index:08x}")
        for index in range(10)
    ]
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    # The spammer's 30 rows are capped to 10 effective votes against 10 honest
    # ones, so its share is half - not 75%.
    assert artifact["corpus"]["largest_client_share"] == pytest.approx(0.5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: the 9 new tests fail with `AttributeError: ... has no attribute 'build_artifact'`.

- [ ] **Step 3: Write the implementation**

Append to `scripts/arena_score.py`:

```python
ARTIFACT_SCHEMA_VERSION = 1

_METADATA_FIELDS = (
    ("display_filename", "model_filename"),
    ("repo_id", "model_repo_id"),
    ("provider", "model_provider"),
    ("quant_bits", "quant_bits"),
)


def collect_metadata(rows: list[dict]) -> dict[str, dict]:
    """Display metadata per model key, last non-empty value winning.

    Rows arrive sorted ascending by recorded_at, so "last" means "most
    recently reported" - a renamed file or a newly known repo id wins over a
    stale one. Identity fields are independently optional per side, so a key
    can end up with a filename and nothing else.
    """
    metadata: dict[str, dict] = {}
    for row in sorted(
        rows,
        key=lambda item: (
            item.get("recorded_at") if isinstance(item.get("recorded_at"), str) else "",
            item.get("battle_id") if isinstance(item.get("battle_id"), str) else "",
        ),
    ):
        for side in SIDES:
            key = model_key(row, side)
            if key is None:
                continue
            entry = metadata.setdefault(
                key, {name: None for name, _field in _METADATA_FIELDS}
            )
            for name, field in _METADATA_FIELDS:
                value = row.get(f"{field}_{side}")
                if isinstance(value, str) and value.strip():
                    entry[name] = value.strip()
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    entry[name] = float(value)
    return metadata


def build_artifact(
    rows: list[dict],
    *,
    generated_at: str,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """The full leaderboard artifact, deterministic for a given corpus."""
    battles, dropped = validate_rows(rows)
    weights = battle_weights(battles)
    metadata = collect_metadata(rows)

    total_weight = sum(weights)
    per_client: dict[str, float] = defaultdict(float)
    for battle, weight in zip(battles, weights):
        per_client[battle.client_id] += weight

    strengths = fit_quality(battles, weights)
    intervals = bootstrap_intervals(battles, resamples=resamples, seed=seed)
    counts = effective_battles(battles, weights)
    rates = both_bad_rates(battles, weights)
    efficiency = fit_efficiency(battles, weights)

    raw_battles: dict[str, int] = defaultdict(int)
    for battle in battles:
        raw_battles[battle.key_a] += 1
        raw_battles[battle.key_b] += 1

    ranked_input = [
        (key, strengths[key], intervals[key][0], intervals[key][1])
        for key in sorted(
            strengths, key=lambda item: (-strengths[item], item)
        )
        if counts.get(key, 0.0) >= MIN_EFFECTIVE_BATTLES
    ]
    tiers = assign_tiers(ranked_input)

    models = []
    for key in sorted(strengths):
        low, high = intervals[key]
        tier = tiers.get(key)
        entry = {
            "key": key,
            "display_filename": metadata.get(key, {}).get("display_filename"),
            "repo_id": metadata.get(key, {}).get("repo_id"),
            "provider": metadata.get(key, {}).get("provider"),
            "quant_bits": metadata.get(key, {}).get("quant_bits"),
            "battles": raw_battles[key],
            "effective_battles": counts.get(key, 0.0),
            "provisional": tier is None,
            "quality": {
                "strength": strengths[key],
                "ci_low": low,
                "ci_high": high,
                "tier": tier,
            },
            "both_bad_rate": rates.get(key, 0.0),
            "quality_warning": (
                rates.get(key, 0.0) >= BOTH_BAD_WARNING_RATE
                and counts.get(key, 0.0) >= MIN_EFFECTIVE_BATTLES
            ),
            "efficiency": efficiency.get(key),
        }
        models.append(entry)

    def order(entry: dict) -> tuple:
        tier = entry["quality"]["tier"]
        measured = entry["efficiency"]
        return (
            tier if tier is not None else math.inf,
            -measured["rating"] if measured else math.inf,
            entry["key"],
        )

    models.sort(key=order)

    grouped_tiers: dict[int, list[str]] = defaultdict(list)
    for entry in models:
        tier = entry["quality"]["tier"]
        if tier is not None:
            grouped_tiers[tier].append(entry["key"])

    component_sizes: dict[int, int] = defaultdict(int)
    for measured in efficiency.values():
        component_sizes[measured["component"]] += 1

    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "corpus": {
            "rows_fetched": len(rows),
            "rows_used": len(battles),
            "rows_dropped": dropped,
            "effective_votes": total_weight,
            "client_count": len(per_client),
            "largest_client_share": (
                max(per_client.values()) / total_weight if total_weight else 0.0
            ),
        },
        "models": models,
        "tiers": [
            {"tier": tier, "model_keys": grouped_tiers[tier]}
            for tier in sorted(grouped_tiers)
        ],
        "efficiency_components": [
            {"component": component, "model_count": component_sizes[component]}
            for component in sorted(component_sizes)
        ],
        "provisional_models": sorted(
            entry["key"] for entry in models if entry["provisional"]
        ),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena_score.py -q`
Expected: 46 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/arena_score.py tests/test_arena_score.py
git commit -m "$(cat <<'EOF'
feat(arena): assemble the leaderboard artifact

Quality tiers, efficiency ratings, both_bad rates, drop-reason counts and
corpus provenance in one deterministic document: the same corpus in any input
order produces a byte-identical file, so the nightly job never commits noise.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 6: Publication quality gate

**Files:**
- Create: `scripts/arena_quality_gate.py`
- Test: `tests/test_arena_quality_gate.py`

**Interfaces:**
- Consumes: the artifact dict shape from Task 5.
- Produces:
  - `MIN_EFFECTIVE_VOTES = 200`, `MAX_SINGLE_CLIENT_SHARE = 0.5`, `MIN_RANKED_MODELS = 5`, `MAX_RANKED_MODEL_DROP = 0.3`
  - `@dataclass(frozen=True) class GateResult` with `status: str` (`"passed"` or `"blocked"`) and `reasons: tuple[str, ...]`
  - `ranked_model_count(artifact: dict) -> int`
  - `evaluate(artifact: dict, incumbent: dict | None = None) -> GateResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arena_quality_gate.py`:

```python
"""Publication gate for the arena leaderboard artifact."""

from __future__ import annotations

from scripts import arena_quality_gate


def _artifact(*, votes=500.0, share=0.2, ranked=8) -> dict:
    models = [
        {
            "key": f"filename:model-{index}.gguf",
            "provisional": False,
            "quality": {"tier": 1},
        }
        for index in range(ranked)
    ]
    return {
        "schema_version": 1,
        "generated_at": "2026-09-27T04:00:00+00:00",
        "corpus": {
            "rows_fetched": 900,
            "rows_used": 880,
            "rows_dropped": {},
            "effective_votes": votes,
            "client_count": 40,
            "largest_client_share": share,
        },
        "models": models,
        "tiers": [{"tier": 1, "model_keys": [m["key"] for m in models]}],
        "efficiency_components": [],
        "provisional_models": [],
    }


def test_a_healthy_artifact_passes():
    assert arena_quality_gate.evaluate(_artifact()).status == "passed"


def test_too_little_data_is_blocked():
    result = arena_quality_gate.evaluate(_artifact(votes=42.0))
    assert result.status == "blocked"
    assert any("effective votes" in reason for reason in result.reasons)


def test_one_machine_dominating_is_blocked():
    result = arena_quality_gate.evaluate(_artifact(share=0.81))
    assert result.status == "blocked"
    assert any("client" in reason for reason in result.reasons)


def test_too_few_ranked_models_is_blocked():
    result = arena_quality_gate.evaluate(_artifact(ranked=3))
    assert result.status == "blocked"
    assert any("ranked models" in reason for reason in result.reasons)


def test_a_collapse_against_the_incumbent_is_blocked():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=6), incumbent=_artifact(ranked=20)
    )
    assert result.status == "blocked"
    assert any("shrank" in reason for reason in result.reasons)


def test_a_mild_change_against_the_incumbent_passes():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=18), incumbent=_artifact(ranked=20)
    )
    assert result.status == "passed"


def test_growth_against_the_incumbent_passes():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=40), incumbent=_artifact(ranked=20)
    )
    assert result.status == "passed"


def test_the_first_run_has_no_incumbent_and_skips_the_collapse_check():
    result = arena_quality_gate.evaluate(_artifact(ranked=6), incumbent=None)
    assert result.status == "passed"


def test_an_incumbent_with_no_ranked_models_skips_the_collapse_check():
    result = arena_quality_gate.evaluate(
        _artifact(ranked=6), incumbent=_artifact(ranked=0)
    )
    assert result.status == "passed"


def test_every_failing_condition_is_reported_not_just_the_first():
    result = arena_quality_gate.evaluate(_artifact(votes=10.0, share=0.9, ranked=1))
    assert result.status == "blocked"
    assert len(result.reasons) == 3


def test_provisional_models_do_not_count_as_ranked():
    artifact = _artifact(ranked=8)
    for model in artifact["models"][4:]:
        model["provisional"] = True
        model["quality"]["tier"] = None
    assert arena_quality_gate.ranked_model_count(artifact) == 4
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena_quality_gate.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.arena_quality_gate'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/arena_quality_gate.py`:

```python
#!/usr/bin/env python3
"""Decide whether a computed arena leaderboard may be published.

Sub-project C's counterpart to scripts/model_quality_gate.py. A blocked run is
a success that publishes nothing, not a failed workflow: the nightly job must
not go red because the corpus is thin.

Spec: docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md
"""

from __future__ import annotations

from dataclasses import dataclass

#: A leaderboard fitted from a handful of battles is noise presented as fact.
MIN_EFFECTIVE_VOTES = 200

#: The telemetry corpus is already overwhelmingly one machine. Catch the same
#: failure here before publication, not after.
MAX_SINGLE_CLIENT_SHARE = 0.5

MIN_RANKED_MODELS = 5

#: A sudden collapse in ranked models is a corpus or parsing regression, not a
#: real change in what people run.
MAX_RANKED_MODEL_DROP = 0.3


@dataclass(frozen=True)
class GateResult:
    status: str
    reasons: tuple[str, ...]


def ranked_model_count(artifact: dict) -> int:
    """Models that actually carry a tier. Provisional entries do not count."""
    models = artifact.get("models")
    if not isinstance(models, list):
        return 0
    return sum(1 for model in models if not model.get("provisional", True))


def evaluate(artifact: dict, incumbent: dict | None = None) -> GateResult:
    """Every failing condition is reported, not just the first one."""
    corpus = artifact.get("corpus") or {}
    reasons: list[str] = []

    votes = float(corpus.get("effective_votes") or 0.0)
    if votes < MIN_EFFECTIVE_VOTES:
        reasons.append(
            f"only {votes:.1f} effective votes, need {MIN_EFFECTIVE_VOTES}"
        )

    share = float(corpus.get("largest_client_share") or 0.0)
    if share > MAX_SINGLE_CLIENT_SHARE:
        reasons.append(
            f"one client holds {share:.0%} of the effective weight, "
            f"limit {MAX_SINGLE_CLIENT_SHARE:.0%}"
        )

    ranked = ranked_model_count(artifact)
    if ranked < MIN_RANKED_MODELS:
        reasons.append(f"only {ranked} ranked models, need {MIN_RANKED_MODELS}")

    previous = ranked_model_count(incumbent) if incumbent else 0
    if previous > 0:
        drop = (previous - ranked) / previous
        if drop > MAX_RANKED_MODEL_DROP:
            reasons.append(
                f"ranked models shrank {drop:.0%} against the incumbent "
                f"({previous} -> {ranked}), limit {MAX_RANKED_MODEL_DROP:.0%}"
            )

    return GateResult(
        status="blocked" if reasons else "passed", reasons=tuple(reasons)
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena_quality_gate.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/arena_quality_gate.py tests/test_arena_quality_gate.py
git commit -m "$(cat <<'EOF'
feat(arena): gate leaderboard publication on corpus health

Refuses to publish on too little data, one machine holding most of the
weight, too few ranked models, or a collapse against the incumbent. Every
failing condition is reported, and a block is a success that publishes
nothing rather than a red workflow.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 7: The aggregator CLI and the authenticated fetch

**Files:**
- Create: `scripts/aggregate_arena_votes.py`
- Test: `tests/test_aggregate_arena_votes.py`

**Interfaces:**
- Consumes: `scripts.arena_score.build_artifact`, `scripts.arena_quality_gate.evaluate`, `omm.atomic.atomic_write_text`, `scripts.retrain_decision.append_github_outputs`.
- Produces:
  - `ARENA_MAX_ROWS = 100000`
  - `is_firebase_rtdb_json_url(url: str) -> bool`
  - `VotesFetchError(RuntimeError)`
  - `fetch_votes(url: str, token: str | None, *, limit: int = ARENA_MAX_ROWS) -> list[dict]`
  - `extract_rows(payload: object) -> list[dict]`
  - `load_votes_file(path: Path) -> list[dict]`
  - `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aggregate_arena_votes.py`:

```python
"""The arena aggregation CLI: fetching, gating, and writing the artifact."""

from __future__ import annotations

import json

import pytest

from scripts import aggregate_arena_votes as aggregate


def _row(index: int, client: str, winner: str) -> dict:
    return {
        "schema_version": 1,
        "battle_id": f"{index:08d}-1111-1111-1111-111111111111",
        "client_id": client,
        "recorded_at": "2026-09-26T10:00:00+00:00",
        "engine": "ollama",
        "winner": winner,
        "model_filename_a": f"alpha-{index % 6}.gguf",
        "model_filename_b": f"beta-{index % 5}.gguf",
        "elapsed_a": 4.0,
        "elapsed_b": 5.0,
        "tokens_a": 100,
        "tokens_b": 120,
        "tokens_per_second_a": 40.0,
        "tokens_per_second_b": 20.0,
        "memory_gb_a": 4.0,
        "memory_gb_b": 4.0,
    }


def _healthy_rows(count: int = 600) -> list[dict]:
    return [
        _row(index, f"{index:08x}", "a" if index % 3 else "b") for index in range(count)
    ]


def test_a_firebase_dict_export_is_parsed():
    rows = aggregate.extract_rows({"powdigest1": _row(0, "aa", "a")})
    assert len(rows) == 1
    assert rows[0]["battle_id"].startswith("00000000")


def test_a_votes_wrapped_export_is_parsed():
    rows = aggregate.extract_rows({"votes": {"powdigest1": _row(0, "aa", "a")}})
    assert len(rows) == 1


def test_a_bare_list_export_is_parsed():
    assert len(aggregate.extract_rows([_row(0, "aa", "a")])) == 1


def test_a_null_export_is_an_empty_corpus():
    assert aggregate.extract_rows(None) == []


def test_non_dict_entries_are_ignored():
    assert aggregate.extract_rows({"a": _row(0, "aa", "a"), "b": "junk"}) == [
        _row(0, "aa", "a")
    ]


def test_an_https_firebase_json_url_is_accepted():
    assert aggregate.is_firebase_rtdb_json_url(
        "https://localfit-8ab57.firebaseio.com/votes.json"
    )


def test_a_non_firebase_host_is_rejected():
    assert not aggregate.is_firebase_rtdb_json_url("https://example.com/votes.json")


def test_a_path_without_json_is_rejected():
    assert not aggregate.is_firebase_rtdb_json_url(
        "https://localfit-8ab57.firebaseio.com/votes"
    )


def test_a_missing_credential_for_a_private_node_is_a_hard_error():
    # An unauthenticated read of a .read:false node returns 401. Treating that
    # as "no votes yet" would publish an empty leaderboard.
    with pytest.raises(aggregate.VotesFetchError, match="credential"):
        aggregate.fetch_votes(
            "https://localfit-8ab57.firebaseio.com/votes.json", None
        )


def test_the_credential_travels_in_the_header_not_the_url(monkeypatch):
    seen = {}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"digest": _row(0, "aa", "a")}

    def _get(url, headers=None, params=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["params"] = params or {}
        return _Response()

    monkeypatch.setattr(aggregate.requests, "get", _get)
    aggregate.fetch_votes("https://localfit-8ab57.firebaseio.com/votes.json", "tok")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert "tok" not in seen["url"]
    assert "tok" not in json.dumps(seen["params"])
    assert seen["params"]["orderBy"] == '"$key"'


def test_a_jsonl_file_is_loaded(tmp_path):
    path = tmp_path / "votes.jsonl"
    path.write_text(
        "\n".join(json.dumps(_row(index, "aa", "a")) for index in range(3)),
        encoding="utf-8",
    )
    assert len(aggregate.load_votes_file(path)) == 3


def test_a_firebase_shaped_json_file_is_loaded(tmp_path):
    path = tmp_path / "votes.json"
    path.write_text(json.dumps({"digest": _row(0, "aa", "a")}), encoding="utf-8")
    assert len(aggregate.load_votes_file(path)) == 1


def test_a_passing_run_writes_the_artifact(tmp_path):
    votes = tmp_path / "votes.jsonl"
    votes.write_text(
        "\n".join(json.dumps(row) for row in _healthy_rows()), encoding="utf-8"
    )
    output = tmp_path / "arena-leaderboard.json"
    outputs = tmp_path / "github-output"
    exit_code = aggregate.main(
        [
            "--votes-file", str(votes),
            "--output", str(output),
            "--quality-gate",
            "--github-output", str(outputs),
            "--resamples", "10",
        ]
    )
    assert exit_code == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["models"]
    assert "gate_status=passed" in outputs.read_text(encoding="utf-8")
    assert "artifact_changed=true" in outputs.read_text(encoding="utf-8")


def test_a_blocked_run_exits_zero_and_writes_nothing(tmp_path, capsys):
    votes = tmp_path / "votes.jsonl"
    votes.write_text(json.dumps(_row(0, "aa", "a")), encoding="utf-8")
    output = tmp_path / "arena-leaderboard.json"
    outputs = tmp_path / "github-output"
    exit_code = aggregate.main(
        [
            "--votes-file", str(votes),
            "--output", str(output),
            "--quality-gate",
            "--github-output", str(outputs),
            "--resamples", "5",
        ]
    )
    assert exit_code == 0
    assert not output.exists()
    assert "gate_status=blocked" in outputs.read_text(encoding="utf-8")
    assert "effective votes" in capsys.readouterr().out


def test_an_unchanged_corpus_reports_no_change(tmp_path):
    votes = tmp_path / "votes.jsonl"
    votes.write_text(
        "\n".join(json.dumps(row) for row in _healthy_rows()), encoding="utf-8"
    )
    output = tmp_path / "arena-leaderboard.json"
    args = [
        "--votes-file", str(votes),
        "--output", str(output),
        "--quality-gate",
        "--resamples", "10",
        "--generated-at", "2026-09-27T04:00:00+00:00",
    ]
    assert aggregate.main(args) == 0
    first = output.read_text(encoding="utf-8")
    outputs = tmp_path / "github-output"
    assert aggregate.main(args + ["--github-output", str(outputs)]) == 0
    assert output.read_text(encoding="utf-8") == first
    assert "artifact_changed=false" in outputs.read_text(encoding="utf-8")


def test_the_written_artifact_verifies_against_its_signature(tmp_path):
    import subprocess
    import sys

    from omm import catalog

    votes = tmp_path / "votes.jsonl"
    votes.write_text(
        "\n".join(json.dumps(row) for row in _healthy_rows()), encoding="utf-8"
    )
    output = tmp_path / "arena-leaderboard.json"
    assert aggregate.main(
        ["--votes-file", str(votes), "--output", str(output), "--resamples", "10"]
    ) == 0

    private = tmp_path / "key"
    public = tmp_path / "key.pub"
    subprocess.run(
        [sys.executable, "scripts/sign_catalog.py", "keygen",
         "--private", str(private), "--public", str(public)],
        check=True,
    )
    manifest = tmp_path / "arena-leaderboard.manifest.json"
    subprocess.run(
        [sys.executable, "scripts/sign_catalog.py", "sign", str(output),
         "--private", str(private), "--manifest", str(manifest)],
        check=True,
    )
    catalog.verify_signed_artifact(
        output.read_bytes(),
        json.loads(manifest.read_text(encoding="utf-8")),
        public.read_text(encoding="utf-8").strip(),
    )
```

Before writing the implementation, confirm `scripts/sign_catalog.py`'s exact
subcommand and flag names with `python scripts/sign_catalog.py --help` and
`python scripts/sign_catalog.py keygen --help`, and adjust the two
`subprocess.run` calls in the last test to match. The test must drive the real
signing script, not a reimplementation.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_aggregate_arena_votes.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.aggregate_arena_votes'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/aggregate_arena_votes.py`:

```python
#!/usr/bin/env python3
"""Aggregate collected arena votes into the signed leaderboard artifact.

Sub-project C's entry point (issue #409): fetch or load vote rows, score them
with scripts/arena_score.py, run scripts/arena_quality_gate.py, and write
published/arena-leaderboard.json. Signing is a separate workflow step that
calls scripts/sign_catalog.py, exactly as train.yml does for the
recommendation model.

Spec: docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from omm.atomic import atomic_write_text  # noqa: E402
from scripts import arena_quality_gate  # noqa: E402
from scripts.arena_score import BOOTSTRAP_RESAMPLES, build_artifact  # noqa: E402
from scripts.retrain_decision import append_github_outputs  # noqa: E402

#: Fetch cap, as train_model.py has MAX_REAL_ROWS.
ARENA_MAX_ROWS = 100000

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class VotesFetchError(RuntimeError):
    """The vote export could not be fetched or parsed (not an empty corpus)."""


def is_firebase_rtdb_json_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    official = hostname.endswith(".firebaseio.com") or hostname.endswith(
        ".firebasedatabase.app"
    )
    return parsed.scheme == "https" and official and parsed.path.endswith(".json")


def extract_rows(payload: object) -> list[dict]:
    """Accept every shape an RTDB export arrives in.

    A dict keyed by PoW digest, a {"votes": {...}} wrapper, or a bare list.
    Parsing only one of them would turn a shape change into a silent empty
    corpus that the gate reads as "no data yet".
    """
    if isinstance(payload, dict):
        inner = payload.get("votes")
        if isinstance(inner, (dict, list)):
            return extract_rows(inner)
        return [row for row in payload.values() if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def fetch_votes(
    url: str, token: str | None, *, limit: int = ARENA_MAX_ROWS
) -> list[dict]:
    """Read the private `votes` node.

    This deliberately does NOT reuse train_model.fetch_real_rows. That function
    refuses to send a credential to a Firebase host because the `telemetry`
    node is world-readable, so attaching one would leak it for nothing. The
    `votes` node is `.read: false`, so an authenticated read is mandatory - the
    opposite requirement, and a missing credential is a hard error rather than
    an empty corpus.

    The credential goes in the Authorization header, never in a query
    parameter, so it cannot end up in a request log or in CI output.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and hostname in _LOOPBACK
    ):
        raise VotesFetchError("votes URL must use HTTPS (HTTP only for loopback)")
    if not token and hostname not in _LOOPBACK:
        raise VotesFetchError(
            "a credential is required to read the private votes node "
            "(set LOCALFIT_VOTES_ADMIN_TOKEN)"
        )
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params: dict[str, object] = {}
    if is_firebase_rtdb_json_url(url):
        # RTDB keys here are PoW digests, not timestamps, so this is a uniform
        # sample of the corpus. Do not describe it as "the newest rows".
        params = {"orderBy": '"$key"', "limitToLast": limit}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise VotesFetchError(
            f"couldn't fetch votes from {parsed.scheme}://{hostname}{parsed.path}: "
            f"{error}"
        ) from error
    return extract_rows(payload)[-limit:]


def load_votes_file(path: Path) -> list[dict]:
    """Read JSONL or Firebase-shaped JSON from disk.

    This is how every test and every pre-deployment run gets its corpus.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        return extract_rows(json.loads(raw))
    except ValueError:
        pass
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as error:
            raise VotesFetchError(f"{path}: not JSON or JSONL: {error}") from error
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _load_json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _serialize(artifact: dict) -> str:
    return json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--votes-url")
    source.add_argument("--votes-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--incumbent", type=Path)
    parser.add_argument("--quality-gate", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    parser.add_argument(
        "--generated-at",
        help="Override the timestamp, so a rerun on an unchanged corpus is "
        "byte-identical. The workflow leaves this unset.",
    )
    args = parser.parse_args(argv)

    import os

    if args.votes_file:
        rows = load_votes_file(args.votes_file)
    else:
        rows = fetch_votes(
            args.votes_url, os.environ.get("LOCALFIT_VOTES_ADMIN_TOKEN")
        )

    generated_at = args.generated_at or datetime.now(timezone.utc).isoformat()
    artifact = build_artifact(
        rows, generated_at=generated_at, resamples=args.resamples
    )

    incumbent = _load_json(args.incumbent) if args.incumbent else None
    result = arena_quality_gate.evaluate(artifact, incumbent)
    outputs = {"gate_status": result.status}

    if args.quality_gate and result.status == "blocked":
        print(f"Arena leaderboard not published ({len(result.reasons)} reason(s)):")
        for reason in result.reasons:
            print(f"  - {reason}")
        outputs["artifact_changed"] = "false"
        if args.github_output:
            append_github_outputs(args.github_output, outputs)
        return 0

    serialized = _serialize(artifact)
    previous = None
    if args.output.exists():
        try:
            previous = args.output.read_text(encoding="utf-8")
        except OSError:
            previous = None
    changed = serialized != previous
    if changed:
        atomic_write_text(args.output, serialized)
    outputs["artifact_changed"] = "true" if changed else "false"
    print(
        f"Arena leaderboard: {len(artifact['models'])} models, "
        f"{artifact['corpus']['effective_votes']:.1f} effective votes, "
        f"gate {result.status}, changed {outputs['artifact_changed']}"
    )
    if args.github_output:
        append_github_outputs(args.github_output, outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Move `import os` to the module's import block rather than leaving it inside
`main` — it is written inline above only to keep the diff readable.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_aggregate_arena_votes.py -q`
Expected: 16 passed.

- [ ] **Step 5: Run the whole new suite together**

Run: `python -m pytest tests/test_arena_score.py tests/test_arena_quality_gate.py tests/test_aggregate_arena_votes.py -q`
Expected: 73 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/aggregate_arena_votes.py tests/test_aggregate_arena_votes.py
git commit -m "$(cat <<'EOF'
feat(arena): add the leaderboard aggregation CLI

Reads the private votes node with a header-borne credential, or a local
JSONL/JSON corpus, scores it, gates it, and writes the artifact atomically
only when it changed. A missing credential is a hard error: silently
publishing an empty leaderboard is worse than failing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 8: The nightly workflow

**Files:**
- Create: `.github/workflows/arena-aggregate.yml`
- Test: `tests/test_arena_aggregate_workflow.py`

**Interfaces:**
- Consumes: `scripts/aggregate_arena_votes.py`'s CLI flags and step outputs from Task 7.
- Produces: nothing other tasks consume.

- [ ] **Step 1: Read the model workflow**

Read `.github/workflows/train.yml` end to end and `tests/test_train_workflow.py`.
The new workflow copies its structure: preflight, run, sign only when the gate
passed and the artifact changed, then an auto-merging PR whose head is
SSH-signed with `LOCALFIT_RETRAIN_SSH_KEY`. Copy the actual action SHAs from
`train.yml` rather than inventing them, and copy the PR-creation block's shape
(it handles branch protection, which blocks direct pushes to `main`).

- [ ] **Step 2: Write the failing test**

Create `tests/test_arena_aggregate_workflow.py`:

```python
"""The arena aggregation workflow's text contract."""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = Path(".github/workflows/arena-aggregate.yml")


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_exists(text):
    assert text.strip()


def test_it_runs_nightly_and_can_be_dispatched(text):
    assert 'cron: "0 4 * * *"' in text
    assert "workflow_dispatch" in text


def test_it_does_not_share_the_train_workflow_hour(text):
    # train.yml runs at 03:00 and opens an auto-merging PR of its own.
    assert 'cron: "0 3 * * *"' not in text


def test_a_dispatch_queues_behind_the_cron_run(text):
    assert "group: arena-aggregate" in text
    assert "cancel-in-progress: false" in text


def test_every_action_is_pinned_to_a_commit_sha(text):
    import re

    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses
    for reference in uses:
        assert re.search(r"@[0-9a-f]{40}$", reference), reference


def test_the_credential_is_passed_through_the_environment(text):
    assert "LOCALFIT_VOTES_ADMIN_TOKEN" in text
    # Never interpolated into the URL: it would land in the run log.
    assert "${{ secrets.LOCALFIT_VOTES_ADMIN_TOKEN }}?" not in text
    assert "access_token=" not in text


def test_signing_is_gated_on_the_quality_gate_and_a_real_change(text):
    assert "gate_status == 'passed'" in text
    assert "artifact_changed == 'true'" in text


def test_only_the_two_artifact_paths_are_staged(text):
    assert "git add published/arena-leaderboard.json" in text
    assert "published/arena-leaderboard.manifest.json" in text
    assert "git add -A" not in text
    assert "git add ." not in text


def test_the_pr_head_is_ssh_signed_so_trusted_head_passes(text):
    assert "LOCALFIT_RETRAIN_SSH_KEY" in text
    assert "gpg.format ssh" in text
    assert "commit.gpgsign true" in text


def test_it_never_pushes_straight_to_main(text):
    assert "gh pr create" in text
    assert "git push origin main" not in text
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_arena_aggregate_workflow.py -q`
Expected: every test fails with `FileNotFoundError` on the workflow path.

- [ ] **Step 4: Write the workflow**

Create `.github/workflows/arena-aggregate.yml`. Use `train.yml` as the
template; the aggregation-specific parts are:

```yaml
name: Aggregate arena leaderboard

"on":
  schedule:
    - cron: "0 4 * * *"
  workflow_dispatch: {}

permissions:
  contents: read

# The nightly cron and a manual dispatch both rewrite published/*.json and
# open an auto-merging PR. Serialize them so a dispatched run queues behind the
# cron run instead of racing it into a conflicted PR.
concurrency:
  group: arena-aggregate
  cancel-in-progress: false

jobs:
  aggregate:
    runs-on: ubuntu-latest
    steps:
      # Copy the checkout/setup-python steps and their pinned SHAs verbatim
      # from .github/workflows/train.yml.

      - name: Install omm
        run: pip install -e .

      - name: Preflight the protected votes export
        env:
          LOCALFIT_VOTES_EXPORT_URL: ${{ secrets.LOCALFIT_VOTES_EXPORT_URL }}
          LOCALFIT_VOTES_ADMIN_TOKEN: ${{ secrets.LOCALFIT_VOTES_ADMIN_TOKEN }}
        run: |
          test -n "$LOCALFIT_VOTES_EXPORT_URL" || { echo "LOCALFIT_VOTES_EXPORT_URL is required"; exit 1; }
          test -n "$LOCALFIT_VOTES_ADMIN_TOKEN" || { echo "LOCALFIT_VOTES_ADMIN_TOKEN is required: the votes node is private"; exit 1; }
          if [ -f published/arena-leaderboard.json ]; then
            cp published/arena-leaderboard.json "$RUNNER_TEMP/arena-incumbent.json"
          fi

      - name: Aggregate votes and gate publication
        id: aggregate
        env:
          LOCALFIT_VOTES_EXPORT_URL: ${{ secrets.LOCALFIT_VOTES_EXPORT_URL }}
          LOCALFIT_VOTES_ADMIN_TOKEN: ${{ secrets.LOCALFIT_VOTES_ADMIN_TOKEN }}
        run: |
          incumbent=""
          if [ -f "$RUNNER_TEMP/arena-incumbent.json" ]; then
            incumbent="--incumbent $RUNNER_TEMP/arena-incumbent.json"
          fi
          python scripts/aggregate_arena_votes.py \
            --votes-url "$LOCALFIT_VOTES_EXPORT_URL" \
            --output published/arena-leaderboard.json \
            --quality-gate \
            --github-output "$GITHUB_OUTPUT" \
            $incumbent

      - name: Sign the published leaderboard
        if: >-
          steps.aggregate.outputs.gate_status == 'passed' &&
          steps.aggregate.outputs.artifact_changed == 'true'
        env:
          LOCALFIT_CATALOG_SIGNING_KEY: ${{ secrets.LOCALFIT_CATALOG_SIGNING_KEY }}
        run: |
          umask 077
          key_path="$RUNNER_TEMP/omm-catalog-signing-key"
          printf '%s\n' "$LOCALFIT_CATALOG_SIGNING_KEY" > "$key_path"
          python scripts/sign_catalog.py sign published/arena-leaderboard.json \
            --private "$key_path" \
            --manifest published/arena-leaderboard.manifest.json

      - name: Open a PR with the new leaderboard and auto-merge it
        if: >-
          steps.aggregate.outputs.gate_status == 'passed' &&
          steps.aggregate.outputs.artifact_changed == 'true'
        env:
          GH_TOKEN: ${{ secrets.LOCALFIT_RETRAIN_PAT }}
          LOCALFIT_APPROVAL_PAT: ${{ secrets.LOCALFIT_APPROVAL_PAT }}
          LOCALFIT_RETRAIN_SSH_KEY: ${{ secrets.LOCALFIT_RETRAIN_SSH_KEY }}
        run: |
          # Copy train.yml's PR block verbatim except for these two lines:
          git add published/arena-leaderboard.json published/arena-leaderboard.manifest.json
          # ...and the branch name prefix: arena-leaderboard/<timestamp>
```

Fill in the copied steps from `train.yml` for real — the fragment above is the
delta, not the whole file. The PR body must follow the repository's Korean PR
description rules (`## 한줄 요약`, `## 배경`, `## 무엇을 바꿨나`,
`## 어떻게 확인했나`), because CI check `PR 설명 확인` runs on bot PRs too.
Check how `train.yml` writes its own `PR_BODY` and match it.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_arena_aggregate_workflow.py -q`
Expected: 10 passed.

- [ ] **Step 6: Validate the YAML parses**

Run: `python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/arena-aggregate.yml').read_text())"`
Expected: no output, exit 0. If `yaml` is missing, `pip install pyyaml` in the venv first — it is a dev-only check, not a project dependency.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/arena-aggregate.yml tests/test_arena_aggregate_workflow.py
git commit -m "$(cat <<'EOF'
feat(arena): run the leaderboard aggregation nightly

Cron at 04:00 UTC, an hour after the retrain job so the two never contend for
the auto-merging PR path. Signs only when the gate passed and the artifact
changed, and lands through a PR whose head is SSH-signed by the bot key.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

### Task 9: Document the pipeline where the next session will look

**Files:**
- Modify: `CLAUDE.md` (the `## Architecture` section, after the **Recommendation ML.** paragraph)
- Modify: `docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md` (add the C spec to its cross-references if it carries a roadmap list; skip if it does not)

**Interfaces:**
- Consumes: the shipped behavior of Tasks 1-8.
- Produces: nothing other tasks consume.

- [ ] **Step 1: Verify the full suite before documenting**

Run: `python -m pytest -q --ignore=tests/test_cli_update.py`

`tests/test_cli_update.py` is excluded on purpose: a few of its tests exercise
the real `shutil.rmtree(cli.SRC_DIR)` path and have wiped the real
`~/.omm/src` during an ordinary run.

Expected: no new failures. Roughly 18-22 rich/colour tests fail locally
because this shell exports `FORCE_COLOR=3`; that is pre-existing. Compare
against `git stash && python -m pytest -q --ignore=tests/test_cli_update.py`
only if the count looks wrong.

- [ ] **Step 2: Add the architecture paragraph**

Insert into `CLAUDE.md`'s `## Architecture (the parts that span files)`
section, immediately after the **Recommendation ML.** paragraph:

```markdown
**Arena leaderboard scoring.** `scripts/arena_score.py` turns uploaded votes into
`published/arena-leaderboard.json`: a weighted Bradley-Terry quality fit (with a
fixed-strength phantom opponent, so an undefeated model neither diverges nor
outranks a well-supported one) grouped into confidence-interval tiers, plus a
separate efficiency rating fitted from **within-battle** `tok/s ÷ GiB` log-ratios
so a model is never credited for running on a bigger GPU. Efficiency only breaks
ties inside a quality tier; the two numbers are never blended. One
`(client_id, model pair)` contributes at most `CLIENT_PAIR_VOTE_CAP` effective
votes. `scripts/arena_quality_gate.py` blocks publication on a thin corpus or a
single dominating machine; `scripts/aggregate_arena_votes.py` is the CLI and
`.github/workflows/arena-aggregate.yml` runs it nightly, signing with the same
Ed25519 catalog key as the recommendation model. The math lives in `scripts/`,
never in `src/omm/` — the runtime only reads the artifact. Note the deliberate
asymmetry with telemetry: the `votes` RTDB node is `.read: false`, so the
aggregator sends an `Authorization: Bearer` credential, while
`train_model.py:fetch_real_rows` refuses to send one to the world-readable
`telemetry` node.
```

- [ ] **Step 3: Confirm no command-surface documentation drifted**

Run: `python scripts/check_docs_sync.py`
Expected: exit 0. C adds no command, so `docs/commands.json` and `README.md`
must be unchanged. A warning about a stale `omm.run` copy is a separate
repository and not this branch's job.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: describe the arena leaderboard scoring pipeline

Records why the math lives in scripts/ rather than src/omm/, why the
efficiency axis is a within-battle ratio, and why the votes fetch sends a
credential while the telemetry fetch deliberately refuses to.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FTaCy6Vc1WtKjzrsYcVchF
EOF
)"
```

---

## Not in this plan

Deliberate omissions, each traced to a fixed decision in the spec:

- **No `published/arena-leaderboard.json` is committed.** The first real run
  produces it. Committing a hand-made or synthetic artifact would put an
  unsigned file into `published/`, which the repository rules forbid.
- **No power (watt) measurement.** Separate issue; the fields stay nullable and
  already pass the validator and the rules.
- **No Worker or RTDB rules deployment.** Separate operational step; that is
  why `--votes-file` exists.
- **No user-visible surface.** Sub-project D (#410) owns all of it.
- **No change under `src/omm/`.**
