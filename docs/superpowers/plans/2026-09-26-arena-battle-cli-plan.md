# `omm arena` local battle CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `omm arena`, an interactive command that compares two installed
local models blind on the same prompt, captures the user's vote, and appends
one row per vote to a local-only `~/.omm/arena/votes.jsonl`.

**Architecture:** A new pure-logic module `src/omm/arena.py` owns pairing,
generation timing, and the vote log; `cli.py` gains one Typer command that
drives the round loop and all terminal I/O. Generation reuses `quality.py`'s
existing Ollama/LM Studio transports — extended with a "no sampling overrides"
mode — so no new HTTP or subprocess code is introduced. No new runtime
dependency; nothing is uploaded.

**Tech Stack:** Python 3.10+, Typer, `questionary`/`prompt_toolkit` (lazy
imported inside functions only), `rich` console, `filelock` via
`omm.atomic.locked`, pytest + `typer.testing.CliRunner`.

**Spec:** `docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md`
(sub-project A only). GitHub issue: `omm-hippo/omm#407`.

## Global Constraints

- Command name is **`omm arena`** (decided 2026-09-26, replacing the spec's
  "TBD"). `omm compare` at `cli.py:4333` stays untouched and must not be
  reused or renamed by this work.
- The pair-persistence flag is **`--keep`** (decided 2026-09-26, replacing the
  spec's placeholder `--pin`). Do not add `--pin`; `pin` is reserved for the
  model-version pinning concept discussed in issue #295.
- **One engine per session** (decided 2026-09-26). Both models of every round
  run on the same engine. `engine_a` and `engine_b` still exist in the vote
  schema and are written with the same value, so sub-project B's uploader
  needs no schema change if cross-engine pairing is added later.
- **`watt_a`/`watt_b` are always `null` in this sub-project** (decided
  2026-09-26). The fields exist in every row; no `nvidia-smi`/`rocm-smi`
  reader is written here. `memory_gb_*` **is** measured for Ollama.
- No network calls beyond what the chosen engine already needs. `omm arena`
  must never call `telemetry.send_event`, `usage`, or `error_report` upload
  paths, and never writes outside `OMM_HOME`.
- No new runtime dependency. `questionary`, `prompt_toolkit`, `requests`, and
  `importlib.metadata` stay lazy-imported inside functions — never hoisted to
  module scope — because `omm help` startup time (~140ms) is a tracked
  property of `cli.py`.
- All on-disk state resolves `config.OMM_HOME` **at call time** (the
  `runlog.py:_logs_dir` pattern), never as an import-time module constant, so
  `OMM_HOME` overrides and the test fixtures keep working.
- Blind by default with no opt-out: before the vote is cast, no model name, no
  engine name, no elapsed time, and no token count may reach the terminal.
- Python 3.10+ syntax only (`X | None`, not `Optional[X]`), matching the rest
  of `src/omm/`.
- Every commit runs through `scripts/pre-commit`, which auto-bumps the patch
  version in `pyproject.toml` and `packaging/npm/launcher/package.json`. That
  is expected. Never pass `--no-verify`.
- `git add` only the exact files you changed. Never `git add -A` or `git add .`
  — other Claude sessions share this checkout.
- Branch off `beta`, never `main`. Do not push; pushing is a separate explicit
  ask from the user.

## Review Focus

These are the failure modes the spec implies but does not spell out. Each one
has its test placed in the task that owns the code.

1. **Exactly one positional model given.** `omm arena qwen3:4b` cannot form a
   pair. It must error before any engine is started, not silently draw a
   random partner. (Test in Task 5.)
2. **The same model given twice.** `omm arena qwen3:4b qwen3:4b` is a model
   fighting itself; the vote is meaningless. Must error before generation.
   (Test in Task 2.)
3. **A positional model that exists in the engine but is not omm-managed.**
   The pool is registry-linked models only, so this arg looks "installed" to
   the user but is not eligible. The error must say why and name the fix
   (`omm import`), not just "not installed". (Test in Task 5.)
4. **The user escapes out of a prompt.** The two prompt helpers cancel
   *differently*: `_ask_text` returns `None`, while `_ask_single_key` (and
   `_ask_confirm`, which wraps it) raises `KeyboardInterrupt` from its
   Escape/Ctrl-Q/Ctrl-C binding at `cli.py:3639`, and returns its
   `default_value` on a bare Enter. The round loop must handle all three:
   cancel ends the session cleanly (exit 0, no partial `votes.jsonl` row, no
   traceback), and a bare Enter at the *vote* prompt must re-ask rather than
   silently end a session mid-battle. (Tests in Task 5.)
5. **`votes.jsonl` cannot be written** (read-only `OMM_HOME`, full disk, lock
   contention). The vote is already cast and the reveal already printed; the
   session must warn and continue rather than crash and lose the round.
   (Test in Task 1.)

---

## File Structure

- **Create** `src/omm/arena.py` — pairing, default-sampling generation
  wrapper, measured-memory read, vote-row construction, `votes.jsonl` append.
  Pure logic, no Typer, no `rich`, no prompts. Target ~220 lines.
- **Create** `tests/test_arena.py` — unit tests for `arena.py`.
- **Create** `tests/test_cli_arena.py` — CliRunner tests for the command.
- **Modify** `src/omm/quality.py` — make `generation` optional in `_generate`
  / `_generate_lmstudio` / `_generate_with_runtime` (engine-default sampling),
  and add `loaded_model_memory_gb`.
- **Modify** `src/omm/cli.py` — add the `arena` command near the other
  engine-driven commands (`benchmark` at `cli.py:10432`, `evaluate` at
  `cli.py:10708`); put `arena_cmd` immediately after `evaluate_cmd`.
- **Modify** `docs/commands.json` — regenerated, never hand-edited.
- **Modify** `README.md` — one `## Usage` line.
- **Modify** `docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md` —
  resolve the two TBDs and record the three decisions above.

---

### Task 1: Vote log — path, row schema, append

**Files:**
- Create: `src/omm/arena.py`
- Test: `tests/test_arena.py`

**Interfaces:**
- Consumes: `omm.config` (for `OMM_HOME`), `omm.atomic.locked`.
- Produces:
  - `votes_path() -> pathlib.Path`
  - `build_vote_row(*, prompt, model_a, model_b, engine, result_a, result_b, winner, kept) -> dict`
    where `result_a`/`result_b` are `GenerationResult` (defined in Task 3;
    for this task use the duck-typed attributes `elapsed`, `tokens`,
    `memory_gb`)
  - `append_vote(row: dict) -> bool` — `True` on success, `False` on a
    write/lock failure (never raises `OSError`)
  - `WINNERS: frozenset[str]` = `{"a", "b", "both_bad"}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arena.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from omm import arena, config


@dataclass
class _FakeResult:
    elapsed: float
    tokens: int
    memory_gb: float | None


def _row(**overrides):
    base = dict(
        prompt="Write a haiku about disks.",
        model_a="qwen3-4b-Q4_K_M.gguf",
        model_b="llama3-8b-Q4_K_M.gguf",
        engine="ollama",
        result_a=_FakeResult(elapsed=1.5, tokens=40, memory_gb=3.1),
        result_b=_FakeResult(elapsed=2.5, tokens=60, memory_gb=None),
        winner="a",
        kept=False,
    )
    base.update(overrides)
    return arena.build_vote_row(**base)


def test_votes_path_follows_omm_home(isolated_omm_home):
    assert arena.votes_path() == config.OMM_HOME / "arena" / "votes.jsonl"


def test_build_vote_row_has_exactly_the_spec_fields():
    row = _row()
    assert set(row) == {
        "battle_id", "timestamp", "prompt",
        "model_a", "model_b", "engine_a", "engine_b",
        "elapsed_a", "elapsed_b", "tokens_a", "tokens_b",
        "memory_gb_a", "memory_gb_b", "watt_a", "watt_b",
        "winner", "pinned",
    }
    assert row["engine_a"] == "ollama"
    assert row["engine_b"] == "ollama"
    assert row["watt_a"] is None and row["watt_b"] is None
    assert row["memory_gb_a"] == 3.1
    assert row["memory_gb_b"] is None
    assert row["timestamp"].endswith("+00:00")
    assert len(row["battle_id"]) == 36


def test_build_vote_row_rejects_an_unknown_winner():
    with pytest.raises(ValueError, match="winner"):
        _row(winner="tie")


def test_append_vote_writes_one_json_object_per_line(isolated_omm_home):
    assert arena.append_vote(_row(winner="a")) is True
    assert arena.append_vote(_row(winner="both_bad")) is True
    lines = arena.votes_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["winner"] for line in lines] == ["a", "both_bad"]


def test_append_vote_returns_false_instead_of_raising_when_unwritable(
    isolated_omm_home, monkeypatch
):
    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(arena.Path, "mkdir", _boom)
    assert arena.append_vote(_row()) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'omm.arena'`

- [ ] **Step 3: Write the implementation**

Create `src/omm/arena.py`:

```python
"""Local blind model battles (`omm arena`).

Sub-project A of the arena roadmap: pairing, blind generation, vote capture,
and a local-only vote log. Nothing here uploads anything; `votes.jsonl` is
read by sub-project B when that lands, never by this module.

Design: docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import Timeout as FileLockTimeout

from . import config
from .atomic import locked

logger = logging.getLogger("omm.arena")

WINNERS = frozenset({"a", "b", "both_bad"})


def arena_dir() -> Path:
    """Resolved at call time, never at import: OMM_HOME is overridable and
    the test fixtures monkeypatch `config.OMM_HOME` (see runlog._logs_dir)."""
    return config.OMM_HOME / "arena"


def votes_path() -> Path:
    return arena_dir() / "votes.jsonl"


def build_vote_row(
    *,
    prompt: str,
    model_a: str,
    model_b: str,
    engine: str,
    result_a,
    result_b,
    winner: str,
    kept: bool,
) -> dict:
    """One `votes.jsonl` row, in exactly the shape sub-project B will upload.

    `engine` is a single value because a session never mixes engines; the
    row still carries engine_a/engine_b separately so a later cross-engine
    mode needs no schema migration. `watt_*` is always None in sub-project A.
    """
    if winner not in WINNERS:
        raise ValueError(f"winner must be one of {sorted(WINNERS)}, got {winner!r}")
    return {
        "battle_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "model_a": model_a,
        "model_b": model_b,
        "engine_a": engine,
        "engine_b": engine,
        "elapsed_a": round(float(result_a.elapsed), 3),
        "elapsed_b": round(float(result_b.elapsed), 3),
        "tokens_a": int(result_a.tokens),
        "tokens_b": int(result_b.tokens),
        "memory_gb_a": result_a.memory_gb,
        "memory_gb_b": result_b.memory_gb,
        "watt_a": None,
        "watt_b": None,
        "winner": winner,
        "pinned": bool(kept),
    }


def append_vote(row: dict) -> bool:
    """Append one JSON line. Returns False (never raises) on any filesystem
    or lock failure: the vote is already cast and revealed by the time this
    runs, so a write failure must not take the session down with it."""
    path = votes_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, FileLockTimeout, ValueError, TypeError) as error:
        logger.debug("arena vote append failed: %s", error)
        return False
    logger.info("arena vote recorded", extra={"winner": row.get("winner")})
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/omm/arena.py tests/test_arena.py
git commit -m "feat(arena): add local vote log schema and append"
```

---

### Task 2: Pairing

**Files:**
- Modify: `src/omm/arena.py`
- Test: `tests/test_arena.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the module itself.
- Produces:
  - `class Pairing` with
    `__init__(self, pool: list[str], seed_pair: tuple[str, str] | None = None, keep: bool = False, rng: random.Random | None = None)`
    and `next_pair(self) -> tuple[str, str]`
  - `validate_seed_pair(models: list[str], pool: list[str]) -> tuple[str, str]`
    — raises `ArenaError` on a wrong count, a duplicate, or a model outside
    the pool
  - `class ArenaError(RuntimeError)`

`Pairing` semantics, straight from the spec: a seed pair supplied on the
command line is used for round 1 only; with `keep=True` the round-1 pair
(seeded or randomly drawn) is reused for every later round; otherwise every
round after the first draws a fresh random pair.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arena.py`:

```python
import random


POOL = ["m1", "m2", "m3", "m4"]


def test_seed_pair_is_used_for_round_one_only():
    pairing = arena.Pairing(POOL, seed_pair=("m1", "m2"), rng=random.Random(0))
    assert pairing.next_pair() == ("m1", "m2")
    later = [pairing.next_pair() for _ in range(20)]
    assert any(pair != ("m1", "m2") for pair in later)


def test_keep_holds_the_seed_pair_for_every_round():
    pairing = arena.Pairing(POOL, seed_pair=("m1", "m2"), keep=True, rng=random.Random(0))
    assert [pairing.next_pair() for _ in range(5)] == [("m1", "m2")] * 5


def test_keep_holds_a_randomly_drawn_pair_too():
    pairing = arena.Pairing(POOL, keep=True, rng=random.Random(7))
    first = pairing.next_pair()
    assert [pairing.next_pair() for _ in range(4)] == [first] * 4


def test_random_draw_never_pairs_a_model_with_itself():
    pairing = arena.Pairing(POOL, rng=random.Random(3))
    for _ in range(200):
        left, right = pairing.next_pair()
        assert left != right
        assert left in POOL and right in POOL


def test_random_draw_does_not_favour_either_slot():
    """A draw that always put the lower-indexed model in slot A would leak
    identity across rounds to an attentive user and bias sub-project C's
    Bradley-Terry fit, which reads `winner` against a/b positions."""
    pairing = arena.Pairing(["m1", "m2"], rng=random.Random(11))
    seen = {pairing.next_pair() for _ in range(200)}
    assert seen == {("m1", "m2"), ("m2", "m1")}


def test_validate_seed_pair_rejects_one_model():
    with pytest.raises(arena.ArenaError, match="two models"):
        arena.validate_seed_pair(["m1"], POOL)


def test_validate_seed_pair_rejects_the_same_model_twice():
    with pytest.raises(arena.ArenaError, match="two different"):
        arena.validate_seed_pair(["m1", "m1"], POOL)


def test_validate_seed_pair_rejects_a_model_outside_the_pool():
    with pytest.raises(arena.ArenaError, match="nope"):
        arena.validate_seed_pair(["m1", "nope"], POOL)


def test_validate_seed_pair_returns_the_pair():
    assert arena.validate_seed_pair(["m2", "m3"], POOL) == ("m2", "m3")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena.py -q`
Expected: FAIL — `AttributeError: module 'omm.arena' has no attribute 'Pairing'`

- [ ] **Step 3: Write the implementation**

Add `import random` to the imports in `src/omm/arena.py`, then append:

```python
class ArenaError(RuntimeError):
    """A user-facing arena setup problem; cli.py prints str(error)."""


def validate_seed_pair(models: list[str], pool: list[str]) -> tuple[str, str]:
    if len(models) != 2:
        raise ArenaError(
            "`omm arena` takes two models or none at all - pass two models to "
            "seed the first round, or no models to draw a random pair."
        )
    left, right = models
    if left == right:
        raise ArenaError("Pass two different models; a model cannot battle itself.")
    missing = [m for m in (left, right) if m not in pool]
    if missing:
        raise ArenaError("Not available for this arena session: " + ", ".join(missing))
    return left, right


class Pairing:
    """Which two models face off in each round.

    A seed pair applies to round 1 only unless `keep` is set; `keep` freezes
    whatever round 1 ended up using (seeded or drawn) for the whole session.
    """

    def __init__(
        self,
        pool: list[str],
        seed_pair: tuple[str, str] | None = None,
        keep: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        if len(set(pool)) < 2:
            raise ArenaError("An arena round needs at least 2 distinct models.")
        self._pool = list(pool)
        self._seed_pair = seed_pair
        self._keep = keep
        self._rng = rng or random.Random()
        self._held: tuple[str, str] | None = None

    def next_pair(self) -> tuple[str, str]:
        if self._held is not None:
            return self._held
        if self._seed_pair is not None:
            pair = self._seed_pair
            self._seed_pair = None
        else:
            # sample() draws without replacement and returns them in a random
            # order, so neither slot is biased toward any model.
            pair = tuple(self._rng.sample(self._pool, 2))
        if self._keep:
            self._held = pair
        return pair
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena.py -q`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
git add src/omm/arena.py tests/test_arena.py
git commit -m "feat(arena): add round pairing with --keep persistence"
```

---

### Task 3: Engine-default generation and measured memory

**Files:**
- Modify: `src/omm/quality.py:761-803` (`_generate`, `_generate_with_runtime`),
  `src/omm/quality.py:846-906` (`_generate_lmstudio`), and add
  `loaded_model_memory_gb` next to `_model_is_loaded` at
  `src/omm/quality.py:694`
- Test: `tests/test_quality.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces:
  - `quality._generate(tag, prompt, generation: dict | None, ...)` — when
    `generation is None`, the Ollama payload carries no `options` and no
    `think` key, so the model's own sampling defaults apply
  - `quality._generate_lmstudio(model_key, prompt, generation: dict | None, num_predict, port)`
    — when `generation is None`, the payload carries no `temperature` and no
    `max_tokens`
  - `quality.loaded_model_memory_gb(tag: str) -> float | None` — GB actually
    resident for `tag` per Ollama `/api/ps`, or `None` when unknown

The spec requires "each model's own default sampling (no forced
`temperature=0`/`seed=0`)". Rather than duplicate two HTTP payload builders
and LM Studio's response normalization inside `arena.py`, both existing
transports learn one optional mode. Every existing caller passes a real
`generation` dict and is unaffected.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_quality.py`:

```python
def test_generate_without_a_generation_dict_sends_no_sampling_overrides(monkeypatch):
    captured = {}

    def _fake_request(method, path, payload=None, timeout=None):
        captured["payload"] = payload
        return {"response": "hi", "eval_count": 5, "eval_duration": 1_000_000_000}

    monkeypatch.setattr(quality, "_request_json", _fake_request)
    quality._generate("m:latest", "hello", None)
    assert "options" not in captured["payload"]
    assert "think" not in captured["payload"]
    assert captured["payload"]["stream"] is False


