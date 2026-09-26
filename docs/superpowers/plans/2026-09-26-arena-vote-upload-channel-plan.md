# Arena vote upload channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `omm`'s fourth opt-in outbound channel: arena battle votes go
from `~/.omm/arena/votes.jsonl` into a new private Firebase RTDB `votes`
node through the existing Cloudflare Worker PoW gateway, with the user's
prompt text never leaving the machine.

**Architecture:** A new `src/omm/arena_upload.py` modeled on `usage.py`
owns a separate pending queue, backoff, and log; `cli.py` asks for consent
at the end of an arena session and adds one `flush_pending()` call to the
existing `_root` flush pass. The Worker gains a `/votes` route and a
`validateVoteEvent`, `database.rules.json` gains a `votes` block. No new
runtime dependency.

**Tech Stack:** Python 3.10+, Typer, `filelock` via `omm.atomic.locked`,
`requests` (lazy-imported), pytest; TypeScript + vitest for `cf-worker`;
Firebase RTDB security rules exercised by the emulator via
`scripts/test_firebase_rules.mjs` (needs Java + node).

**Spec:** `docs/superpowers/specs/2026-09-26-arena-vote-upload-channel-design.md`
(sub-project B only). GitHub issue: `omm-hippo/omm#408`. It depends on
sub-project A (`omm arena`, issue #407), already implemented on this branch.

## Global Constraints

- **The user's prompt text is never uploaded**, and neither is anything
  derived from it — no hash, no length bucket, no category. `validateVoteEvent`
  rejects a `prompt` key by name, and a Python test asserts the converted
  payload has no such key. This is the spec's decision 1 and the single most
  important property of this work.
- The channel stays **opt-in**. Default policy is ask-every-time; nothing is
  ever sent without the user answering yes.
- **Policy config key is `arena_vote_send_policy` with the values
  `"always" | "never" | "ask"`**, matching `telemetry_send_policy`'s existing
  vocabulary rather than the spec prose's "enabled/disabled/ask". The consent
  UX is the same `y/n/a` prompt `_ask_upload_choice` already serves for the
  benchmark channel, whose `a` branch saves `"always"`; inventing a second
  vocabulary for the identical flow would be gratuitous. `usage` uses
  `usage_stats_policy` with `"enabled"`/`"never"` and is the odd one out.
- Rows are enqueued **only after consent**, so the queue never holds data the
  user has not agreed to send. `flush_pending` therefore does not require an
  `"always"` policy — it sends whatever is queued — but discards the queue
  without sending when the policy is `"never"`.
- `arena_upload`'s public functions **swallow their own exceptions** and are
  import-side-effect-free, reading `config.OMM_HOME` at call time (the
  `runlog.py:_logs_dir` / `usage.py` contract). An upload problem must never
  change `omm arena`'s exit code or lose a local vote row.
- No new runtime dependency. `requests` and `questionary` stay lazy-imported
  inside functions; `cli.py`'s startup time is a tracked property.
- `PRIVACY.md`, the `omm setting upload` copy, `cf-worker/src/validate.ts`,
  and `database.rules.json` must all describe the same fields. Changing one
  without the others is the documented failure mode.
- Python 3.10+ syntax only (`X | None`). `git add` only your own files —
  other Claude sessions share this checkout. Branch is
  `feat/arena-battle-cli` (already off `beta`); do not push, that is a
  separate explicit ask.
- `scripts/pre-commit` auto-bumps the patch version on every commit. Expected.
  Never `--no-verify`.
- The required CI checks never run `cf-worker`'s own suite. After any
  `cf-worker` change run, by hand:
  `cd cf-worker && npm ci && npm test && npx tsc -p tsconfig.json`.

## Review Focus

Failure modes the spec implies but does not spell out. Each has its test in
the task that owns the code.

1. **A `prompt` key reaching the wire.** The one regression that would break
   the promise this whole design is built on. It needs an assertion on the
   Python side (the payload builder) *and* on the Worker side (the
   validator), because either alone leaves a path open. (Tests in Tasks 2
   and 5.)
2. **A `votes.jsonl` row from a future schema version.** Sub-project A's row
   gained two fields mid-implementation; it will gain more. An unknown key in
   a local row must not end up forwarded verbatim to a validator that rejects
   unknown keys — the payload builder must allow-list what it copies, not
   copy-then-delete. (Test in Task 2.)
3. **Consent declined, then the channel enabled later.** If `n` left rows on
   disk, a later `omm setting upload votes --enable` would silently ship
   votes the user refused. `n` must leave nothing behind. (Test in Task 4.)
4. **A half-sided row.** If one side's generation produced no token count,
   its `tokens_per_second_b` is omitted while `_a` is present. The validator
   rejects asymmetric rows, so the builder must omit symmetrically or the
   row is silently dropped server-side forever. (Tests in Tasks 2 and 5.)
5. **A flush racing another `omm` process.** Two processes flushing the same
   queue must not both POST it, and the loser must not stall a user-facing
   command. (Test in Task 3.)

---

## File Structure

- **Create** `src/omm/arena_upload.py` — policy, payload conversion, queue,
  backoff, log, flush. No Typer, no `rich`, no prompts. Target ~300 lines.
- **Create** `tests/test_arena_upload.py` — unit tests for the module.
- **Create** `tests/test_cli_arena_upload.py` — CliRunner tests for the
  consent prompt and the `omm setting upload votes` command.
- **Modify** `src/omm/config.py` — `VOTES_GATEWAY_ENDPOINT`,
  `arena_vote_send_policy` default + validation, `client_id()` docstring.
- **Modify** `src/omm/cli.py` — session-end consent in `_arena_session`,
  `flush_pending()` in `_root`, `configure_upload_votes` command, the
  4-channel picker and policy table.
- **Modify** `cf-worker/src/index.ts` — `/votes` route.
- **Modify** `cf-worker/src/validate.ts` — `VOTE_FIELDS`, `validateVoteEvent`.
- **Modify** `cf-worker/test/validate.test.ts` — validator tests.
- **Modify** `database.rules.json` — `votes` block.
- **Modify** `scripts/test_firebase_rules.mjs` — deny-read/deny-write checks.
- **Modify** `PRIVACY.md`, `README.md`, `docs/commands.json`, `CLAUDE.md`.

---

### Task 1: Config — endpoint, policy, client_id docstring

**Files:**
- Modify: `src/omm/config.py` (`VOTES_GATEWAY_ENDPOINT` next to
  `USAGE_GATEWAY_ENDPOINT` at `config.py:76`; `DEFAULT_CONFIG` near
  `config.py:98`; the policy validation block near `config.py:205`;
  `client_id()` docstring at `config.py:309`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `config.VOTES_GATEWAY_ENDPOINT: str`
  - `DEFAULT_CONFIG["arena_vote_send_policy"] == "ask"`
  - `load_config()` coerces an invalid `arena_vote_send_policy` to `"ask"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_arena_vote_policy_defaults_to_ask(isolated_omm_home):
    assert config.DEFAULT_CONFIG["arena_vote_send_policy"] == "ask"
    assert config.load_config()["arena_vote_send_policy"] == "ask"


def test_arena_vote_policy_rejects_an_unknown_value(isolated_omm_home):
    """Same coercion telemetry_send_policy gets: an unreadable policy must
    fall back to the safe default, never be treated as consent."""
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text(
        json.dumps({"arena_vote_send_policy": "yes-please"}), encoding="utf-8"
    )
    assert config.load_config()["arena_vote_send_policy"] == "ask"


def test_votes_gateway_endpoint_is_the_shared_worker():
    assert config.VOTES_GATEWAY_ENDPOINT.endswith("/votes")
    assert config.VOTES_GATEWAY_ENDPOINT.startswith("https://")
    # Same Worker host as every other channel - a second host would need its
    # own PoW/rate-limit deployment.
    assert (
        config.VOTES_GATEWAY_ENDPOINT.rsplit("/", 1)[0]
        == config.USAGE_GATEWAY_ENDPOINT.rsplit("/", 1)[0]
    )
```

Add `import json` to that file's imports if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -q -k "arena_vote or votes_gateway"`
Expected: FAIL — `KeyError: 'arena_vote_send_policy'` and
`AttributeError: module 'omm.config' has no attribute 'VOTES_GATEWAY_ENDPOINT'`

- [ ] **Step 3: Write the implementation**

After `USAGE_GATEWAY_ENDPOINT` at `config.py:76`:

```python
VOTES_GATEWAY_ENDPOINT = "https://omm-telemetry-gateway.seong381400.workers.dev/votes"
```

In `DEFAULT_CONFIG`, next to `"telemetry_send_policy": "ask"`:

```python
    # Arena battle votes (`omm arena`). "ask" means the user is asked at the
    # end of a battle session, the same y/n/a prompt the benchmark channel
    # uses; "a" saves "always" here. Never uploads the prompt text - see
    # PRIVACY.md and cf-worker/src/validate.ts:validateVoteEvent.
    "arena_vote_send_policy": "ask",
```

In the validation block near `config.py:205`, beside the
`telemetry_send_policy` coercion:

```python
    if merged.get("arena_vote_send_policy") not in {"always", "never", "ask"}:
        merged["arena_vote_send_policy"] = "ask"
```

In `client_id()`'s docstring, replace `its only caller (omm.usage)` with
`its callers (omm.usage, omm.arena_upload)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: PASS, no regressions in the existing config tests.

- [ ] **Step 5: Commit**

```bash
git add src/omm/config.py tests/test_config.py
git commit -m "feat(config): add the arena vote upload endpoint and policy"
```

---

### Task 2: `arena_upload` payload conversion

**Files:**
- Create: `src/omm/arena_upload.py`
- Test: `tests/test_arena_upload.py`

**Interfaces:**
- Consumes: `config.OMM_HOME`, `config.client_id()`, `registry.load_registry()`,
  `package_metadata` for the client version.
- Produces:
  - `SCHEMA_VERSION: int` = 1
  - `policy(config_data: dict | None = None) -> str` — `"always" | "never" | "ask"`
  - `build_payload(vote_row: dict, *, registry_entries: dict | None = None) -> dict | None`
    — one wire payload, or `None` when the row is unusable
  - `WIRE_FIELDS: frozenset[str]` — every key that may appear on the wire

`build_payload` **allow-lists**: it reads the specific keys it knows from
the local row and writes the specific keys it knows to the payload. It never
copies the row and deletes fields, because sub-project A's local schema will
gain fields and a copy-then-delete builder forwards each new one to a
validator that rejects unknown keys.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arena_upload.py`:

```python
from __future__ import annotations

import json

import pytest

from omm import arena_upload, config, registry


def _local_row(**overrides):
    """A full sub-project A votes.jsonl row."""
    row = {
        "battle_id": "0a8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a",
        "timestamp": "2026-09-26T04:05:06.700000+00:00",
        "prompt": "our internal deploy runbook, summarize it",
        "model_a": "alpha-4b-Q4_K_M.gguf",
        "model_b": "beta-8b-Q5_K_M.gguf",
        "engine_a": "ollama",
        "engine_b": "ollama",
        "elapsed_a": 3.25,
        "elapsed_b": 9.5,
        "tokens_a": 120,
        "tokens_b": 300,
        "memory_gb_a": 3.4,
        "memory_gb_b": None,
        "tokens_per_second_a": 41.2,
        "tokens_per_second_b": None,
        "watt_a": None,
        "watt_b": None,
        "winner": "a",
        "pinned": False,
    }
    row.update(overrides)
    return row


def _registry():
    return {
        "alpha-4b-Q4_K_M.gguf": {
            "provider": "huggingface",
            "repo_id": "org/alpha-gguf",
            "sha256": "a" * 64,
            "quantization_bits": 4,
        },
        "beta-8b-Q5_K_M.gguf": {"provider": "modelscope"},
    }


def test_payload_never_carries_the_prompt(isolated_omm_home):
    """The single most important assertion in this sub-project."""
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert "prompt" not in payload
    serialized = json.dumps(payload)
    assert "internal deploy runbook" not in serialized
    # ...and no hash or length of it either.
    assert not [k for k in payload if "prompt" in k]


def test_payload_fields_are_all_allow_listed(isolated_omm_home):
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert set(payload) <= arena_upload.WIRE_FIELDS
    assert payload["schema_version"] == 1
    assert payload["battle_id"] == "0a8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a"
    assert payload["recorded_at"] == "2026-09-26T04:05:06.700000+00:00"
    assert payload["engine"] == "ollama"
    assert payload["winner"] == "a"
    assert payload["pinned"] is False
    assert payload["client_id"] == config.client_id()


def test_payload_ignores_an_unknown_field_from_a_future_local_schema(isolated_omm_home):
    """A's local row will gain fields. A copy-then-delete builder would
    forward each new one to a validator that rejects unknown keys."""
    payload = arena_upload.build_payload(
        _local_row(some_future_field="x", another={"nested": 1}),
        registry_entries=_registry(),
    )
    assert "some_future_field" not in payload
    assert "another" not in payload
    assert set(payload) <= arena_upload.WIRE_FIELDS


def test_payload_fills_model_identity_from_the_registry(isolated_omm_home):
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert payload["model_filename_a"] == "alpha-4b-Q4_K_M.gguf"
    assert payload["model_provider_a"] == "huggingface"
    assert payload["model_repo_id_a"] == "org/alpha-gguf"
    assert payload["model_digest_a"] == "a" * 64
    assert payload["quant_bits_a"] == 4


def test_payload_omits_identity_fields_the_registry_lacks(isolated_omm_home):
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert payload["model_filename_b"] == "beta-8b-Q5_K_M.gguf"
    assert payload["model_provider_b"] == "modelscope"
    # The key is absent, not present-and-None: usage._post_to strips None
    # before signing, so a None here would silently vanish anyway - being
    # explicit keeps the wire shape checkable.
    assert "model_repo_id_b" not in payload
    assert "model_digest_b" not in payload
    assert "quant_bits_b" not in payload


def test_payload_never_sends_watts(isolated_omm_home):
    """Sub-project B measures no power; C is the first consumer."""
    payload = arena_upload.build_payload(
        _local_row(watt_a=180.0, watt_b=200.0), registry_entries=_registry()
    )
    assert "watt_a" not in payload
    assert "watt_b" not in payload


def test_payload_omits_optional_measurements_symmetrically(isolated_omm_home):
    """The Worker validator rejects an asymmetric row, so a measurement
    missing on one side must drop the other side's too - otherwise the row
    is silently dropped server-side forever."""
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    # memory_gb_b and tokens_per_second_b are None in the fixture.
    assert "memory_gb_a" not in payload and "memory_gb_b" not in payload
    assert "tokens_per_second_a" not in payload
    assert "tokens_per_second_b" not in payload
    # Required-on-both fields stay.
    assert payload["elapsed_a"] == 3.25 and payload["elapsed_b"] == 9.5
    assert payload["tokens_a"] == 120 and payload["tokens_b"] == 300


def test_payload_is_none_for_a_row_missing_a_required_field(isolated_omm_home):
    for missing in ("battle_id", "winner", "model_a", "engine_a"):
        row = _local_row()
        del row[missing]
        assert arena_upload.build_payload(row, registry_entries=_registry()) is None


def test_payload_is_none_when_the_two_engines_disagree(isolated_omm_home):
    """A session is single-engine. A row saying otherwise came from a
    future cross-engine mode this wire schema cannot express (one `engine`)."""
    row = _local_row(engine_b="lmstudio")
    assert arena_upload.build_payload(row, registry_entries=_registry()) is None


def test_payload_is_none_for_an_unknown_winner(isolated_omm_home):
    assert arena_upload.build_payload(_local_row(winner="tie"), registry_entries=_registry()) is None


def test_policy_reads_the_config_key(isolated_omm_home):
    assert arena_upload.policy({"arena_vote_send_policy": "always"}) == "always"
    assert arena_upload.policy({"arena_vote_send_policy": "never"}) == "never"
    assert arena_upload.policy({}) == "ask"
    assert arena_upload.policy({"arena_vote_send_policy": "nonsense"}) == "ask"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_arena_upload.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'omm.arena_upload'`

- [ ] **Step 3: Write the implementation**

Create `src/omm/arena_upload.py`:

```python
"""Arena battle vote uploads (`omm arena`'s opt-in outbound channel).

Sub-project B of the arena roadmap: take the rows `omm arena` wrote to
``~/.omm/arena/votes.jsonl`` and ship them, opt-in, through the shared
Cloudflare Worker PoW gateway into the private Firebase RTDB ``votes``
node. Modeled on ``usage.py``, which solved the same queue/backoff/flush
problems for the anonymous daily batch.

**The user's prompt text is never uploaded**, and neither is anything
derived from it. ``build_payload`` allow-lists what reaches the wire; the
Worker's ``validateVoteEvent`` rejects a ``prompt`` key by name as a second
line of defense. See the design at
docs/superpowers/specs/2026-09-26-arena-vote-upload-channel-design.md and
the user-facing PRIVACY.md.

Every public function here swallows its own errors: a vote is already saved
locally by the time any of this runs, and an upload problem must never take
down the battle session that produced it.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from filelock import Timeout as FileLockTimeout

from . import config
from .atomic import atomic_write_text, backup_corrupt_file, locked

logger = logging.getLogger("omm.arena_upload")

SCHEMA_VERSION = 1
_PENDING_MAX = 5000
_MAX_LOG_LINES = 500
_DETAIL_SLICE = 300
_BACKOFF_SECONDS = 6 * 3600

WINNERS = frozenset({"a", "b", "both_bad"})
ENGINES = frozenset({"ollama", "lmstudio"})

#: Every key that may appear on the wire. Mirrored by `VOTE_FIELDS` in
#: cf-worker/src/validate.ts and by the `votes` block in
#: database.rules.json - all three must agree.
WIRE_FIELDS = frozenset(
    {
        "schema_version",
        "battle_id",
        "client_id",
        "client_version",
        "recorded_at",
        "engine",
        "winner",
        "pinned",
    }
    | {
        f"{name}_{side}"
        for side in ("a", "b")
        for name in (
            "model_provider",
            "model_repo_id",
            "model_filename",
            "model_digest",
            "quant_bits",
            "elapsed",
            "tokens",
            "tokens_per_second",
            "memory_gb",
            "watt",
        )
    }
)


def arena_dir() -> Path:
    """Resolved at call time; OMM_HOME is overridable (runlog._logs_dir)."""
    return config.OMM_HOME / "arena"


def _pending_path() -> Path:
    return arena_dir() / "votes-pending.jsonl"


def _backoff_path() -> Path:
    return arena_dir() / "votes-backoff.json"


def _log_path() -> Path:
    return arena_dir() / "votes-upload.log"


def policy(config_data: dict[str, Any] | None = None) -> str:
    """"always" | "never" | "ask" - same vocabulary as
    telemetry_send_policy, because the consent prompt is the same y/n/a."""
    data = config_data if config_data is not None else config.load_config()
    value = data.get("arena_vote_send_policy")
    return value if value in {"always", "never", "ask"} else "ask"


def _client_version() -> str | None:
    try:
        from . import package_metadata

        version = package_metadata.installed_version()
    except Exception:
        return None
    return version if isinstance(version, str) and version else None


def _optional_str(value: object, limit: int) -> str | None:
    if isinstance(value, str) and value and len(value) <= limit:
        return value
    return None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().removeprefix("sha256:")
    if len(lowered) == 64 and all(c in "0123456789abcdef" for c in lowered):
        return lowered
    return None


def build_payload(
    vote_row: dict, *, registry_entries: dict | None = None
) -> dict | None:
    """One wire payload from one local votes.jsonl row, or None when the
    row cannot make a valid one.

    Allow-listing, not copy-then-delete: sub-project A's local row gains
    fields over time (it gained tokens_per_second_a/b during its own
    implementation), and a builder that copied the row would forward each
    new field to a validator that rejects unknown keys.
    """
    if not isinstance(vote_row, dict):
        return None
    battle_id = _optional_str(vote_row.get("battle_id"), 64)
    recorded_at = _optional_str(vote_row.get("timestamp"), 50)
    winner = vote_row.get("winner")
    engine_a = vote_row.get("engine_a")
    if (
        battle_id is None
        or recorded_at is None
        or winner not in WINNERS
        or engine_a not in ENGINES
        # A session is single-engine; one `engine` field cannot express a
        # cross-engine row, so refuse rather than pick a side.
        or vote_row.get("engine_b") != engine_a
    ):
        return None

    if registry_entries is None:
        from . import registry

        try:
            registry_entries = registry.load_registry()
        except Exception:
            registry_entries = {}

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "battle_id": battle_id,
        "client_id": config.client_id(),
        "recorded_at": recorded_at,
        "engine": engine_a,
        "winner": winner,
        "pinned": bool(vote_row.get("pinned")),
    }
    client_version = _client_version()
    if client_version is not None:
        payload["client_version"] = client_version

    # Required per side. A row missing either is unusable.
    sides: dict[str, dict[str, Any]] = {}
    for side in ("a", "b"):
        filename = _optional_str(vote_row.get(f"model_{side}"), 300)
        elapsed = _optional_number(vote_row.get(f"elapsed_{side}"))
        tokens = vote_row.get(f"tokens_{side}")
        if (
            filename is None
            or elapsed is None
            or isinstance(tokens, bool)
            or not isinstance(tokens, int)
        ):
            return None
        sides[side] = {"filename": filename, "elapsed": elapsed, "tokens": tokens}

    for side, values in sides.items():
        payload[f"model_filename_{side}"] = values["filename"]
        payload[f"elapsed_{side}"] = round(values["elapsed"], 3)
        payload[f"tokens_{side}"] = values["tokens"]
        entry = registry_entries.get(values["filename"]) or {}
        if not isinstance(entry, dict):
            entry = {}
        provider = _optional_str(entry.get("provider"), 64)
        if provider is not None:
            payload[f"model_provider_{side}"] = provider
        repo_id = _optional_str(entry.get("repo_id"), 512)
        if repo_id is not None:
            payload[f"model_repo_id_{side}"] = repo_id
        digest = _digest(entry.get("sha256"))
        if digest is not None:
            payload[f"model_digest_{side}"] = digest
        quant_bits = _optional_number(entry.get("quantization_bits"))
        if quant_bits is not None:
            payload[f"quant_bits_{side}"] = quant_bits

    # Optional measurements go in only when BOTH sides have them: the
    # Worker validator rejects an asymmetric row, so sending one side alone
    # would have the row silently dropped server-side forever.
    for name, source in (("memory_gb", "memory_gb"), ("tokens_per_second", "tokens_per_second")):
        left = _optional_number(vote_row.get(f"{source}_a"))
        right = _optional_number(vote_row.get(f"{source}_b"))
        if left is not None and right is not None:
            payload[f"{name}_a"] = left
            payload[f"{name}_b"] = right

    # watt_* is never sent by sub-project B - no power reader exists yet.
    # The validator and rules accept the field so sub-project C, its first
    # consumer, needs no rules redeploy.
    return payload
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_arena_upload.py -q`
Expected: PASS (12 passed)

If `package_metadata.installed_version` does not exist under that name,
read `src/omm/package_metadata.py` and use the real accessor; do not invent
one. The payload's `client_version` is optional, so a `None` return is a
valid outcome, not a failure.

- [ ] **Step 5: Commit**

```bash
git add src/omm/arena_upload.py tests/test_arena_upload.py
git commit -m "feat(arena): add vote upload payload conversion"
```

---

### Task 3: `arena_upload` queue, backoff, and flush

**Files:**
- Modify: `src/omm/arena_upload.py`
- Test: `tests/test_arena_upload.py`

**Interfaces:**
- Consumes: `build_payload`, `policy`, `WIRE_FIELDS` (Task 2);
  `telemetry._solve_proof_of_work`, `telemetry._remove_sent_snapshot_entries`
  (existing).
- Produces:
  - `enqueue(vote_rows: list[dict]) -> int` — payloads queued
  - `pending_count() -> int`
  - `discard_pending() -> int`
  - `flush_pending(force: bool = False) -> int` — rows sent
  - `log_attempt(outcome: str, detail: str = "") -> None`

Callers enqueue only what the user consented to (spec decision 7), so
`flush_pending` does not gate on `"always"`. It discards without sending
when the policy is `"never"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arena_upload.py`:

```python
def _stub_post(monkeypatch, *, ok=True, status=200, raises=None):
    sent = []

    class _Resp:
        status_code = status
        text = ""

    class _Requests:
        @staticmethod
        def post(endpoint, json=None, timeout=None):
            if raises is not None:
                raise raises
            sent.append((endpoint, json))
            return _Resp()

        class RequestException(Exception):
            pass

    import sys

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    monkeypatch.setattr(
        "omm.telemetry._solve_proof_of_work", lambda event_json: (1, 2)
    )
    return sent


def test_enqueue_writes_one_payload_per_row(isolated_omm_home):
    assert arena_upload.enqueue([_local_row(), _local_row()]) == 2
    assert arena_upload.pending_count() == 2
    lines = arena_upload._pending_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "prompt" not in json.loads(lines[0])


def test_enqueue_skips_a_row_it_cannot_convert(isolated_omm_home):
    assert arena_upload.enqueue([_local_row(winner="tie"), _local_row()]) == 1
    assert arena_upload.pending_count() == 1


def test_enqueue_never_raises_on_a_disk_error(isolated_omm_home, monkeypatch):
    """`omm arena`'s session end calls this; a disk problem must not change
    the command's exit code or lose the local vote row."""
    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(arena_upload.Path, "mkdir", _boom)
    assert arena_upload.enqueue([_local_row()]) == 0


def test_discard_pending_empties_the_queue(isolated_omm_home):
    arena_upload.enqueue([_local_row(), _local_row()])
    assert arena_upload.discard_pending() == 2
    assert arena_upload.pending_count() == 0
    assert not arena_upload._pending_path().exists()


def test_flush_sends_queued_rows_under_an_ask_policy(isolated_omm_home, monkeypatch):
    """The one-time-consent path: `y` queues rows and leaves the policy
    "ask". A flush that demanded "always" would strand them forever."""
    config.update_config(arena_vote_send_policy="ask")
    arena_upload.enqueue([_local_row(), _local_row()])
    sent = _stub_post(monkeypatch)
    assert arena_upload.flush_pending() == 2
    assert len(sent) == 2
    assert all(endpoint == config.VOTES_GATEWAY_ENDPOINT for endpoint, _ in sent)
    assert arena_upload.pending_count() == 0


def test_flush_discards_without_sending_when_the_policy_is_never(
    isolated_omm_home, monkeypatch
):
    arena_upload.enqueue([_local_row()])
    config.update_config(arena_vote_send_policy="never")
    _stub_post(monkeypatch, raises=AssertionError("must not POST"))
    assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 0


def test_flush_on_an_empty_queue_does_no_http(isolated_omm_home, monkeypatch):
    _stub_post(monkeypatch, raises=AssertionError("must not POST"))
    assert arena_upload.flush_pending() == 0


def test_flush_refuses_a_foreign_endpoint(isolated_omm_home, monkeypatch):
    """This channel has no self-hosted variant; only the shared Worker."""
    arena_upload.enqueue([_local_row()])
    _stub_post(monkeypatch, raises=AssertionError("must not POST"))
    assert arena_upload._post_to("https://evil.example/votes", {"a": 1}) is False


def test_an_http_error_sets_a_backoff_and_keeps_the_queue(
    isolated_omm_home, monkeypatch
):
    arena_upload.enqueue([_local_row()])
    _stub_post(monkeypatch, status=500)
    assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 1
    # Backoff is now active, so a second call sends nothing at all.
    _stub_post(monkeypatch, raises=AssertionError("must not POST during backoff"))
    assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 1


def test_force_ignores_the_backoff(isolated_omm_home, monkeypatch):
    arena_upload.enqueue([_local_row()])
    _stub_post(monkeypatch, status=500)
    arena_upload.flush_pending()
    sent = _stub_post(monkeypatch)
    assert arena_upload.flush_pending(force=True) == 1
    assert len(sent) == 1


def test_a_row_queued_during_a_slow_send_survives(isolated_omm_home, monkeypatch):
    arena_upload.enqueue([_local_row()])
    late = _local_row(battle_id="11111111-2222-4333-8444-555555555555")

    class _Resp:
        status_code = 200
        text = ""

    class _Requests:
        @staticmethod
        def post(endpoint, json=None, timeout=None):
            arena_upload.enqueue([late])
            return _Resp()

        class RequestException(Exception):
            pass

    import sys

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    monkeypatch.setattr("omm.telemetry._solve_proof_of_work", lambda event_json: (1, 2))
    assert arena_upload.flush_pending() == 1
    remaining = [
        json.loads(line)
        for line in arena_upload._pending_path().read_text(encoding="utf-8").splitlines()
    ]
    assert [r["battle_id"] for r in remaining] == [late["battle_id"]]


def test_a_losing_flush_race_gives_up_instead_of_waiting(isolated_omm_home, monkeypatch):
    """Two omm processes must not both POST the same queue, and the loser
    must not stall a user-facing command."""
    from omm.atomic import locked as real_locked

    arena_upload.enqueue([_local_row()])
    _stub_post(monkeypatch, raises=AssertionError("must not POST while locked"))
    flush_lock = arena_upload._pending_path().with_name(
        arena_upload._pending_path().name + ".flush"
    )
    with real_locked(flush_lock, timeout=10):
        assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 1


def test_a_corrupt_pending_file_does_not_crash(isolated_omm_home, monkeypatch):
    arena_upload._pending_path().parent.mkdir(parents=True, exist_ok=True)
    arena_upload._pending_path().write_text("{not json\n", encoding="utf-8")
    assert arena_upload.pending_count() == 0
    _stub_post(monkeypatch)
    assert arena_upload.flush_pending() == 0


def test_the_queue_drops_the_oldest_rows_past_the_cap(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(arena_upload, "_PENDING_MAX", 2)
    rows = [
        _local_row(battle_id=f"{i:08d}-2222-4333-8444-555555555555") for i in range(4)
    ]
    arena_upload.enqueue(rows)
    queued = [
        json.loads(line)["battle_id"]
        for line in arena_upload._pending_path().read_text(encoding="utf-8").splitlines()
    ]
    assert queued == [rows[2]["battle_id"], rows[3]["battle_id"]]


def test_the_attempt_log_is_local_and_bounded(isolated_omm_home):
    for i in range(arena_upload._MAX_LOG_LINES + 20):
        arena_upload.log_attempt("sent_ok", f"row {i}")
    lines = arena_upload._log_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) <= arena_upload._MAX_LOG_LINES
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_arena_upload.py -q -k "enqueue or flush or discard or backoff or corrupt or queue_is_capped or attempt_log or race or foreign"`
Expected: FAIL — `AttributeError: module 'omm.arena_upload' has no attribute 'enqueue'`

- [ ] **Step 3: Write the implementation**

Append to `src/omm/arena_upload.py`:

```python
def _read_pending_unlocked(path: Path) -> list[dict]:
    try:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
    except OSError:
        return []


def _read_pending() -> list[dict]:
    path = _pending_path()
    try:
        with locked(path, timeout=10):
            return _read_pending_unlocked(path)
    except (OSError, FileLockTimeout):
        return []


def pending_count() -> int:
    return len(_read_pending())


def enqueue(vote_rows: list[dict]) -> int:
    """Convert consented local rows into wire payloads and queue them.

    Callers must have consent already (spec decision 7): the queue never
    holds data the user has not agreed to send. Returns how many rows were
    queued; never raises.
    """
    try:
        if policy() == "never":
            return 0
        payloads = []
        for row in vote_rows or []:
            payload = build_payload(row)
            if payload is not None:
                payloads.append(payload)
        if not payloads:
            return 0
        path = _pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked(path, timeout=10):
            existing = _read_pending_unlocked(path)
            combined = (existing + payloads)[-_PENDING_MAX:]
            atomic_write_text(
                path, "".join(json.dumps(p, sort_keys=True) + "\n" for p in combined)
            )
        logger.info("arena votes queued", extra={"queued": len(payloads)})
        return len(payloads)
    except Exception as error:
        logger.debug("arena vote enqueue failed: %s", error)
        return 0


def discard_pending() -> int:
    """Drop the queue unsent. Used when the user declines, and when the
    channel is turned off - a refused vote must not linger on disk."""
    try:
        path = _pending_path()
        with locked(path, timeout=10):
            count = len(_read_pending_unlocked(path))
            path.unlink(missing_ok=True)
        return count
    except (OSError, FileLockTimeout):
        return 0


def log_attempt(outcome: str, detail: str = "") -> None:
    """Local-only attempt log. Never uploaded, bounded, best-effort."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"{stamp} {outcome} {detail[:_DETAIL_SLICE]}".rstrip()
        with locked(path, timeout=5):
            try:
                existing = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                existing = []
            kept = (existing + [line])[-_MAX_LOG_LINES:]
            atomic_write_text(path, "\n".join(kept) + "\n")
    except Exception:
        pass


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _backoff_active() -> bool:
    until = _read_json(_backoff_path()).get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return False
    return time.time() < until


def _set_backoff(seconds: float) -> None:
    try:
        path = _backoff_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"until": time.time() + seconds}))
    except OSError:
        pass


def _clear_backoff() -> None:
    try:
        _backoff_path().unlink(missing_ok=True)
    except OSError:
        pass


def _post_to(endpoint: str, payload: dict) -> bool:
    """POST one PoW-signed vote. Only the shared Worker endpoint is ever an
    allowed target - this channel has no self-hosted variant, matching
    usage.py."""
    if endpoint != config.VOTES_GATEWAY_ENDPOINT:
        log_attempt("skipped_bad_endpoint")
        return False
    import requests

    from omm.telemetry import _solve_proof_of_work

    wire = {k: v for k, v in payload.items() if v is not None}
    event_json = json.dumps(wire, sort_keys=True, separators=(",", ":"))
    timestamp_ms, nonce = _solve_proof_of_work(event_json)
    try:
        resp = requests.post(
            endpoint,
            json={"event_json": event_json, "timestamp": timestamp_ms, "nonce": nonce},
            timeout=10,
        )
    except requests.RequestException as error:
        log_attempt("send_failed_network", str(error))
        return False
    if 200 <= resp.status_code < 300:
        log_attempt("sent_ok")
        return True
    log_attempt(
        f"send_failed_http_{resp.status_code}", str(getattr(resp, "text", "") or "")
    )
    return False


def _remove_sent_rows(snapshot: list[dict]) -> None:
    """Remove exactly the rows in `snapshot`, keeping anything enqueued
    while the send was in flight. Same read-snapshot-then-diff pattern
    telemetry/usage use."""
    if not snapshot:
        return
    from omm.telemetry import _remove_sent_snapshot_entries

    path = _pending_path()
    try:
        with locked(path, timeout=10):
            current = _read_pending_unlocked(path)
            remaining = _remove_sent_snapshot_entries(
                current, snapshot, list(range(len(snapshot)))
            )
            if remaining:
                atomic_write_text(
                    path,
                    "".join(
                        json.dumps(p, sort_keys=True) + "\n"
                        for p in remaining[-_PENDING_MAX:]
                    ),
                )
            else:
                path.unlink(missing_ok=True)
    except (OSError, FileLockTimeout):
        pass


def flush_pending(force: bool = False) -> int:
    """Send every queued vote, one POST each. Returns how many were sent.

    No policy gate on "always": the queue only ever holds rows the user
    consented to (spec decision 7), so a `y`-for-this-session queue must
    still go out. A policy of "never" discards instead of sending - turning
    the channel off means earlier consent does not carry.

    Guarded by a non-blocking flush lock, so two `omm` processes racing do
    not both post the same queue and the loser never stalls a user-facing
    command. Swallows every error.
    """
    try:
        if policy() == "never":
            discard_pending()
            return 0
        path = _pending_path()
        with locked(path.with_name(f"{path.name}.flush"), timeout=0):
            rows = _read_pending()
            if not rows:
                return 0
            if not force and _backoff_active():
                return 0
            sent: list[dict] = []
            for row in rows:
                if not _post_to(config.VOTES_GATEWAY_ENDPOINT, row):
                    break
                sent.append(row)
            if sent:
                _remove_sent_rows(sent)
            if len(sent) == len(rows):
                _clear_backoff()
            else:
                _set_backoff(_BACKOFF_SECONDS)
            return len(sent)
    except Exception:
        return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_arena_upload.py -q`
Expected: PASS (all of Task 2's and Task 3's tests)

`backup_corrupt_file` is imported but only used if you add a corrupt-file
quarantine; `_read_pending_unlocked` already skips unparseable lines, which
is what the corrupt-file test asserts. If the import is unused, remove it
rather than leaving a dead name.

- [ ] **Step 5: Commit**

```bash
git add src/omm/arena_upload.py tests/test_arena_upload.py
git commit -m "feat(arena): add the vote upload queue, backoff, and flush"
```

---

### Task 4: CLI — session-end consent, flush, and `omm setting upload votes`

**Files:**
- Modify: `src/omm/cli.py` — `_arena_session` (the round loop added by
  sub-project A), the `_root` flush section (`cli.py:996` area),
  `_print_upload_policy_table` (`cli.py:8912`), `upload_menu`'s
  non-TTY hint, `_upload_channel_menu` (`cli.py:9505`), and a new
  `configure_upload_votes` beside `configure_upload_usage` (`cli.py:9036`)
- Test: `tests/test_cli_arena_upload.py`

**Interfaces:**
- Consumes: `arena_upload.policy/enqueue/pending_count/discard_pending/flush_pending`
  (Tasks 2-3); existing `_ask_upload_choice`, `_reject_conflicting_policy_flags`,
  `config_mod.update_config`, `load_config`.
- Produces: `configure_upload_votes` (the `omm setting upload votes`
  command) and `_arena_consent_and_enqueue(rows: list[dict]) -> None`.

`_arena_session` must collect the rows it wrote during the session and call
`_arena_consent_and_enqueue` once at the end — after the loop, on every exit
path out of it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_arena_upload.py`:

```python
from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import arena, arena_upload, cli, config


runner = CliRunner()


def _result(text="x"):
    return arena.GenerationResult(
        text=text, elapsed=1.0, tokens=10, tokens_per_second=10.0, memory_gb=1.0
    )


def _patch_arena(monkeypatch, pool=("alpha:latest", "beta:latest")):
    monkeypatch.setattr(cli, "_select_benchmark_engine", lambda: "ollama")
    monkeypatch.setattr(cli, "_select_benchmark_engine_for_models", lambda models: "ollama")
    monkeypatch.setattr(cli, "_ensure_engine_running", lambda *a, **k: ("ollama", None))
    monkeypatch.setattr(cli, "_stop_engine_daemon", lambda engine, handle: None)
    monkeypatch.setattr(
        cli,
        "_arena_eligible_models",
        lambda engine: {tag: f"{tag.split(':')[0]}.gguf" for tag in pool},
    )
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result())


def _one_round(monkeypatch, *, vote="a"):
    texts, votes, continues = iter(["p"]), iter([vote]), iter([False])
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: next(texts))
    monkeypatch.setattr(cli, "_ask_single_key", lambda *a, **k: next(votes))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: next(continues))