def test_generate_lmstudio_without_a_generation_dict_sends_no_sampling_overrides(monkeypatch):
    captured = {}

    def _fake_request(port, method, path, payload=None, timeout=None):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(quality, "_lmstudio_request_json", _fake_request)
    quality._generate_lmstudio("key", "hello", None, None, 1234)
    assert "temperature" not in captured["payload"]
    assert "max_tokens" not in captured["payload"]


def test_loaded_model_memory_gb_prefers_vram_then_total(monkeypatch):
    monkeypatch.setattr(
        quality,
        "_request_json",
        lambda *a, **k: {"models": [{"name": "m:latest", "size": 8_000_000_000,
                                     "size_vram": 4_000_000_000}]},
    )
    assert quality.loaded_model_memory_gb("m:latest") == pytest.approx(4.0, rel=1e-3)

    monkeypatch.setattr(
        quality,
        "_request_json",
        lambda *a, **k: {"models": [{"name": "m:latest", "size": 8_000_000_000,
                                     "size_vram": 0}]},
    )
    assert quality.loaded_model_memory_gb("m:latest") == pytest.approx(8.0, rel=1e-3)


def test_loaded_model_memory_gb_is_none_when_unknown(monkeypatch):
    monkeypatch.setattr(quality, "_request_json", lambda *a, **k: {"models": []})
    assert quality.loaded_model_memory_gb("m:latest") is None

    def _raise(*a, **k):
        raise quality.QualityEvaluationError("down", failure_reason="unknown")

    monkeypatch.setattr(quality, "_request_json", _raise)
    assert quality.loaded_model_memory_gb("m:latest") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_quality.py -q -k "no_sampling_overrides or loaded_model_memory_gb"`
Expected: FAIL — `TypeError: 'NoneType' object is not subscriptable` for the
two generate tests, `AttributeError: ... has no attribute
'loaded_model_memory_gb'` for the other two.

- [ ] **Step 3: Write the implementation**

Replace the body of `_generate` at `src/omm/quality.py:761`:

```python
def _generate(tag: str, prompt: str, generation: dict | None, num_predict: int | None = None,
              runtime_options: dict | None = None, supports_thinking: bool = True) -> dict:
    payload: dict = {
        "model": tag,
        "prompt": prompt,
        "stream": False,
    }
    if generation is not None:
        options = {
            "temperature": generation["temperature"],
            "seed": generation["seed"],
            "num_ctx": generation["num_ctx"],
            "num_predict": num_predict or generation["num_predict"],
        }
        options.update(runtime_options or {})
        payload["options"] = options
        if supports_thinking:
            # Ollama rejects the top-level `think` field outright - HTTP 400
            # "does not support thinking" - for any model whose capabilities
            # don't list "thinking", even when the value is False. Omitting the
            # field entirely is the documented-safe choice for those models.
            payload["think"] = generation["think"]
    elif runtime_options:
        payload["options"] = dict(runtime_options)
    # generation is None (`omm arena`): the model's own sampling defaults
    # apply, which is the point - arena compares what a user actually gets.
    data = _request_json("POST", "/api/generate", payload)
    if not isinstance(data.get("response"), str):
        raise QualityEvaluationError(
            f"Ollama returned no text response for '{tag}'", failure_reason=FAILURE_REASON_UNKNOWN
        )
    return data
```

Change the `_generate_with_runtime` signature at `src/omm/quality.py:790` so
`generation: dict | None` (the body needs no other change).

In `_generate_lmstudio` at `src/omm/quality.py:846`, change the signature to
`generation: dict | None` and replace the payload construction:

```python
    payload: dict = {
        "model": model_key,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if generation is not None:
        payload["max_tokens"] = num_predict or generation["num_predict"]
        payload["temperature"] = generation["temperature"]
    elif num_predict is not None:
        payload["max_tokens"] = num_predict
```

Add next to `_model_is_loaded` at `src/omm/quality.py:694`:

```python
def loaded_model_memory_gb(tag: str) -> float | None:
    """GB `tag` actually occupies right now, per Ollama's own /api/ps.

    Prefers `size_vram` (what the GPU holds) and falls back to `size` when
    the model is running on CPU, where size_vram is 0. Returns None whenever
    the daemon can't be reached or the model isn't resident - sub-project C's
    efficiency axis treats a null as "no measurement", never as zero.
    """
    try:
        models = _request_json("GET", "/api/ps", timeout=10).get("models")
    except QualityEvaluationError:
        return None
    if not isinstance(models, list):
        return None
    for item in models:
        if not isinstance(item, dict) or not _tag_matches(item.get("name"), tag):
            continue
        for key in ("size_vram", "size"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value / 1_000_000_000
        return None
    return None
```

- [ ] **Step 4: Run the tests to verify they pass, and that nothing regressed**

Run: `python -m pytest tests/test_quality.py tests/test_cli_benchmark.py tests/test_cli_evaluate.py -q`
Expected: PASS. If rich-console colour assertions fail, check `echo $FORCE_COLOR`
— this machine exports `FORCE_COLOR=3`, which causes ~18-22 pre-existing
failures unrelated to this change; re-run those with `FORCE_COLOR= `.

- [ ] **Step 5: Commit**

```bash
git add src/omm/quality.py tests/test_quality.py
git commit -m "feat(quality): allow engine-default sampling and read loaded model memory"
```

---

### Task 4: `arena.run_round_generation` — timed blind generation for one side

**Files:**
- Modify: `src/omm/arena.py`
- Test: `tests/test_arena.py`

**Interfaces:**
- Consumes: `quality._generate`, `quality._generate_lmstudio`,
  `quality._tokens_per_second`, `quality.loaded_model_memory_gb`,
  `quality.ensure_model_unloaded`, `linker.unload_lmstudio_model` (all from
  Task 3 / existing code).
- Produces:
  - `@dataclass(frozen=True) GenerationResult` with fields
    `text: str`, `elapsed: float`, `tokens: int`,
    `tokens_per_second: float | None`, `memory_gb: float | None`
  - `generate_side(model_ref: str, prompt: str, *, engine: str, lmstudio_port: int | None) -> GenerationResult`
    — raises `quality.QualityEvaluationError` on any engine failure, always
    unloads the model afterwards

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arena.py`:

```python
from omm import quality


def test_generate_side_times_the_call_and_unloads_afterwards(monkeypatch):
    calls = []
    clock = iter([100.0, 103.5])

    monkeypatch.setattr(arena.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        arena.quality,
        "_generate",
        lambda tag, prompt, generation, **kwargs: {
            "response": "  an answer  ",
            "eval_count": 70,
            "eval_duration": 3_500_000_000,
        },
    )
    monkeypatch.setattr(arena.quality, "loaded_model_memory_gb", lambda tag: 2.5)
    monkeypatch.setattr(
        arena.quality, "ensure_model_unloaded", lambda tag: calls.append(tag) or True
    )

    result = arena.generate_side("m:latest", "hi", engine="ollama", lmstudio_port=None)
    assert result.text == "an answer"
    assert result.elapsed == pytest.approx(3.5)
    assert result.tokens == 70
    assert result.tokens_per_second == pytest.approx(20.0)
    assert result.memory_gb == 2.5
    assert calls == ["m:latest"]


def test_generate_side_unloads_even_when_generation_fails(monkeypatch):
    calls = []

    def _boom(*args, **kwargs):
        raise quality.QualityEvaluationError("oom", failure_reason="out_of_memory")

    monkeypatch.setattr(arena.quality, "_generate", _boom)
    monkeypatch.setattr(
        arena.quality, "ensure_model_unloaded", lambda tag: calls.append(tag) or True
    )
    with pytest.raises(quality.QualityEvaluationError):
        arena.generate_side("m:latest", "hi", engine="ollama", lmstudio_port=None)
    assert calls == ["m:latest"]


def test_generate_side_on_lmstudio_reports_null_memory_and_unloads_there(monkeypatch):
    unloaded = []
    monkeypatch.setattr(
        arena.quality,
        "_generate_lmstudio",
        lambda key, prompt, generation, num_predict, port: {
            "response": "ok", "eval_count": 10, "eval_duration": 1_000_000_000
        },
    )
    monkeypatch.setattr(
        arena.quality,
        "loaded_model_memory_gb",
        lambda tag: pytest.fail("LM Studio must not be asked for Ollama's /api/ps"),
    )
    monkeypatch.setattr(
        arena.linker, "unload_lmstudio_model", lambda key: unloaded.append(key) or True
    )
    result = arena.generate_side("key", "hi", engine="lmstudio", lmstudio_port=1234)
    assert result.memory_gb is None
    assert result.tokens == 10
    assert unloaded == ["key"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_arena.py -q -k generate_side`
Expected: FAIL — `AttributeError: module 'omm.arena' has no attribute 'generate_side'`

- [ ] **Step 3: Write the implementation**

Add `import time` and `from dataclasses import dataclass` plus
`from . import linker`, `from . import quality` to `src/omm/arena.py`'s
imports, then append:

```python
@dataclass(frozen=True)
class GenerationResult:
    text: str
    elapsed: float
    tokens: int
    tokens_per_second: float | None
    memory_gb: float | None


def generate_side(
    model_ref: str,
    prompt: str,
    *,
    engine: str,
    lmstudio_port: int | None,
) -> GenerationResult:
    """One side of a round: load-by-generating, time it, read memory, unload.

    Sampling is the model's own default (`generation=None`) - arena compares
    what a user would actually get, not a reproducible fixture. Extended
    thinking is never displayed or stored: Ollama returns the trace in a
    separate `thinking` field, and only `response` is read here.

    Always unloads afterwards, including on failure, so the next side gets a
    clean machine. Raises QualityEvaluationError with the engine's existing
    FailureReason on any load/generate failure; the caller aborts the round.
    """
    started = time.monotonic()
    try:
        if engine == "lmstudio":
            data = quality._generate_lmstudio(model_ref, prompt, None, None, lmstudio_port)
        else:
            data = quality._generate(model_ref, prompt, None)
        elapsed = time.monotonic() - started
        memory_gb = None if engine == "lmstudio" else quality.loaded_model_memory_gb(model_ref)
    finally:
        if engine == "lmstudio":
            linker.unload_lmstudio_model(model_ref)
        else:
            quality.ensure_model_unloaded(model_ref)
    tokens = data.get("eval_count")
    return GenerationResult(
        text=str(data.get("response", "")).strip(),
        elapsed=elapsed,
        tokens=int(tokens) if isinstance(tokens, int) and not isinstance(tokens, bool) else 0,
        tokens_per_second=quality._tokens_per_second(data),
        memory_gb=memory_gb,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_arena.py -q`
Expected: PASS (17 passed)

- [ ] **Step 5: Commit**

```bash
git add src/omm/arena.py tests/test_arena.py
git commit -m "feat(arena): add timed blind generation per side"
```

---

### Task 5: The `omm arena` command

**Files:**
- Modify: `src/omm/cli.py` — insert `arena_cmd` immediately after
  `evaluate_cmd` ends (`evaluate_cmd` starts at `cli.py:10708`)
- Test: `tests/test_cli_arena.py`

**Interfaces:**
- Consumes: `arena.Pairing`, `arena.validate_seed_pair`, `arena.ArenaError`,
  `arena.generate_side`, `arena.build_vote_row`, `arena.append_vote`,
  `arena.GenerationResult` (Tasks 1-4); existing `cli.py` helpers
  `_select_benchmark_engine`, `_select_benchmark_engine_for_models`,
  `_ensure_engine_running`, `_stop_engine_daemon`, `_print_no_engine_error`,
  `_print_engine_selection_notice`, `_lmstudio_installed_models`,
  `_ask_text`, `_ask_single_key`, `_ask_confirm`, `_global_opts`.
- Produces: the `arena` Typer command plus four module-level helpers,
  `_arena_eligible_models(engine) -> dict[str, str]` (runtime ref -> registry
  filename), `_arena_reveal(pair, engine, result_a, result_b, winner) -> None`,
  `_arena_vote() -> str | None`, and
  `_arena_session(pairing, pool, engine, lmstudio_port, keep) -> None`.

The eligible pool is **registry-linked models only**, per the spec ("installed
and linked into a runnable engine via the existing hub+link registry"), which
means models the user pulled straight into Ollama are excluded. That is a
sharp edge, so the "fewer than 2" error distinguishes "you have no models"
from "you have models but omm doesn't manage them" and names `omm import`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_arena.py`:

```python
from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import arena, cli, config, quality


runner = CliRunner()


def _result(text, elapsed=1.0, tokens=10, tps=10.0, memory=1.0):
    return arena.GenerationResult(
        text=text, elapsed=elapsed, tokens=tokens, tokens_per_second=tps, memory_gb=memory
    )


def _patch_engine(monkeypatch, pool=("alpha:latest", "beta:latest", "gamma:latest")):
    monkeypatch.setattr(cli, "_select_benchmark_engine", lambda: "ollama")
    monkeypatch.setattr(cli, "_select_benchmark_engine_for_models", lambda models: "ollama")
    monkeypatch.setattr(cli, "_ensure_engine_running", lambda *a, **k: ("ollama", None))
    monkeypatch.setattr(cli, "_stop_engine_daemon", lambda engine, handle: None)
    monkeypatch.setattr(
        cli,
        "_arena_eligible_models",
        lambda engine: {tag: f"{tag.split(':')[0]}.gguf" for tag in pool},
    )


def _patch_prompts(monkeypatch, *, texts, votes, continues):
    text_iter, vote_iter, continue_iter = iter(texts), iter(votes), iter(continues)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: next(text_iter))
    monkeypatch.setattr(
        cli, "_ask_single_key", lambda *a, **k: next(vote_iter)
    )
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: next(continue_iter))


def test_one_round_writes_a_vote_row(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=["why is the sky blue?"], votes=["a"], continues=[False])
    monkeypatch.setattr(
        cli.arena,
        "generate_side",
        lambda ref, prompt, **kwargs: _result(f"answer from {ref}"),
    )
    result = runner.invoke(cli.app, ["arena", "alpha:latest", "beta:latest"])
    assert result.exit_code == 0, result.output
    rows = [
        json.loads(line)
        for line in arena.votes_path().read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["model_a"] == "alpha.gguf"
    assert rows[0]["model_b"] == "beta.gguf"
    assert rows[0]["winner"] == "a"
    assert rows[0]["pinned"] is False


def test_blind_phase_leaks_no_identity_before_the_vote(monkeypatch, isolated_omm_home):
    """The single most important assertion in this feature."""
    _patch_engine(monkeypatch)
    seen_before_vote = {}

    def _vote(*args, **kwargs):
        seen_before_vote["output"] = _live_output()
        return "a"

    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "hello")
    monkeypatch.setattr(cli, "_ask_single_key", _vote)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.arena,
        "generate_side",
        lambda ref, prompt, **kwargs: _result("a neutral answer", elapsed=7.25, tokens=99, tps=13.6),
    )

    captured = []
    real_print = cli.console.print
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: captured.append(" ".join(str(x) for x in a)) or real_print(*a, **k))

    def _live_output():
        return "\n".join(captured)

    result = runner.invoke(cli.app, ["arena", "alpha:latest", "beta:latest"])
    assert result.exit_code == 0, result.output
    before = seen_before_vote["output"]
    for leak in ("alpha", "beta", "Ollama", "ollama", "7.2", "99", "13.6", "tok/s"):
        assert leak not in before, f"blind phase leaked {leak!r}:\n{before}"
    assert "Response 1" in before and "Response 2" in before
    # ...and the reveal, after the vote, does name them.
    assert "alpha:latest" in result.output and "beta:latest" in result.output