def test_declining_consent_queues_nothing(monkeypatch, isolated_omm_home):
    """Review focus 3: if `n` left rows on disk, a later
    `omm setting upload votes --enable` would ship refused votes."""
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 0
    assert not arena_upload._pending_path().exists()
    # The local record is untouched - declining upload is not declining the vote.
    assert len(arena.votes_path().read_text(encoding="utf-8").splitlines()) == 1


def test_accepting_once_queues_and_leaves_the_policy_ask(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 1
    assert config.load_config()["arena_vote_send_policy"] == "ask"


def test_accepting_always_saves_the_policy(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "always")
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 1
    assert config.load_config()["arena_vote_send_policy"] == "always"


def test_an_always_policy_queues_without_asking(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    config.update_config(arena_vote_send_policy="always")
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not ask")),
    )
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 1


def test_a_never_policy_neither_asks_nor_queues(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    config.update_config(arena_vote_send_policy="never")
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not ask")),
    )
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 0


def test_a_session_with_no_recorded_vote_does_not_ask(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: None)
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not ask")),
    )
    assert runner.invoke(cli.app, ["arena"]).exit_code == 0


def test_an_enqueue_failure_does_not_break_the_session(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    monkeypatch.setattr(
        cli.arena_upload,
        "enqueue",
        lambda rows: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert len(arena.votes_path().read_text(encoding="utf-8").splitlines()) == 1


def test_setting_upload_votes_enable_and_disable(isolated_omm_home, monkeypatch):
    enabled = runner.invoke(cli.app, ["setting", "upload", "votes", "--enable"])
    assert enabled.exit_code == 0, enabled.output
    assert config.load_config()["arena_vote_send_policy"] == "always"

    arena_upload.enqueue([_vote_row()])
    assert arena_upload.pending_count() == 1
    disabled = runner.invoke(cli.app, ["setting", "upload", "votes", "--disable"])
    assert disabled.exit_code == 0, disabled.output
    assert config.load_config()["arena_vote_send_policy"] == "never"
    # Turning the channel off discards what was queued under consent.
    assert arena_upload.pending_count() == 0
    assert "1" in disabled.output


def test_setting_upload_votes_rejects_two_flags(isolated_omm_home):
    both = runner.invoke(cli.app, ["setting", "upload", "votes", "--enable", "--disable"])
    assert both.exit_code == 1
    assert "one of" in both.output


def test_the_policy_table_lists_four_channels(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "upload"])
    assert result.exit_code == 0, result.output
    for channel in ("benchmark", "usage", "crash", "votes"):
        assert channel in result.output


def _vote_row():
    return {
        "battle_id": "0a8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a",
        "timestamp": "2026-09-26T04:05:06.700000+00:00",
        "prompt": "secret",
        "model_a": "alpha.gguf",
        "model_b": "beta.gguf",
        "engine_a": "ollama",
        "engine_b": "ollama",
        "elapsed_a": 1.0,
        "elapsed_b": 2.0,
        "tokens_a": 10,
        "tokens_b": 20,
        "memory_gb_a": None,
        "memory_gb_b": None,
        "tokens_per_second_a": None,
        "tokens_per_second_b": None,
        "watt_a": None,
        "watt_b": None,
        "winner": "a",
        "pinned": False,
    }


def test_root_flush_runs_for_an_ordinary_command(monkeypatch, isolated_omm_home):
    """The queue drains on the next `omm` run, not during a battle."""
    calls = {"n": 0}

    def _flush(*args, **kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(cli.arena_upload, "flush_pending", _flush)
    runner.invoke(cli.app, ["list"])
    assert calls["n"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_arena_upload.py -q`
Expected: FAIL — `AttributeError: module 'omm.cli' has no attribute
'arena_upload'` and `No such command 'votes'`.

- [ ] **Step 3: Write the implementation**

Add `arena_upload` to `cli.py`'s `from omm import (...)` block, immediately
after `arena`.

In `_arena_session`, collect each written row and hand them over once at the
end. Change the `arena.append_vote` branch to also remember the row:

```python
            if arena.append_vote(row):
                rounds += 1
                recorded.append(row)
            else:
```

Initialize `recorded: list[dict] = []` beside `rounds = 0`, and after the
loop — before the closing "N round(s) recorded" line — call:

```python
    _arena_consent_and_enqueue(recorded)
```

Add the helper next to `_arena_reveal`:

```python
def _arena_consent_and_enqueue(rows: list[dict]) -> None:
    """Ask once, at the end of the session, whether to upload this
    session's votes - then queue them.

    Nothing is queued before this point: the queue must never hold data the
    user has not agreed to send, so a session killed mid-way contributes
    nothing to it (its votes are still in the local votes.jsonl). The
    prompt is the same y/n/a `_ask_upload_choice` serves for benchmark
    uploads, where `a` saves the policy.

    Never raises: an upload problem must not change `omm arena`'s exit code.
    """
    if not rows:
        return
    try:
        decision = arena_upload.policy()
        if decision == "never":
            return
        if decision == "ask":
            answer = _ask_upload_choice(
                f"Upload {len(rows)} battle vote(s)? Your prompt text is never sent."
            )
            if answer == "no":
                return
            if answer == "always":
                config_mod.update_config(arena_vote_send_policy="always")
        queued = arena_upload.enqueue(rows)
        if queued and not _global_opts().quiet:
            console.print(
                f"[muted]{queued} vote(s) queued; they upload on your next "
                "omm command.[/muted]"
            )
    except typer.Exit:
        raise
    except Exception:
        # Includes a non-TTY _ask_upload_choice, a disk error, and a
        # KeyboardInterrupt-free cancel. The votes stay local.
        return
```

In `_root`'s flush section, after the `usage.flush_pending()` block:

```python
        # Arena battle votes: queued only after the user consented at the
        # end of a session, so this sends whatever is waiting.
        try:
            arena_upload.flush_pending()
        except Exception:
            pass
```

In `_print_upload_policy_table`, add:

```python
    table.add_row("votes", cfg.get("arena_vote_send_policy", "ask"))
```

In `upload_menu`'s non-TTY hint, change the channel list to
`<benchmark|usage|crash|votes>`.

In `_upload_channel_menu`, read the policy alongside the others:

```python
        votes_pol = cfg.get("arena_vote_send_policy", "ask")
```

add the choice:

```python
                    questionary.Choice(f"Battle votes (current: {votes_pol})", value="votes"),
```

and a branch mirroring the benchmark one:

```python
            elif channel == "votes":
                action = _ask_select(
                    questionary.select(
                        f"Battle vote uploads (current: {votes_pol}):",
                        choices=[
                            questionary.Choice("Always send", value="enable"),
                            questionary.Choice("Never send", value="disable"),
                            questionary.Choice("Ask after each session (default)", value="ask"),
                            questionary.Choice("← Back", value="back"),
                        ],
                    )
                )
                if action in (None, "back"):
                    continue
                configure_upload_votes(
                    enable=action == "enable",
                    disable=action == "disable",
                    ask=action == "ask",
                )
```

Add the command next to `configure_upload_usage`:

```python
@upload_app.command(name="votes")
@global_flags
def configure_upload_votes(
    enable: bool = typer.Option(False, "--enable", help="Always upload battle votes."),
    disable: bool = typer.Option(False, "--disable", help="Never upload battle votes."),
    ask: bool = typer.Option(False, "--ask", help="Ask after each battle session (the default)."),
) -> None:
    """Arena battle vote uploads (opt-in, asked after each session).

    The prompt you typed is never uploaded - only which model won, the two
    models' identities, and their measured speed and memory. See PRIVACY.md.
    Run with no flags to print the current policy.
    """
    # `_upload_channel_menu` calls this as a plain function, so an omitted
    # keyword binds to the truthy OptionInfo default. Coerce defensively,
    # exactly as configure_upload_usage does.
    if not isinstance(enable, bool):
        enable = False
    if not isinstance(disable, bool):
        disable = False
    if not isinstance(ask, bool):
        ask = False
    _reject_conflicting_policy_flags(enable, disable, ask)
    if enable:
        config_mod.update_config(arena_vote_send_policy="always")
        console.print(
            "[success]Battle vote uploads enabled.[/success] "
            "Turn off any time: `omm setting upload votes --disable`."
        )
    elif disable:
        config_mod.update_config(arena_vote_send_policy="never")
        discarded = arena_upload.discard_pending()
        console.print(
            "[success]Battle vote uploads disabled.[/success]"
            + (f" Discarded {discarded} queued vote(s)." if discarded else "")
        )
    elif ask:
        config_mod.update_config(arena_vote_send_policy="ask")
        console.print("[success]Battle votes will be asked about after each session.[/success]")
    else:
        cfg = load_config()
        console.print(f"Policy: {cfg.get('arena_vote_send_policy', 'ask')}")
        console.print(f"Queued: {arena_upload.pending_count()} vote(s)")
```

Check the real name and signature of `_reject_conflicting_policy_flags`
(`cli.py:8943` area) before relying on it, and confirm `_ask_upload_choice`
returns the strings `"yes" | "no" | "always"` as used above.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_arena_upload.py tests/test_cli_arena.py tests/test_arena.py -q`
Expected: PASS. Sub-project A's arena tests must stay green — the
session-end call is new behavior on a path they already cover.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_arena_upload.py
git commit -m "feat(cli): ask to upload arena votes and add the votes channel"
```

---

### Task 5: Worker route, validator, and RTDB rules

**Files:**
- Modify: `cf-worker/src/validate.ts` (add beside `validateUsageEvent` at
  `validate.ts:590`)
- Modify: `cf-worker/src/index.ts` (the path dispatch at `index.ts:115`, and
  the validator dispatch at `index.ts:173`)
- Modify: `cf-worker/test/validate.test.ts`
- Modify: `database.rules.json` (a `votes` block beside `usage`)
- Modify: `scripts/test_firebase_rules.mjs`

**Interfaces:**
- Consumes: `WIRE_FIELDS` from Task 2 — the TypeScript `VOTE_FIELDS` set must
  list exactly the same names.
- Produces: `validateVoteEvent(event) -> {valid, reason?}`, the `/votes`
  route, and the `votes` rules block.

- [ ] **Step 1: Write the failing tests**

Append to `cf-worker/test/validate.test.ts` (match the file's existing
`describe`/`it` style — read the top of the file first):

```typescript
function validVote(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    battle_id: "0a8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a",
    client_id: "0123456789abcdef0123456789abcdef",
    client_version: "0.3.140",
    recorded_at: "2026-09-26T04:05:06.700000+00:00",
    engine: "ollama",
    winner: "a",
    pinned: false,
    model_filename_a: "alpha-4b-Q4_K_M.gguf",
    model_filename_b: "beta-8b-Q5_K_M.gguf",
    elapsed_a: 3.25,
    elapsed_b: 9.5,
    tokens_a: 120,
    tokens_b: 300,
    ...overrides,
  };
}

describe("validateVoteEvent", () => {
  it("accepts a minimal valid vote", () => {
    expect(validateVoteEvent(validVote()).valid).toBe(true);
  });

  it("rejects a prompt key by name", () => {
    const r = validateVoteEvent(validVote({ prompt: "our internal runbook" }));
    expect(r.valid).toBe(false);
    expect(r.reason).toContain("prompt");
  });

  it("rejects any prompt-derived key", () => {
    for (const key of ["prompt_sha256", "prompt_length_bucket", "prompt_category"]) {
      expect(validateVoteEvent(validVote({ [key]: "x" })).valid).toBe(false);
    }
  });

  it("rejects an unknown field", () => {
    expect(validateVoteEvent(validVote({ some_future_field: 1 })).valid).toBe(false);
  });

  it("rejects a tie winner", () => {
    expect(validateVoteEvent(validVote({ winner: "tie" })).valid).toBe(false);
  });

  it("accepts both_bad", () => {
    expect(validateVoteEvent(validVote({ winner: "both_bad" })).valid).toBe(true);
  });

  it("rejects an unknown engine", () => {
    expect(validateVoteEvent(validVote({ engine: "llamacpp" })).valid).toBe(false);
  });

  it("rejects an asymmetric row", () => {
    const r = validateVoteEvent(validVote({ memory_gb_a: 3.4 }));
    expect(r.valid).toBe(false);
    expect(r.reason).toContain("memory_gb_b");
  });

  it("accepts a symmetric optional measurement", () => {
    expect(
      validateVoteEvent(validVote({ memory_gb_a: 3.4, memory_gb_b: 7.1 })).valid,
    ).toBe(true);
  });

  it("accepts an absent watt and rejects one out of range", () => {
    expect(validateVoteEvent(validVote()).valid).toBe(true);
    expect(
      validateVoteEvent(validVote({ watt_a: 180, watt_b: 220 })).valid,
    ).toBe(true);
    expect(
      validateVoteEvent(validVote({ watt_a: 99999, watt_b: 220 })).valid,
    ).toBe(false);
  });

  it("rejects a bad client_id", () => {
    expect(validateVoteEvent(validVote({ client_id: "NOTHEX" })).valid).toBe(false);
  });

  it("rejects a bad digest", () => {
    expect(
      validateVoteEvent(validVote({ model_digest_a: "abc", model_digest_b: "b".repeat(64) }))
        .valid,
    ).toBe(false);
  });

  it("rejects a filename with a path separator", () => {
    expect(
      validateVoteEvent(validVote({ model_filename_a: "../../etc/passwd" })).valid,
    ).toBe(false);
  });

  it("rejects an out-of-range elapsed", () => {
    expect(validateVoteEvent(validVote({ elapsed_a: 99999 })).valid).toBe(false);
  });

  it("rejects a missing required field", () => {
    const row = validVote();
    delete (row as Record<string, unknown>).winner;
    expect(validateVoteEvent(row).valid).toBe(false);
  });
});
```

Add `validateVoteEvent` to that file's import from `../src/validate`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd cf-worker && npm ci && npm test 2>&1 | tail -20`
Expected: FAIL — `validateVoteEvent is not exported` / is not a function.

- [ ] **Step 3: Write the implementation**

In `cf-worker/src/validate.ts`, after `validateUsageEvent`:

```typescript
const VOTE_SIDE_FIELDS = [
  "model_provider", "model_repo_id", "model_filename", "model_digest",
  "quant_bits", "elapsed", "tokens", "tokens_per_second", "memory_gb", "watt",
] as const;
const VOTE_FIELDS = new Set<string>([
  "schema_version", "battle_id", "client_id", "client_version",
  "recorded_at", "engine", "winner", "pinned",
  ...VOTE_SIDE_FIELDS.flatMap((n) => [`${n}_a`, `${n}_b`]),
]);
const VOTE_WINNERS = new Set(["a", "b", "both_bad"]);
const VOTE_ENGINES = new Set(["ollama", "lmstudio"]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const HEX64_RE = /^[0-9a-f]{64}$/;
// Both sides must carry the same optional measurements: sub-project C
// compares the two sides of one battle, so a half-measured row is not
// aggregatable and must be refused at ingest rather than stored and
// silently skipped later.
const VOTE_NUMERIC_RANGES: Record<string, [number, number]> = {
  elapsed: [0, 3600],
  tokens: [0, 1_000_000],
  tokens_per_second: [0, 100_000],
  memory_gb: [0, 1024],
  watt: [0, 2000],
  quant_bits: [0.5, 32],
};

export function validateVoteEvent(event: TelemetryEvent): { valid: boolean; reason?: string } {
  for (const key of Object.keys(event)) {
    if (!VOTE_FIELDS.has(key)) return { valid: false, reason: `unknown field: ${key}` };
  }
  // Named explicitly although the allow-list above already rejects it: not
  // uploading the user's prompt text is the core promise of this channel,
  // and it must be verifiable by eye and by its own test.
  if ("prompt" in event) return { valid: false, reason: "prompt must never be uploaded" };
  if (
    !hasAll(event, [
      "schema_version", "battle_id", "client_id", "recorded_at", "engine", "winner",
      "model_filename_a", "model_filename_b", "elapsed_a", "elapsed_b", "tokens_a", "tokens_b",
    ])
  ) {
    return { valid: false, reason: "missing required vote field" };
  }
  if (num(event, "schema_version") !== 1) return { valid: false, reason: "unsupported schema_version" };
  if (!UUID_RE.test(str(event, "battle_id"))) return { valid: false, reason: "invalid battle_id" };
  if (!/^[0-9a-f]{8,64}$/.test(str(event, "client_id"))) return { valid: false, reason: "invalid client_id" };
  if (!VOTE_WINNERS.has(str(event, "winner"))) return { valid: false, reason: "invalid winner" };
  if (!VOTE_ENGINES.has(str(event, "engine"))) return { valid: false, reason: "invalid engine" };
  if (has(event, "pinned") && bool(event, "pinned") === undefined) {
    return { valid: false, reason: "invalid pinned" };
  }
  if (!(str(event, "recorded_at").length >= 20 && str(event, "recorded_at").length <= 50)) {
    return { valid: false, reason: "invalid recorded_at" };
  }
  if (has(event, "client_version") && !safeStr(event, "client_version", 100)) {
    return { valid: false, reason: "invalid client_version" };
  }
  for (const name of VOTE_SIDE_FIELDS) {
    const a = `${name}_a`;
    const b = `${name}_b`;
    if (has(event, a) !== has(event, b)) {
      return { valid: false, reason: `asymmetric row: ${has(event, a) ? b : a} missing` };
    }
    if (!has(event, a)) continue;
    for (const key of [a, b]) {
      if (name === "model_provider" && !safeStr(event, key, 64)) return { valid: false, reason: `invalid ${key}` };
      if (name === "model_repo_id" && !safeStr(event, key, 512)) return { valid: false, reason: `invalid ${key}` };
      if (name === "model_filename" && !safeStr(event, key, 300)) return { valid: false, reason: `invalid ${key}` };
      if (name === "model_digest" && !HEX64_RE.test(str(event, key))) return { valid: false, reason: `invalid ${key}` };
      const range = VOTE_NUMERIC_RANGES[name];
      if (range !== undefined) {
        const value = num(event, key);
        if (!Number.isFinite(value) || value < range[0] || value > range[1]) {
          return { valid: false, reason: `invalid ${key}` };
        }
        if (name === "tokens" && !isInt(value)) return { valid: false, reason: `invalid ${key}` };
      }
    }
  }
  return { valid: true };
}
```

`safeStr` rejects path separators and control characters, which is what the
filename test asserts — confirm it does before relying on it
(`validate.ts:64`).

In `cf-worker/src/index.ts`, extend the path dispatch:

```typescript
          : url.pathname === "/votes"
            ? "votes"
            : null;
```

and the validator dispatch at `index.ts:173`, adding a `votes` branch that
calls `validateVoteEvent`.

In `database.rules.json`, add beside the `usage` block:

```json
    "votes": {
      ".read": false,
      ".write": false,
      "$vote": {
        ".validate": "newData.hasChildren(['schema_version', 'battle_id', 'client_id', 'recorded_at', 'engine', 'winner', 'model_filename_a', 'model_filename_b', 'elapsed_a', 'elapsed_b', 'tokens_a', 'tokens_b']) && !newData.child('prompt').exists() && newData.child('memory_gb_a').exists() == newData.child('memory_gb_b').exists() && newData.child('tokens_per_second_a').exists() == newData.child('tokens_per_second_b').exists() && newData.child('model_digest_a').exists() == newData.child('model_digest_b').exists() && newData.child('watt_a').exists() == newData.child('watt_b').exists()",
        "schema_version": { ".validate": "newData.isNumber() && newData.val() == 1" },
        "battle_id": { ".validate": "newData.isString() && newData.val().matches(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)" },
        "client_id": { ".validate": "newData.isString() && newData.val().matches(/^[0-9a-f]{8,64}$/)" },
        "client_version": { ".validate": "!newData.exists() || (newData.isString() && newData.val().length > 0 && newData.val().length <= 100)" },
        "recorded_at": { ".validate": "newData.isString() && newData.val().length >= 20 && newData.val().length <= 50" },
        "engine": { ".validate": "newData.val() == 'ollama' || newData.val() == 'lmstudio'" },
        "winner": { ".validate": "newData.val() == 'a' || newData.val() == 'b' || newData.val() == 'both_bad'" },
        "pinned": { ".validate": "!newData.exists() || newData.isBoolean()" },
        "$other": { ".validate": false }
      }
    },
```

Then add, inside that `$vote` block, one entry per side field, following the
`telemetry` node's style. For `a` and `b` each:

```json
        "model_provider_a": { ".validate": "!newData.exists() || (newData.isString() && newData.val().length > 0 && newData.val().length <= 64)" },
        "model_repo_id_a": { ".validate": "!newData.exists() || (newData.isString() && newData.val().length > 0 && newData.val().length <= 512)" },
        "model_filename_a": { ".validate": "newData.isString() && newData.val().length > 0 && newData.val().length <= 300 && !newData.val().contains('/') && !newData.val().contains('\\\\')" },
        "model_digest_a": { ".validate": "!newData.exists() || (newData.isString() && newData.val().matches(/^[0-9a-f]{64}$/))" },
        "quant_bits_a": { ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0.5 && newData.val() <= 32)" },
        "elapsed_a": { ".validate": "newData.isNumber() && newData.val() >= 0 && newData.val() <= 3600" },
        "tokens_a": { ".validate": "newData.isNumber() && newData.val() % 1 == 0 && newData.val() >= 0 && newData.val() <= 1000000" },
        "tokens_per_second_a": { ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0 && newData.val() <= 100000)" },
        "memory_gb_a": { ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0 && newData.val() <= 1024)" },
        "watt_a": { ".validate": "!newData.exists() || (newData.isNumber() && newData.val() >= 0 && newData.val() <= 2000)" },
```

`$other: false` means every field must have its own entry, so write both the
`_a` and the `_b` copy of each. A missing entry makes an otherwise valid row
fail, which the emulator test in Step 5 will catch.

In `scripts/test_firebase_rules.mjs`, add a block mirroring the `/usage`
one (`test_firebase_rules.mjs:123` area): a direct client write to `votes`
must be rejected with and without auth, and a direct read must be rejected
too.

- [ ] **Step 4: Run the Worker tests**

Run: `cd cf-worker && npm test 2>&1 | tail -20`
Expected: PASS, including the 15 new `validateVoteEvent` cases.

Run: `cd cf-worker && npx tsc -p tsconfig.json`
Expected: no output (clean typecheck).

- [ ] **Step 5: Run the RTDB rules emulator test**

Run:
```bash
npx --yes firebase-tools emulators:exec --only database --project demo-localfit \
  "node scripts/test_firebase_rules.mjs"
```
Expected: exits 0. Needs Java + node; if Java is unavailable, say so
explicitly in the report rather than claiming the rules were verified.

- [ ] **Step 6: Commit**

```bash
git add cf-worker/src/validate.ts cf-worker/src/index.ts cf-worker/test/validate.test.ts database.rules.json scripts/test_firebase_rules.mjs
git commit -m "feat(cf-worker): accept arena vote uploads on a private votes node"
```

---

### Task 6: Documentation sync

**Files:**
- Modify: `PRIVACY.md`
- Modify: `README.md` (`## Usage`)
- Modify: `docs/commands.json` (regenerated)
- Modify: `CLAUDE.md` (the Telemetry paragraph, which enumerates the
  channels and says "Three separate opt-in outbound channels")

- [ ] **Step 1: Add the PRIVACY.md section**

Add a section for the fourth channel in the same shape as the existing
three. It must state:

- what is sent: which model won, both models' provider/repo/filename/sha256/
  quantization, each side's elapsed time, token count, and — when measured —
  decode speed and resident memory; a random install id; the omm version.
- what is never sent: **the prompt text you typed, and nothing derived from
  it** — no hash, no length, no category. Also no model paths, no hardware
  specification, no IP-based data.
- how to control it: asked after each battle session by default;
  `omm setting upload votes --enable/--disable/--ask`.
- where it goes: the same Cloudflare Worker gateway as the other channels,
  into a Firebase RTDB node that is not publicly readable.

- [ ] **Step 2: Add the README usage line**

In `README.md`'s `## Usage` block, beside the other `omm setting upload`
lines if present, otherwise after the `omm arena` line:

```
omm setting upload votes [--enable|--disable|--ask]  # Control arena battle vote uploads (prompt text is never sent)
```

- [ ] **Step 3: Update CLAUDE.md's Telemetry paragraph**

It currently reads "Three separate opt-in outbound channels share the
`cf-worker/` PoW gateway, each its own RTDB node + `database.rules.json`
block + `validate.ts` validator: `telemetry` ... `error_reports` ... and
`usage` ...". Change it to four and add the votes channel, noting that it is
`arena_upload.py`, that its node is not world-readable, and that the prompt
text is never uploaded. Also update the sentence listing
`omm setting upload {benchmark,usage,crash}` to include `votes`.

- [ ] **Step 4: Regenerate and verify the command reference**

Run: `.venv/bin/python scripts/export_command_reference.py`
Then: `.venv/bin/python scripts/check_docs_sync.py`
Expected: exits 0. It prints an `omm.run copy is stale` warning about a
separate repository; that is not this branch's job.

- [ ] **Step 5: Run the whole suite**

Run: `FORCE_COLOR= .venv/bin/python -m pytest -q --ignore=tests/test_cli_update.py`
Expected: no failures. `tests/test_cli_update.py` is excluded because it
exercises the real `shutil.rmtree(SRC_DIR)` path and has wiped a real
`~/.omm/src` during an ordinary run.

- [ ] **Step 6: Commit**

```bash
git add PRIVACY.md README.md CLAUDE.md docs/commands.json
git commit -m "docs: document the arena vote upload channel"
```

---

## Done criteria

- `.venv/bin/python -m pytest tests/test_arena_upload.py tests/test_cli_arena_upload.py tests/test_arena.py tests/test_cli_arena.py tests/test_config.py -q` passes.
- `FORCE_COLOR= .venv/bin/python -m pytest -q --ignore=tests/test_cli_update.py` shows no failures.
- `cd cf-worker && npm ci && npm test && npx tsc -p tsconfig.json` passes.
- The Firebase emulator rules test passes, or its absence is reported
  explicitly as unverified.
- `.venv/bin/python scripts/check_docs_sync.py` exits 0.
- `PRIVACY.md`, the `omm setting upload votes` help text, `validate.ts`, and
  `database.rules.json` all agree on the field list.
- Nothing pushed. Pushing is a separate explicit ask.