def test_keep_reuses_the_same_pair_every_round(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    _patch_prompts(
        monkeypatch, texts=["p1", "p2"], votes=["a", "b"], continues=[True, False]
    )
    monkeypatch.setattr(
        cli.arena, "generate_side", lambda ref, prompt, **kwargs: _result("x")
    )
    result = runner.invoke(cli.app, ["arena", "alpha:latest", "beta:latest", "--keep"])
    assert result.exit_code == 0, result.output
    rows = [
        json.loads(line)
        for line in arena.votes_path().read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert {(r["model_a"], r["model_b"]) for r in rows} == {("alpha.gguf", "beta.gguf")}
    assert all(r["pinned"] is True for r in rows)


def test_one_positional_model_errors_before_any_generation(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    monkeypatch.setattr(
        cli.arena,
        "generate_side",
        lambda *a, **k: pytest_fail_no_generation(),
    )
    result = runner.invoke(cli.app, ["arena", "alpha:latest"])
    assert result.exit_code == 1
    assert "two models" in result.output


def pytest_fail_no_generation():
    raise AssertionError("generation must not start on a bad argument")


def test_unmanaged_model_argument_names_the_import_fix(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    result = runner.invoke(cli.app, ["arena", "alpha:latest", "stranger:latest"])
    assert result.exit_code == 1
    assert "stranger:latest" in result.output
    assert "omm import" in result.output


def test_fewer_than_two_eligible_models_errors(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch, pool=("alpha:latest",))
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 1
    assert "at least 2" in result.output


def test_escape_at_the_text_prompt_ends_the_session_without_a_vote(
    monkeypatch, isolated_omm_home
):
    _patch_engine(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: None)
    monkeypatch.setattr(
        cli.arena, "generate_side", lambda *a, **k: pytest_fail_no_generation()
    )
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert not arena.votes_path().exists()


def test_escape_at_the_vote_prompt_ends_the_session_without_a_row(
    monkeypatch, isolated_omm_home
):
    """_ask_single_key cancels by raising KeyboardInterrupt (cli.py:3639),
    not by returning None like _ask_text does."""
    _patch_engine(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "hello")
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))

    def _cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_ask_single_key", _cancel)
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert not arena.votes_path().exists()


def test_bare_enter_at_the_vote_prompt_reasks_instead_of_ending(
    monkeypatch, isolated_omm_home
):
    """_ask_single_key returns default_value on Enter. Ending a battle
    session on a stray Enter would throw away a round the user already
    waited through."""
    _patch_engine(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "hello")
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))
    answers = iter([None, "b"])
    monkeypatch.setattr(cli, "_ask_single_key", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    rows = arena.votes_path().read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["winner"] == "b"


def test_a_failed_round_writes_no_row_and_keeps_the_session_alive(
    monkeypatch, isolated_omm_home
):
    _patch_engine(monkeypatch)
    calls = {"n": 0}

    def _generate(ref, prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise quality.QualityEvaluationError(
                "ran out of memory", failure_reason="out_of_memory"
            )
        return _result("fine")

    monkeypatch.setattr(cli.arena, "generate_side", _generate)
    _patch_prompts(monkeypatch, texts=["p1", "p2"], votes=["b"], continues=[True, False])
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    rows = arena.votes_path().read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1, "only the second, successful round is recorded"
    assert "memory" in result.output.lower()


def test_arena_never_uploads(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=["p"], votes=["a"], continues=[False])
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))
    monkeypatch.setattr(
        cli.telemetry,
        "send_event",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("arena must not upload")),
    )
    assert runner.invoke(cli.app, ["arena"]).exit_code == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cli_arena.py -q`
Expected: FAIL — every test errors with `No such command 'arena'` (exit code 2).

- [ ] **Step 3: Write the implementation**

Add `arena` to `cli.py`'s existing `from omm import ...` block (find the
sibling imports of `benchmark`, `quality as quality_mod`, `linker` near the
top of `cli.py` and add `arena` alphabetically). Then insert after
`evaluate_cmd`:

```python
def _arena_eligible_models(engine: str) -> dict[str, str]:
    """Registry-linked models that `engine` can actually serve right now,
    as {runtime ref -> registry filename}.

    Registry-linked only, per the design: arena rows identify a model by its
    omm registry entry so sub-projects B-E can aggregate across machines. A
    model the user pulled straight into Ollama has no such identity and is
    left out; the caller's error text points those users at `omm import`.
    """
    entries = [
        (filename, value)
        for filename, value in registry.load_registry().items()
        if isinstance(filename, str) and isinstance(value, dict)
    ]
    eligible: dict[str, str] = {}
    if engine == "lmstudio":
        installed = _lmstudio_installed_models()
        for filename, entry in entries:
            resolved = linker.resolve_lmstudio_model(entry.get("repo_id"), filename)
            key = (resolved or {}).get("model_key")
            if isinstance(key, str) and key in installed:
                eligible[key] = filename
        return eligible
    try:
        live = set(quality_mod.list_benchmarkable_tags())
    except quality_mod.QualityEvaluationError:
        live = set()
    runtime_names = linker.resolve_ollama_runtime_names_batch(entries)
    for filename, _entry in entries:
        tag = runtime_names.get(filename)
        if isinstance(tag, str) and tag in live:
            eligible[tag] = filename
    return eligible


def _arena_reveal(
    pair: tuple[str, str], engine: str, result_a, result_b, winner: str
) -> None:
    """Post-vote only. Never call this before a vote is recorded."""
    table = Table(title="Reveal", box=None)
    table.add_column("SIDE")
    table.add_column("MODEL")
    table.add_column("RUNNER")
    table.add_column("TIME", justify="right")
    table.add_column("TOK/S", justify="right")
    for label, ref, result in (
        ("Response 1", pair[0], result_a),
        ("Response 2", pair[1], result_b),
    ):
        speed = "-" if result.tokens_per_second is None else f"{result.tokens_per_second:.1f}"
        table.add_row(label, ref, _engine_label(engine), f"{result.elapsed:.1f}s", speed)
    console.print(table)
    verdict = {
        "a": "You picked Response 1.",
        "b": "You picked Response 2.",
        "both_bad": "You rated both responses bad.",
    }[winner]
    console.print(f"[muted]{verdict}[/muted]")


@app.command(name="arena")
@global_flags
def arena_cmd(
    models: list[str] = typer.Argument(
        None,
        help="Optionally seed the first round with two installed models. "
        "Omit them to draw a random pair.",
    ),
    keep: bool = typer.Option(
        False,
        "--keep",
        help="Reuse the first round's pair for every round instead of "
        "drawing a fresh pair each time.",
    ),
) -> None:
    """Compare two installed models blind, on your own prompt, and vote.

    Responses are shown as "Response 1"/"Response 2" with no name, runner or
    timing until you have voted. Votes are appended to
    `~/.omm/arena/votes.jsonl` and are never uploaded.
    """
    models = list(models or [])
    engine = _select_benchmark_engine_for_models(models) if models else None
    if engine is None:
        engine = _select_benchmark_engine()
        if engine is None:
            _print_no_engine_error("arena")
            raise typer.Exit(1)
        if not _global_opts().quiet:
            _print_engine_selection_notice(engine)
    engine, started_daemon = _ensure_engine_running(
        engine, "arena", assume_yes=_global_opts().yes
    )
    try:
        pool = _arena_eligible_models(engine)
        if len(pool) < 2:
            errors.print_cli_error(
                err_console,
                f"`omm arena` needs at least 2 models installed through omm and "
                f"linked into {_engine_label(engine)}; found {len(pool)}.",
                fix="Run `omm list` to see what omm manages, `omm install` to add "
                "one, or `omm import` to adopt models this runner already has.",
            )
            raise typer.Exit(1)
        try:
            seed_pair = arena.validate_seed_pair(models, sorted(pool)) if models else None
            pairing = arena.Pairing(sorted(pool), seed_pair=seed_pair, keep=keep)
        except arena.ArenaError as error:
            errors.print_cli_error(
                err_console,
                str(error),
                fix="Run `omm list` to see the models omm manages, or `omm import` "
                "to adopt models this runner already has.",
            )
            raise typer.Exit(1) from error
        lmstudio_port = linker.lmstudio_server_port() if engine == "lmstudio" else None
        _arena_session(pairing, pool, engine, lmstudio_port, keep)
    finally:
        if started_daemon is not None:
            _stop_engine_daemon(engine, started_daemon)


def _arena_vote() -> str | None:
    """The vote prompt, re-asked on a bare Enter.

    `_ask_single_key` returns its `default_value` on Enter and raises
    KeyboardInterrupt on Escape/Ctrl-C. Those two gestures must not mean the
    same thing here: a stray Enter mid-session would throw away a round the
    user already waited through, while Escape really does mean "stop".
    Returns None only when the user cancelled.
    """
    while True:
        try:
            winner = _ask_single_key(
                "Which response is better?",
                [("1", "Response 1", "a"), ("2", "Response 2", "b"), ("x", "Both bad", "both_bad")],
                default_value=None,
                instruction="(1/2/x)",
            )
        except KeyboardInterrupt:
            return None
        if winner in arena.WINNERS:
            return winner
        console.print("[muted]Press 1, 2, or x.[/muted]")


def _arena_session(pairing, pool: dict[str, str], engine: str, lmstudio_port, keep: bool) -> None:
    """The round loop. Split out of `arena_cmd` so the daemon-stop `finally`
    above stays readable and covers every exit path out of the loop."""
    rounds = 0
    while True:
        pair = pairing.next_pair()
        prompt = _ask_text("Your prompt for both models:")
        if not prompt or not prompt.strip():
            break
        prompt = prompt.strip()
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[accent]{task.description}[/accent]"),
                TimeElapsedColumn(),
                console=console,
                disable=_global_opts().quiet,
            ) as progress:
                # One undifferentiated description for both sides: a per-model
                # spinner label would name the model before the vote.
                task_id = progress.add_task("Generating both responses...", total=2)
                result_a = arena.generate_side(
                    pair[0], prompt, engine=engine, lmstudio_port=lmstudio_port
                )
                progress.advance(task_id)
                result_b = arena.generate_side(
                    pair[1], prompt, engine=engine, lmstudio_port=lmstudio_port
                )
                progress.advance(task_id)
        except quality_mod.QualityEvaluationError as error:
            err_console.print(f"[error]This round failed: {escape(str(error))}[/error]")
        else:
            console.print()
            console.print("[accent]Response 1[/accent]")
            console.print(result_a.text or "[muted](empty response)[/muted]")
            console.print()
            console.print("[accent]Response 2[/accent]")
            console.print(result_b.text or "[muted](empty response)[/muted]")
            console.print()
            winner = _arena_vote()
            if winner is None:
                break
            _arena_reveal(pair, engine, result_a, result_b, winner)
            row = arena.build_vote_row(
                prompt=prompt,
                model_a=pool[pair[0]],
                model_b=pool[pair[1]],
                engine=engine,
                result_a=result_a,
                result_b=result_b,
                winner=winner,
                kept=keep,
            )
            if not arena.append_vote(row):
                err_console.print(
                    "[warn]Could not write this vote to "
                    f"{escape(str(arena.votes_path()))}; the round still counted "
                    "on screen but was not saved.[/warn]"
                )
            rounds += 1
        try:
            another = _ask_confirm("Another round?", default=True)
        except KeyboardInterrupt:
            # _ask_confirm wraps _ask_single_key, so Escape here raises too.
            break
        if not another:
            break
    if rounds and not _global_opts().quiet:
        console.print(
            f"[muted]{rounds} round(s) recorded in {escape(str(arena.votes_path()))}.[/muted]"
        )
```

Check the names of `Progress`, `SpinnerColumn`, `TextColumn`,
`TimeElapsedColumn`, `Table`, `escape`, `errors`, `registry`, `_engine_label`
against the top of `cli.py` before relying on them — they are all already
imported there for `benchmark_cmd`; do not re-import.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cli_arena.py -q`
Expected: PASS (11 passed)

Then the whole suite, minus the file that can delete the real `~/.omm/src`:

Run: `python -m pytest -q --ignore=tests/test_cli_update.py`
Expected: PASS, apart from the known `FORCE_COLOR=3` colour failures described
in Task 3 Step 4.

- [ ] **Step 5: Smoke-check the real command**

Run:
```bash
OMM_HOME=$(mktemp -d) python -m omm.cli arena --help
```
Expected: the help text shows `--keep` and the two optional model arguments,
and no traceback.

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_arena.py
git commit -m "feat(cli): add omm arena blind model battle command"
```

---

### Task 6: Documentation sync

**Files:**
- Modify: `docs/commands.json` (regenerated, never hand-edited)
- Modify: `README.md` (`## Usage` section)
- Modify: `docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md`

`CLAUDE.md` makes this mandatory: `src/omm/cli.py` is the single source of
truth for commands, and CI job `docs-sync` runs the same check as
`scripts/check_docs_sync.py`.

- [ ] **Step 1: Regenerate the command reference**

Run: `python scripts/export_command_reference.py`
Expected: `docs/commands.json` gains an `arena` entry.

- [ ] **Step 2: Add the README usage line**

In `README.md`'s `## Usage` block, add a line in the same
`omm cmd [flags]  # description` format the neighbouring entries use:

```
omm arena [MODEL1 MODEL2] [--keep]  # Compare two installed models blind and vote
```

- [ ] **Step 3: Verify docs sync**

Run: `python scripts/check_docs_sync.py`
Expected: exits 0 with no diff reported.

If it reports a mismatch on unrelated commands, your local `click`/`typer`
versions differ from CI's resolved versions — reinstall with
`python -m pip install -e ".[dev]"` before regenerating, or the committed
`docs/commands.json` will fail the `docs-sync` job.

- [ ] **Step 4: Record the resolved decisions in the spec**

In `docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md`, under
"Product contract", replace the two TBD bullets so the spec matches what
shipped:

- The command-name bullet becomes: "Command name is `omm arena` (confirmed
  2026-09-26). `compare` stays reserved for the existing catalog-comparison
  command at `cli.py:4333`."
- The pairing bullet's `--pin` becomes `--keep` (confirmed 2026-09-26;
  `pin` is reserved for model-version pinning, issue #295).

Add two bullets to the same section:

- "A session uses one engine for every round (confirmed 2026-09-26).
  `engine_a`/`engine_b` stay separate fields so cross-engine pairing needs no
  schema migration later."
- "`watt_a`/`watt_b` are written as `null` in sub-project A (confirmed
  2026-09-26); the `nvidia-smi`/`rocm-smi` reader lands with sub-project C,
  which is the first consumer. `memory_gb_*` is measured for Ollama via
  `/api/ps`."

- [ ] **Step 5: Commit**

```bash
git add docs/commands.json README.md docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md
git commit -m "docs: document omm arena and resolve the spec's open decisions"
```

---

## Done criteria

- `python -m pytest tests/test_arena.py tests/test_cli_arena.py tests/test_quality.py -q` passes.
- `python -m pytest -q --ignore=tests/test_cli_update.py` shows no new failures
  beyond the known `FORCE_COLOR=3` colour ones.
- `python scripts/check_docs_sync.py` exits 0.
- `omm arena --help` works from a scratch `OMM_HOME`.
- Nothing has been pushed. Pushing and opening the PR (Korean body, four
  required sections, base `beta`, closes #407) is a separate explicit ask.
