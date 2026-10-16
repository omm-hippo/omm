# Arena public leaderboard (sub-project D, #410) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rank arena models by user votes per memory group and show that ranking in `omm arena --leaderboard` and on omm.run, with an honest "collecting" state before enough votes exist.

**Architecture:** Vote rows gain an optional `memory_kind`/`memory_bucket` pair classified by a new pure module `omm/memory_group.py`. The nightly aggregator scores the overall corpus and every group with the existing scorer, gates each scope separately, merges with the last published board so a ranked scope is never blanked, and always publishes a signed v2 artifact (`status: ranked | collecting`). Two readers verify the Ed25519 signature before showing anything: a new `omm/arena_board.py` behind the CLI flag, and a request-time fetcher on the omm.run Worker.

**Tech Stack:** Python 3.10+ / Typer / requests / cryptography (omm); TypeScript Cloudflare Worker + vitest (`cf-worker/`); Firebase RTDB rules + emulator test; Next 16 + OpenNext on Cloudflare, `tsx --test`, WebCrypto Ed25519 (omm-site).

**Spec:** `docs/superpowers/specs/2026-10-16-arena-public-leaderboard-design.md`
**Approved mockups (placeholder numbers only):** `docs/superpowers/specs/assets/2026-10-16-arena-leaderboard-web-mockup-v4.html`, `docs/superpowers/specs/assets/2026-10-16-arena-leaderboard-cli-mockup-v2.html`

## Global Constraints

- Two repos. Tasks 1–8 run in `/Users/shinmingyu/Project/Localfit` on branch `feat/arena-leaderboard` (off `beta`). Tasks 9–11 run in `/Users/shinmingyu/Project/omm-site` (repo `omm-hippo/omm.run`) on a new branch `feat/arena-leaderboard` off its `main`.
- Commit freely; **never push, never deploy, never open a PR without the user's explicit ask** (Task 3 and Task 12 stop and ask).
- `git add <own files>` only; re-check `git log -5` / `git status` before every commit (concurrent sessions). The pre-commit hook bumps the patch version; that is expected.
- The prompt text is never uploaded or derived from (B's promise). `validateVoteEvent` keeps rejecting `prompt` by name.
- Vote row `schema_version` stays **1**; a row without the memory pair stays valid and counts toward overall only. Artifact `schema_version` becomes **2**.
- No blended score. Quality tier is the primary sort; efficiency only orders inside a tier and only among models whose rating is comparable.
- `raw_median_tok_s_per_gb` is never a column or sort key; only the web drawer shows it, labelled "참고: 측정 기기 중앙값" (en: "Reference: median of measuring machines").
- Both readers show rankings only from an artifact whose signature verified with `catalog_public_key` = `p8uo6GFXDcg8Rp7/t8GGl5hwPsXhObY5vI1sll5KpaI=`.
- Readers fetch from `https://raw.githubusercontent.com/omm-hippo/omm/main/published/arena-leaderboard.json` and `.../arena-leaderboard.manifest.json`. A 404 means "not published yet", not an error.
- Scoring math stays in `scripts/`; `src/omm/` never imports `scripts/`.
- Do not hoist `requests`/`questionary` imports to module scope in `cli.py` or `arena_board.py` (startup time).
- CLI copy is English. Site copy: English on the default locale, Korean on `/ko`, Korean starting from the mockup wording.
- Site: obey `design/DIRECTION.md` — dark only, zero-chroma neutrals, single accent `#FFB000`, radius ≤ 8px, no shadows/glass, no emoji, no 3-column icon-card grids, every number real. Read `node_modules/next/dist/docs/` before writing Next code (repo AGENTS.md).

## Calls made while planning (not in the spec — surface to the user)

1. **Wire bucket values are ASCII.** The spec's `≤6` / `≤8` labels are sent as `0-6` / `0-8`; displays still print `≤6GB`. Unified `8` keeps `8` (no Apple Silicon has less).
2. **Efficiency comparability = the scope's component 0** (its largest efficiency component), for both within-tier ordering and display. The spec said "the tier's largest component" for ordering; using one component for the whole scope keeps the efficiency column comparable across tiers, which "the tier's largest" does not. Normally the two coincide.
3. **A non-installable numbered row** is recorded in the session cache as `arena-unsourced:<filename>`; `_resolve_ref` turns that into a plain error for every command, and the session cache keeps it out of Tab-completion.
4. **"Changed" ignores `generated_at`.** A quiet night (same content) writes nothing for ranked and collecting files alike; each scope carries its own `updated_at`, preserved when its content did not change.
5. **A schema-1 incumbent** (none exists today) is never overwritten by a collecting file.
6. **Group gate thresholds:** 100 effective votes, 3 ranked models, largest client share ≤ 0.5.

## Review Focus

1. **Memory reading of 0 or missing** (sandboxed psutil returns `ram_total_gb=0.0`; a GPU reports `vram_total_gb=0`) → the row carries no group, never `cpu:0-8`. Test in Task 1.
2. **A future artifact `schema_version` 3** reaching an old client → plain "update omm to read this leaderboard" message, no crash, no ranking. Tests in Task 7 (CLI) and Task 9 (site).
3. **Offline after one successful run** → the CLI shows the cached board only if the cached bytes still verify; a tampered cache shows no ranking. Test in Task 7.
4. **A group key in the artifact the reader does not know** (bucket table changed later) → listed with its raw key as label, never a crash. Tests in Task 7 and Task 10.
5. **A board model whose sha256 matches an installed model under a different filename** → shown as installed, numbered ref is the registry filename. Test in Task 7.

---

## Part 1 — memory group on the vote row (B change)

### Task 1: `omm/memory_group.py` — classification and bucket table

**Files:**
- Create: `src/omm/memory_group.py`
- Test: `tests/test_memory_group.py`

**Interfaces:**
- Produces:
  - `KINDS = ("unified", "vram", "cpu")`
  - `BUCKET_LABELS: dict[str, tuple[str, ...]]` — `{"unified": ("8","16","18-24","32-36","48-64","96-128","192+"), "vram": ("0-6","8","10-12","16","20-24","32+"), "cpu": ("0-8","16","32","64+")}`
  - `bucket_for(kind: str, reading_gib: float) -> str | None`
  - `classify(hw: HardwareInfo, *, system: str | None = None) -> tuple[str, str] | None` (`system` defaults to `platform.system()`)
  - `is_valid(kind: object, bucket: object) -> bool`
  - `display_label(kind: str, bucket: str) -> str` — e.g. `"Mac · unified memory 16GB"`, `"PC · GPU VRAM 20–24GB"`, `"PC · GPU VRAM ≤6GB"`, `"CPU only · RAM 64GB+"`. Unknown pair → `f"{kind}:{bucket}"`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from omm import memory_group as mg
from omm.hardware import HardwareInfo

def _hw(*, unified=False, ram=16.0, vram=None):
    return HardwareInfo(os_name="x", os_version="x", cpu="x", ram_total_gb=ram,
                        ram_available_gb=ram, unified_memory=unified, gpu_name=None,
                        vram_total_gb=vram, vram_free_gb=None)

@pytest.mark.parametrize("gib,label", [
    (8, "8"), (7.9, "8"), (16, "16"), (15.8, "16"), (18, "18-24"), (24, "18-24"),
    (32, "32-36"), (36, "32-36"), (48, "48-64"), (64, "48-64"), (96, "96-128"),
    (128, "96-128"), (192, "192+")])
def test_unified_marketed_sizes(gib, label):
    assert mg.bucket_for("unified", gib) == label

@pytest.mark.parametrize("gib,label", [
    (4, "0-6"), (6, "0-6"), (7.98, "8"), (8, "8"), (10, "10-12"), (12, "10-12"),
    (16, "16"), (15.99, "16"), (20, "20-24"), (23.99, "20-24"), (24, "20-24"),
    (32, "32+"), (48, "32+")])
def test_vram_marketed_sizes(gib, label):
    assert mg.bucket_for("vram", gib) == label

@pytest.mark.parametrize("gib,label", [(8, "0-8"), (16, "16"), (32, "32"), (31.6, "32"), (64, "64+")])
def test_cpu_marketed_sizes(gib, label):
    assert mg.bucket_for("cpu", gib) == label

def test_apple_silicon_groups_by_ram():
    assert mg.classify(_hw(unified=True, ram=15.8, vram=15.8), system="Darwin") == ("unified", "16")

def test_pc_with_gpu_groups_by_vram_on_linux_and_windows():
    assert mg.classify(_hw(ram=64, vram=23.99), system="Linux") == ("vram", "20-24")
    assert mg.classify(_hw(ram=64, vram=7.98), system="Windows") == ("vram", "8")

def test_no_gpu_is_cpu_only_where_detection_is_trustworthy():
    assert mg.classify(_hw(ram=31.6), system="Windows") == ("cpu", "32")
    assert mg.classify(_hw(ram=16), system="Darwin") == ("cpu", "16")  # Intel Mac, no NVML

def test_linux_without_detected_gpu_sends_no_group():
    assert mg.classify(_hw(ram=32), system="Linux") is None

@pytest.mark.parametrize("hw,system,expected", [
    (_hw(unified=True, ram=0.0, vram=0.0), "Darwin", None),   # sandboxed psutil
    (_hw(ram=0.0), "Windows", None),
    (_hw(ram=float("nan")), "Windows", None),
    (_hw(ram=32, vram=0.0), "Windows", ("cpu", "32")),         # 0 VRAM = no usable GPU
    (_hw(ram=32), "FreeBSD", None)])
def test_unusable_readings_send_no_group(hw, system, expected):
    assert mg.classify(hw, system=system) == expected

def test_is_valid_rejects_cross_kind_buckets():
    assert mg.is_valid("vram", "8") and not mg.is_valid("unified", "0-6")
    assert not mg.is_valid("gpu", "8") and not mg.is_valid("cpu", None)
```

- [ ] **Step 2: Run `python -m pytest tests/test_memory_group.py -q`** — expect FAIL (`No module named 'omm.memory_group'`).

- [ ] **Step 3: Implement `src/omm/memory_group.py`**

Round first with `math.floor(reading + 0.5)` (not `round()`, which is banker's rounding), then take the first bucket whose upper bound ≥ the rounded value; the last label is the overflow. Upper bounds: unified `8,16,24,36,64,128` → `192+`; vram `6,8,12,16,24` → `32+`; cpu `8,16,32` → `64+`. A non-finite or ≤ 0 reading returns None. `classify`: `unified_memory` → unified by `ram_total_gb`; else usable `vram_total_gb` (> 0, finite) → vram; else on `Darwin`/`Windows` → cpu by RAM; anything else → None. Pure stdlib + the `HardwareInfo` type only (scripts import this module). Module docstring: why marketed sizes and not `usage.py`'s half-open buckets (spec Part 1).

- [ ] **Step 4: Run `python -m pytest tests/test_memory_group.py -q`** — expect PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/memory_group.py tests/test_memory_group.py
git commit -m "feat(arena): classify a machine into a marketed-size memory group"
```

### Task 2: Send the pair; validator, rules, privacy text

**Files:**
- Modify: `src/omm/arena_upload.py` (`WIRE_FIELDS`, `build_payload`, `enqueue`)
- Modify: `src/omm/cli.py` (`_arena_consent_and_enqueue` prompt text)
- Modify: `cf-worker/src/validate.ts` (`VOTE_FIELDS`, new `VOTE_MEMORY_BUCKETS`, `validateVoteEvent`)
- Modify: `database.rules.json` (`votes/$vote`)
- Modify: `PRIVACY.md` §4, `CLAUDE.md` Telemetry paragraph (votes field list)
- Test: `tests/test_arena_upload.py`, `tests/test_cli_arena_upload.py`, `tests/test_vote_wire_contract.py` (new), `cf-worker/test/validate.test.ts`, `scripts/test_firebase_rules.mjs`

**Interfaces:**
- Consumes: `memory_group.classify`, `memory_group.BUCKET_LABELS`, `memory_group.display_label` (Task 1).
- Produces: wire fields `memory_kind`, `memory_bucket`; `build_payload(vote_row, *, registry_entries=None, memory_group: tuple[str, str] | None = None) -> dict | None`; `arena_upload.current_memory_group() -> tuple[str, str] | None` (never raises; `hardware.last_scan() or hardware.scan_hardware()` then `classify`).

- [ ] **Step 1: Write the failing Python tests**

```python
# tests/test_arena_upload.py
def test_payload_carries_the_memory_group_pair(isolated_omm_home):
    p = arena_upload.build_payload(_local_row(), registry_entries=_registry(),
                                   memory_group=("unified", "16"))
    assert p["memory_kind"] == "unified" and p["memory_bucket"] == "16"
    assert set(p) <= arena_upload.WIRE_FIELDS

def test_payload_without_a_group_omits_both_keys(isolated_omm_home):
    p = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert "memory_kind" not in p and "memory_bucket" not in p

def test_an_invalid_group_is_dropped_not_sent(isolated_omm_home):
    p = arena_upload.build_payload(_local_row(), registry_entries=_registry(),
                                   memory_group=("unified", "0-6"))
    assert "memory_kind" not in p and "memory_bucket" not in p

def test_enqueue_classifies_once_and_stamps_every_row(isolated_omm_home, monkeypatch):
    calls = []
    monkeypatch.setattr(arena_upload, "current_memory_group",
                        lambda: calls.append(1) or ("vram", "8"))
    config.update_config(arena_vote_send_policy="always")
    rows = [_local_row(), _local_row(battle_id="1b8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a")]
    assert arena_upload.enqueue(rows) == 2
    assert len(calls) == 1
    assert all(r["memory_kind"] == "vram" for r in arena_upload._read_pending())

def test_a_hardware_scan_failure_still_queues_the_votes(isolated_omm_home, monkeypatch):
    monkeypatch.setattr("omm.hardware.scan_hardware", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr("omm.hardware.last_scan", lambda: None)
    # enqueue returns the row count; rows have no memory_kind
```

```python
# tests/test_vote_wire_contract.py — the 3-way check
def test_rules_keys_equal_the_python_wire_fields():
    keys = set(_rules()["votes"]["$vote"]) - {".validate", "$other"}
    assert keys == arena_upload.WIRE_FIELDS

def test_validate_ts_declares_every_bucket_label():
    text = (ROOT / "cf-worker/src/validate.ts").read_text()
    block = re.search(r"VOTE_MEMORY_BUCKETS[^{]*\{(.*?)\};", text, re.S).group(1)
    for kind, labels in memory_group.BUCKET_LABELS.items():
        found = re.search(rf"{kind}:\s*new Set\(\[(.*?)\]\)", block, re.S).group(1)
        assert sorted(re.findall(r'"([^"]+)"', found)) == sorted(labels)

def test_rules_declare_every_bucket_label():
    rule = _rules()["votes"]["$vote"]["memory_bucket"][".validate"]
    for kind, labels in memory_group.BUCKET_LABELS.items():
        for label in labels:
            assert f"'{label}'" in rule
    assert "memory_kind" in _rules()["votes"]["$vote"][".validate"]
```

```python
# tests/test_cli_arena_upload.py — consent prompt names the group
# with arena_upload.current_memory_group -> ("unified","16") and policy "ask",
# the prompt text passed to _ask_upload_choice equals:
"Upload 2 battle vote(s) and this machine's memory group (Mac · unified memory 16GB)? Your prompt text is never sent."
# and with current_memory_group -> None it stays the old text:
"Upload 2 battle vote(s)? Your prompt text is never sent."
```

- [ ] **Step 2: Run** `python -m pytest tests/test_arena_upload.py tests/test_vote_wire_contract.py tests/test_cli_arena_upload.py -q` — expect the new tests FAIL.

- [ ] **Step 3: Implement the Python side**

`WIRE_FIELDS` gains `memory_kind`, `memory_bucket`. `build_payload` adds the pair only when `memory_group.is_valid(*pair)`. `enqueue` calls `current_memory_group()` once per call and passes it to every `build_payload`. `_arena_consent_and_enqueue` calls `arena_upload.current_memory_group()` before asking and uses the exact texts above. Do not change the local `votes.jsonl` row.

- [ ] **Step 4: Write the failing Worker + rules tests**

`cf-worker/test/validate.test.ts`: accepts a vote with `memory_kind:"unified", memory_bucket:"16"`; accepts one without either; rejects only `memory_kind`; rejects only `memory_bucket`; rejects `memory_kind:"vram", memory_bucket:"18-24"` (cross-kind); rejects `memory_kind:"gpu"`.
`scripts/test_firebase_rules.mjs`: the authenticated `votes` POST with the pair is accepted by `.validate`; one with only `memory_kind` is rejected (mirror the existing `voteFixed` pattern).

- [ ] **Step 5: Implement `validate.ts` and `database.rules.json`**

`validate.ts`: add both names to `VOTE_FIELDS`; add
`const VOTE_MEMORY_BUCKETS: Record<string, Set<string>> = { unified: new Set([...]), vram: new Set([...]), cpu: new Set([...]) };` with Task 1's labels; in `validateVoteEvent`, `has(memory_kind) !== has(memory_bucket)` → `"asymmetric row: memory group"`, otherwise when present the bucket must be in `VOTE_MEMORY_BUCKETS[kind]` → else `"invalid memory group"`.
Rules: append `&& newData.child('memory_kind').exists() == newData.child('memory_bucket').exists()` to `$vote/.validate`; `memory_kind/.validate`: `newData.val() == 'unified' || 'vram' || 'cpu'`; `memory_bucket/.validate`: one OR-clause per kind, `(newData.parent().child('memory_kind').val() == 'unified' && (newData.val() == '8' || ...))`.

- [ ] **Step 6: Run all of these and expect PASS**

```bash
python -m pytest tests/test_arena_upload.py tests/test_vote_wire_contract.py tests/test_cli_arena_upload.py tests/test_database_rules.py -q
cd cf-worker && npm ci && npm test && npx tsc -p tsconfig.json && cd ..
JAVA_HOME=/opt/homebrew/opt/openjdk npx --yes firebase-tools emulators:exec --only database --project demo-localfit "node scripts/test_firebase_rules.mjs"
```

- [ ] **Step 7: Update `PRIVACY.md` §4 and CLAUDE.md**

PRIVACY §4 "what is sent" list gains: "this machine's memory group — Mac unified-memory size, PC GPU VRAM size, or RAM size on a machine with no GPU — as a marketed-size bucket such as `16GB` (never the exact amount, GPU model or CPU). Linux machines whose GPU omm cannot detect send no group." Note that users already on "always" are not asked again (spec decision 7). CLAUDE.md Telemetry paragraph: add "a coarse memory group" to the `votes` field list.

- [ ] **Step 8: Commit**

```bash
git add src/omm/arena_upload.py src/omm/cli.py cf-worker/src/validate.ts cf-worker/test/validate.test.ts database.rules.json scripts/test_firebase_rules.mjs PRIVACY.md CLAUDE.md tests/test_arena_upload.py tests/test_cli_arena_upload.py tests/test_vote_wire_contract.py
git commit -m "feat(arena): send a coarse memory group with each vote"
```

### Task 3: Deploy Worker + rules, verify one live row (outward — ask first)

No code. Must finish before the client change is pushed anywhere. If the user defers the deploy, continue with Task 4 and come back to this task before Task 12's push.

- [ ] **Step 1: Stop and ask the user** for permission to deploy the Worker and the RTDB rules from this branch. Show the diff that will ship (`git diff origin/beta -- cf-worker/src database.rules.json`).
- [ ] **Step 2: Diff live rules against the file** (live rules readable at `/.settings/rules.json` with the firebase CLI token); any unexpected drift → stop and report.
- [ ] **Step 3: Deploy**

```bash
npx firebase-tools deploy --only database --project localfit-8ab57
cd cf-worker && CLOUDFLARE_ACCOUNT_ID=370e521d89a70d895de8448a36449b93 npx wrangler deploy && cd ..
```

- [ ] **Step 4: Live check** with a scratchpad script modelled on B's: build a payload via `arena_upload.build_payload(row, registry_entries={}, memory_group=("unified","16"))`, `_post_to` it → `sent`; a payload with only `memory_kind` → `drop` (HTTP 400); one without the pair → `sent`. Then delete both test rows from `/votes` (they are fake votes) and confirm the node is back to its prior count.
- [ ] **Step 5: Record** the Worker version id and the result in the memory file `project_omm_arena_subproject_d_brainstorm_2026-10-16.md`.

---

## Part 2 — aggregation: groups, pairs, status file (C change)

### Task 4: Score one scope; fix within-tier order; pairwise counts

**Files:**
- Modify: `scripts/arena_score.py`
- Test: `tests/test_arena_score.py`

**Interfaces:**
- Produces:
  - `score_scope(rows: list[dict], *, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> dict` with keys `corpus, models, tiers, efficiency_components, provisional_models, pairs` (today's `build_artifact` body minus `schema_version`/`generated_at`, plus `pairs`).
  - Each model entry gains `efficiency_comparable: bool` (= `efficiency is not None and efficiency["component"] == 0`).
  - `pair_counts(battles: list[Battle], weights: list[float]) -> list[dict]`, each `{"a": str, "b": str, "wins_a": float, "wins_b": float, "both_bad": float, "battles": int}` with `a < b`, sorted by `(a, b)`; weighted counts use `battle_weights`, `battles` is the raw count.

- [ ] **Step 1: Write the failing tests**

```python
def test_within_tier_order_never_compares_ratings_across_components():
    # tier 1 = {m1, m2, m3}; m1/m2 measured against each other (component 0),
    # m3 only against m4 (component 1) with a huge rating.
    scope = arena_score.score_scope(rows, resamples=20)
    tier1 = [m["key"] for m in scope["models"] if m["quality"]["tier"] == 1]
    assert tier1.index("<m3>") > max(tier1.index("<m1>"), tier1.index("<m2>"))
    m3 = next(m for m in scope["models"] if m["key"] == "<m3>")
    assert m3["efficiency_comparable"] is False

def test_models_outside_component_0_follow_by_quality_strength():
    ...  # two non-comparable models in one tier: higher strength first

def test_pair_counts_sum_to_the_scope():
    scope = arena_score.score_scope(rows, resamples=20)
    assert sum(p["battles"] for p in scope["pairs"]) == scope["corpus"]["rows_used"]
    assert sum(p["wins_a"] + p["wins_b"] + p["both_bad"] for p in scope["pairs"]) == \
        pytest.approx(scope["corpus"]["effective_votes"])
    assert all(p["a"] < p["b"] for p in scope["pairs"])

def test_pair_counts_respect_the_client_pair_cap():
    # one client, 30 wins of X over Y -> wins sum to CLIENT_PAIR_VOTE_CAP, battles == 30
```

- [ ] **Step 2: Run** `python -m pytest tests/test_arena_score.py -q` — new tests FAIL.
- [ ] **Step 3: Implement.** Extract `score_scope`; `build_artifact` temporarily returns `{"schema_version": ARTIFACT_SCHEMA_VERSION, "generated_at": ..., **score_scope(...)}` so existing tests keep passing. New `order()` key: `(tier or inf, 0 if comparable else 1, -rating if comparable else -strength, key)`. Update the module comment that described the old order.
- [ ] **Step 4: Run** `python -m pytest tests/test_arena_score.py -q` — PASS.
- [ ] **Step 5: Commit** `git add scripts/arena_score.py tests/test_arena_score.py && git commit -m "fix(arena): order within a tier only by comparable efficiency; add pair counts"`

### Task 5: Groups and artifact v2

**Files:**
- Modify: `scripts/arena_score.py` (`build_artifact`, `ARTIFACT_SCHEMA_VERSION = 2`, new `group_key`)
- Test: `tests/test_arena_score.py`

**Interfaces:**
- Consumes: `omm.memory_group.is_valid` (Task 1; update the module docstring: stdlib plus omm's pure `memory_group` table), `score_scope` (Task 4).
- Produces:
  - `group_key(row: dict) -> str | None` → `"unified:16"` etc., None when absent/invalid.
  - `build_artifact(rows, *, generated_at, resamples, seed) -> dict` = `{"schema_version": 2, "generated_at": str, **score_scope(all rows), "groups": [{"key", "memory_kind", "memory_bucket", **score_scope(group rows)}, ...]}`, groups sorted by key, one per key that has ≥ 1 row. No `status`/`ranked` here — the gate decides (Task 6).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_group_scores_exactly_like_the_scorer_on_its_own_rows():
    art = arena_score.build_artifact(rows, generated_at="t", resamples=20)
    group = next(g for g in art["groups"] if g["key"] == "unified:16")
    alone = arena_score.score_scope([r for r in rows if arena_score.group_key(r) == "unified:16"], resamples=20)
    assert {k: group[k] for k in alone} == alone

def test_rows_without_a_group_count_only_in_overall():
    assert art["corpus"]["rows_used"] == len(rows)
    assert sum(g["corpus"]["rows_fetched"] for g in art["groups"]) == len(grouped_rows)

def test_an_invalid_pair_is_ungrouped_not_dropped():
    # memory_kind="vram", memory_bucket="18-24" -> overall only
def test_schema_version_is_2(): ...
def test_unchanged_corpus_is_byte_identical(): ...  # json.dumps(sort_keys) of two runs equal
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `python -m pytest tests/test_arena_score.py -q` — PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(arena): score every memory group alongside the overall board"` (add the two files).

### Task 6: Per-scope gate, keep-last-good merge, always-signed status file

**Files:**
- Modify: `scripts/arena_quality_gate.py`, `scripts/aggregate_arena_votes.py`, `.github/workflows/arena-aggregate.yml`
- Test: `tests/test_arena_quality_gate.py`, `tests/test_aggregate_arena_votes.py`, `tests/test_arena_aggregate_workflow.py`

**Interfaces:**
- Consumes: v2 `build_artifact` output (Task 5).
- Produces:
  - Constants `MIN_GROUP_EFFECTIVE_VOTES = 100`, `MIN_GROUP_RANKED_MODELS = 3` (share limit reuses `MAX_SINGLE_CLIENT_SHARE`).
  - `evaluate(scope: dict, incumbent_scope: dict | None = None, *, min_votes: float = MIN_EFFECTIVE_VOTES, min_ranked: int = MIN_RANKED_MODELS) -> GateResult` (existing callers unchanged).
  - `publish(fresh: dict, incumbent: dict | None, *, gated: bool = True) -> tuple[dict, dict[str, GateResult]]` → the artifact to write, plus gate results keyed `"overall"` / group key. Every scope in the output (top level and each group) carries `ranked: bool`, `reasons: list[str]`, `updated_at: str | None`; an unranked scope keeps `corpus` but has empty `models`, `tiers`, `efficiency_components`, `provisional_models`, `pairs`. Top-level `status` = `"ranked"` if any scope is ranked, else `"collecting"`.
  - Merge rule per scope: passed → fresh scope, `updated_at` = incumbent scope's `updated_at` if that scope was ranked and its content (minus `updated_at`, `reasons`, `ranked`) is equal, else `fresh["generated_at"]`. Blocked + incumbent scope ranked → incumbent scope verbatim. Blocked otherwise → collecting scope with `reasons` = the gate's reasons. Group set = fresh groups ∪ incumbent's ranked groups. `gated=False` treats every scope as passed (local preview only). An incumbent whose `schema_version != 2` counts as no incumbent here.
  - `aggregate_arena_votes.py`: outputs `status` and `artifact_changed`; `gate_status` is removed. It writes only when the serialized artifact differs from the file on disk **ignoring top-level `generated_at`**, and never writes a `collecting` result over an existing file whose `schema_version != 2`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_arena_quality_gate.py
def test_a_group_dominated_by_one_client_is_blocked():
    r = gate.evaluate(group_scope(share=0.8, votes=150, ranked=4),
                      min_votes=gate.MIN_GROUP_EFFECTIVE_VOTES, min_ranked=gate.MIN_GROUP_RANKED_MODELS)
    assert r.status == "blocked" and any("80%" in x for x in r.reasons)

def test_a_group_can_rank_while_overall_collects():
    out, results = gate.publish(fresh_with(overall=thin, groups={"unified:16": healthy}), None)
    assert out["status"] == "ranked" and out["ranked"] is False and out["models"] == []
    assert next(g for g in out["groups"] if g["key"] == "unified:16")["ranked"] is True

def test_collecting_never_overwrites_a_ranked_scope():
    out, _ = gate.publish(fresh_with(overall=thin), incumbent=ranked_incumbent)
    assert out["ranked"] is True and out["models"] == ranked_incumbent["models"]

def test_a_ranked_model_collapse_keeps_the_previous_board():
    # incumbent 10 ranked, fresh 5 -> blocked by MAX_RANKED_MODEL_DROP -> incumbent kept, page not blank

def test_a_newly_passing_group_publishes_while_another_is_blocked(): ...
def test_unchanged_content_keeps_updated_at(): ...
def test_nothing_ranked_ever_gives_a_collecting_file_with_counts_and_reasons():
    out, _ = gate.publish(fresh_with(overall=thin), None)
    assert out["status"] == "collecting" and out["corpus"]["client_count"] == 4 and out["reasons"]
```

```python
# tests/test_aggregate_arena_votes.py — replace test_a_blocked_run_exits_zero_and_writes_nothing
def test_a_first_blocked_run_writes_a_collecting_file(tmp_path): ...  # status collecting, outputs artifact_changed=true
def test_a_quiet_night_writes_nothing(tmp_path):
    # run twice with different --generated-at on the same corpus -> second run artifact_changed=false, file bytes unchanged
def test_a_collecting_result_never_replaces_a_schema_1_file(tmp_path): ...
def test_the_written_collecting_file_verifies_against_its_signature(tmp_path): ...  # mirror the existing signed test
```

```python
# tests/test_arena_aggregate_workflow.py
assert "steps.aggregate.outputs.artifact_changed == 'true'" in text
assert "gate_status" not in text
```

- [ ] **Step 2: Run** `python -m pytest tests/test_arena_quality_gate.py tests/test_aggregate_arena_votes.py tests/test_arena_aggregate_workflow.py -q` — FAIL.
- [ ] **Step 3: Implement** `evaluate` kwargs, `publish`, the aggregator change (print one line per scope: key, ranked/collecting, reasons), and the workflow: sign and PR steps gate on `artifact_changed == 'true'` only. Update the PR body's 배경 to say a run with too little data publishes a "collecting" file with real counts instead of skipping, and that a board already published is never replaced by one.
- [ ] **Step 4: Run** the same three test files plus `python -m pytest tests/test_arena_score.py -q` — PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(arena): gate each scope and publish a signed collecting status"` (add the six files).

---

## Part 3 — `omm arena --leaderboard`

### Task 7: `omm/arena_board.py` — fetch, verify, cache, select, number

**Files:**
- Create: `src/omm/arena_board.py`
- Modify: `src/omm/config.py` (two URL constants next to `VOTES_GATEWAY_ENDPOINT`)
- Test: `tests/test_arena_board.py`

**Interfaces:**
- Consumes: `catalog.verify_signed_artifact`, `predictor._read_bounded_json_response`, `memory_group.display_label`, `search.exact_install_ref`, `registry.load_registry()` shape.
- Produces:
  - `config.ARENA_LEADERBOARD_URL`, `config.ARENA_LEADERBOARD_MANIFEST_URL` (the two raw `main/published/` URLs).
  - `SUPPORTED_SCHEMA = 2`; `UNSOURCED_REF_PREFIX = "arena-unsourced:"`; `MAX_BOARD_BYTES = 8 * 1024 * 1024`.
  - Exceptions: `BoardVerificationError`, `BoardUnavailable` (network failed and no verifying cache), `BoardTooNew` (schema > 2).
  - `fetch_board(public_key: str) -> dict | None` — None means 404 (unpublished). Live first; after a verified live fetch, write raw bytes + manifest to `config.OMM_HOME / "arena" / "leaderboard.json"` and `leaderboard.manifest.json` (resolved at call time). On a network error (not 404) re-verify and use the cache.
  - `@dataclass Selection: scope: dict, scope_key: str ("overall" or group key), scope_label: str, machine_group: tuple[str, str] | None, machine_label: str | None, fell_back: bool`
  - `select_scope(artifact: dict, machine_group: tuple[str, str] | None) -> Selection` — machine's group if present and `ranked`, else overall (`fell_back=True` when the machine had a group that was not ranked or not present).
  - `@dataclass BoardRow: number: int, model: dict, installed_filename: str | None, ref: str`
  - `numbered_rows(scope: dict, registry_entries: dict) -> list[BoardRow]` — non-provisional models in artifact order, numbered from 1 across tiers. Installed match: key `sha256:<d>` vs registry `sha256` first, then `display_filename` (case-insensitive) vs registry filename. Ref: installed → registry filename; else `search.exact_install_ref({"repo_id", "filename": display_filename, "provider"})` when `repo_id` is set; else `UNSOURCED_REF_PREFIX + display_filename`.

- [ ] **Step 1: Write the failing tests** (sign fixtures with a throwaway key from `sign_catalog.generate_keys`; mock `requests.get` via the `sys.modules` singleton)

```python
def test_a_verified_board_is_returned_and_cached(...): ...
def test_a_tampered_artifact_raises_and_shows_nothing(...):
    with pytest.raises(arena_board.BoardVerificationError): arena_board.fetch_board(pub)
def test_a_404_means_unpublished(...): assert arena_board.fetch_board(pub) is None
def test_offline_uses_a_cache_that_still_verifies(...): ...
def test_a_tampered_cache_is_not_used_offline(...):
    with pytest.raises(arena_board.BoardUnavailable): ...
def test_a_newer_schema_raises_too_new(...): ...   # schema_version 3, valid signature
def test_unranked_group_falls_back_to_overall():
    s = arena_board.select_scope(art, ("vram", "8"))
    assert s.scope_key == "overall" and s.fell_back is True
def test_an_unknown_group_key_gets_its_raw_label(): ...  # "vram:7" -> label "vram:7"
def test_numbers_run_across_tiers_and_skip_provisional(): ...
def test_installed_matches_by_sha_even_under_another_filename():
    rows = arena_board.numbered_rows(scope, {"renamed.gguf": {"sha256": "a"*64}})
    assert rows[0].installed_filename == "renamed.gguf" and rows[0].ref == "renamed.gguf"
def test_a_not_installed_model_gets_an_exact_install_ref():
    assert rows[1].ref == "org/qwen-gguf:qwen-7b-Q4_K_M.gguf"
def test_a_model_without_repo_gets_an_unsourced_ref():
    assert rows[2].ref == "arena-unsourced:local-only.gguf"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_arena_board.py -q` — FAIL.
- [ ] **Step 3: Implement** (lazy `import requests` inside functions).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(arena): fetch and verify the published leaderboard"` (add the three files).

### Task 8: The `--leaderboard` flag

**Files:**
- Modify: `src/omm/cli.py` (`arena_cmd`, new `_arena_leaderboard()`, `_resolve_ref`)
- Modify: `src/omm/session_cache.py` (`record_results` keeps `UNSOURCED_REF_PREFIX` refs out of `seen`)
- Modify: `docs/commands.json` (regenerated), `README.md` `## Usage`
- Test: `tests/test_cli_arena_leaderboard.py` (new), `tests/test_session_cache.py`

**Interfaces:**
- Consumes: everything in Task 7's Produces; `arena_upload.current_memory_group()` (Task 2); `cli._quant_label`.
- Produces: `omm arena --leaderboard [--json]`.

Rendering (copy and layout from the spec's Part 3 block; numbers only from the artifact):
- Header: `Arena leaderboard · <scope label>` + ` (this machine)` when the scope is the machine's group. Line 2: `<rows_used> votes from <client_count> machines · updated <updated_at, "%Y-%m-%d %H:%M UTC"> · signature verified`.
- Fallback line (before the header) when `fell_back` and the machine has a group: `Your group (<machine label>) needs more votes — showing all machines.`
- Tier blocks `TIER n`; columns: `[n]` name (filename stem without the quant suffix), quant (`_quant_label`), quality `strength±half-width` (2 decimals; half-width = `(ci_high - ci_low)/2`), efficiency `exp(rating)` as `1.4×` when `efficiency_comparable` else `—`, battles (raw), trailing `installed` and/or `! often fails` (`quality_warning`).
- Footnotes: `—` footnote `— measured on a disjoint set of battles, not comparable` only if some row printed `—` with non-null efficiency; `N more models need more votes. Full board: https://omm.run/leaderboard` when provisional > 0.
- `Next:` block, each line only when it applies: `omm install <n>` (first numbered row not installed and not unsourced), `omm arena <n> <m>` (first two installed numbered rows), `omm info <n>` (first numbered row not unsourced).
- `status == "collecting"`: `Arena leaderboard · collecting votes`, `<rows_used> votes from <client_count> machines so far — more votes are needed before ranking starts.`, the machine group's own counts if present, then `Vote with: omm arena`. No numbers, session cache untouched.
- Unpublished (None): `The arena leaderboard hasn't been published yet. Vote with: omm arena` — exit 0.
- `BoardVerificationError` / `BoardUnavailable` / `BoardTooNew`: `errors.print_cli_error`, exit 1, no ranking (`BoardTooNew` fix line: `Run \`omm update\`.`).
- `--json` always prints `{"status": "ranked"|"collecting"|"unpublished", "machine_group": {"key","memory_kind","memory_bucket","label"}|null, "scope": {"key","label","ranked","fell_back"}|null, "generated_at": str|null, "corpus": dict|null, "models": [{"number": int|null, "ref": str|null, "installed": bool, ...artifact model fields}], "provisional_count": int}`.

- [ ] **Step 1: Write the failing tests** (stub `arena_board.fetch_board`, `arena_upload.current_memory_group`, `registry.load_registry`, `session_cache` path)

```python
def test_leaderboard_cannot_be_combined_with_models():
    r = runner.invoke(cli.app, ["arena", "--leaderboard", "a.gguf", "b.gguf"]); assert r.exit_code == 1
    assert "--leaderboard" in r.output
def test_leaderboard_cannot_be_combined_with_keep(): ...
def test_leaderboard_never_starts_an_engine(monkeypatch):
    monkeypatch.setattr(cli, "_select_benchmark_engine", lambda: pytest.fail("engine touched"))
def test_ranked_group_output(): ...        # header "(this machine)", TIER 1, "[1]", "1.4×", "installed", "Next:"
def test_unranked_group_falls_back_with_one_line(): ...
def test_collecting_shows_real_counts_and_no_numbers(): ...
def test_unpublished_exits_zero(): ...
def test_verification_failure_shows_no_ranking():
    assert r.exit_code == 1 and "TIER" not in r.output and "[1]" not in r.output
def test_json_shape_is_identical_across_states():
    shapes = {frozenset(json.loads(run(state).output)) for state in ("ranked","fallback","collecting","unpublished")}
    assert len(shapes) == 1
def test_numbered_refs_feed_install_and_arena(monkeypatch):
    # after --leaderboard, cli._resolve_ref("2") == exact install ref,
    # and cli._arena_normalize_refs(["1","3"], pool) maps to runtime refs of installed rows
def test_an_unsourced_number_errors_plainly():
    # _resolve_ref("3") -> exit 1, message contains "can't be installed from the leaderboard"
def test_no_installed_pair_drops_the_arena_hint(): ...
```

`tests/test_session_cache.py`: `record_results(["a", "arena-unsourced:x.gguf"])` → `load_last_results()` has both, `load_seen()` lacks the unsourced one.

- [ ] **Step 2: Run** `python -m pytest tests/test_cli_arena_leaderboard.py tests/test_session_cache.py -q` — FAIL.
- [ ] **Step 3: Implement.** The `--leaderboard` branch runs before arity validation and engine selection. `_resolve_ref`: when the looked-up ref starts with `arena_board.UNSOURCED_REF_PREFIX`, print `#<n> (<filename>) was ranked from votes with no source repo, so it can't be installed from the leaderboard.` — fatal → exit 1, non-fatal → warn and return None. Update `arena_cmd`'s docstring with one sentence on `--leaderboard`.
- [ ] **Step 4: Docs trio**

```bash
python scripts/export_command_reference.py
python scripts/check_docs_sync.py
```

README `## Usage`: `omm arena --leaderboard  # Show how people with machines like yours rated models`.

- [ ] **Step 5: Run** `python -m pytest tests/test_cli_arena_leaderboard.py tests/test_cli_arena.py tests/test_session_cache.py tests/test_arena_board.py -q` and `python -m pytest -q` (expect only the known `FORCE_COLOR` failures; compare against a run on the branch base). Smoke: `OMM_HOME=$(mktemp -d) omm arena --leaderboard` → "hasn't been published yet", exit 0.
- [ ] **Step 6: Commit** `git commit -m "feat(arena): add omm arena --leaderboard"` (add the files above).

---

## Part 4 — omm.run page (repo `~/Project/omm-site`)

Before Task 9: `git fetch` and fast-forward `main` (local `.claude/*` changes are not ours; stop if they block), then `git checkout -b feat/arena-leaderboard`. Run `npm ci`. Read `node_modules/next/dist/docs/` for route params, `dynamic`, and server components before writing pages.

### Task 9: Leaderboard data layer with server-side verification

**Files:**
- Create: `src/lib/arena/source.ts`, `src/lib/arena/verify.ts`, `src/lib/arena/types.ts`, `src/lib/arena/groups.ts`
- Modify: `scripts/check-omm-sync.mjs`
- Test: `tests/arena-source.test.ts`, `tests/fixtures/arena/` (signed fixtures)

**Interfaces:**
- Produces:
  - `ARENA_PUBLIC_KEY = "p8uo6GFXDcg8Rp7/t8GGl5hwPsXhObY5vI1sll5KpaI="` (in `verify.ts`).
  - `verifySignedArtifact(raw: Uint8Array, manifest: unknown, publicKeyB64: string): Promise<boolean>` — manifest `schema_version === 1`, `artifact_sha256` equals SHA-256 hex of `raw`, Ed25519 verify of base64 `signature` over `raw` via `crypto.subtle.importKey("raw", key, { name: "Ed25519" }, false, ["verify"])`.
  - `type BoardState = { kind: "ranked" | "collecting"; artifact: ArenaArtifact } | { kind: "unpublished" } | { kind: "error"; reason: "verification" | "unavailable" | "too_new" }`
  - `fetchBoard(): Promise<BoardState>` — same structure as `src/lib/omm-docs/source.ts` (runtime `caches.default`, `EDGE_CACHE_TTL_SECONDS = 600`, 5s timeout, `redirect: "manual"`), caching the raw bytes + manifest pair and verifying on every read, cache hit included. 404 on the artifact → `unpublished`. `React.cache`-wrapped.
  - `groupLabel(key: string, locale: Locale): string` and `groupFamily(key): "unified" | "vram" | "cpu" | "unknown"` mirroring `memory_group.display_label` (ko: `Mac · 통합 메모리 16GB`, `PC · GPU VRAM 20–24GB`, `CPU 전용 · RAM 32GB`); unknown → raw key.
- `check-omm-sync.mjs`: read `src/omm/config.py` from `OMM_SOURCE_DIR`, extract `"catalog_public_key": "<b64>"`, fail if it differs from `ARENA_PUBLIC_KEY` in `src/lib/arena/verify.ts`.

- [ ] **Step 1: Make fixtures.** In the omm repo, generate a throwaway key pair with `python scripts/sign_catalog.py generate`, build a ranked and a collecting artifact with `scripts/aggregate_arena_votes.py --votes-file` on synthetic rows (no `--quality-gate` for the ranked one), sign each, and copy artifact + manifest + the throwaway public key into `tests/fixtures/arena/`. The test public key is passed in; it is never `ARENA_PUBLIC_KEY`.
- [ ] **Step 2: Write the failing tests** (`tests/arena-source.test.ts`): valid fixture verifies; one flipped byte → false; manifest hash mismatch → false; `fetchBoard` with a stubbed `fetch` returning 404 → `unpublished`; tampered → `{kind: "error", reason: "verification"}`; `schema_version: 3` signed → `too_new`; collecting fixture → `collecting`; `groupLabel("vram:7", "en") === "vram:7"`.
- [ ] **Step 3: Run** `npm test` — FAIL. **Step 4: Implement.** **Step 5: Run** `npm test` and `OMM_SOURCE_DIR=/Users/shinmingyu/Project/Localfit npm run check:omm-sync` — PASS.
- [ ] **Step 6: Commit** `git commit -m "기능: 아레나 리더보드 데이터를 서명 검증 후 불러오기"` (match the site repo's Korean commit style; check `git log` first).

### Task 10: The page — hero, scope nav, tier table, drawer, states

**Files:**
- Create: `src/app/[locale]/leaderboard/page.tsx`, `src/components/leaderboard/{Hero,ScopeNav,TierTable,ModelDrawer,CollectingState,BoardMessage}.tsx`
- Modify: `src/i18n/dictionaries/en.ts`, `ko.ts` (new `leaderboard` section), `src/components/Nav.tsx` (one link)
- Test: `tests/leaderboard.test.ts`

**Interfaces:**
- Consumes: `fetchBoard`, `groupLabel`, `groupFamily`, types (Task 9).
- Produces: route `/leaderboard` and `/ko/leaderboard`; scope in the query (`?scope=unified:16`, default `overall`); selected model in `?model=<key>`. `export const dynamic = "force-dynamic"`.

Layout follows the mockup v4 file exactly in structure (hero → board on `--bg-1` with nav `--bg-0` / table / drawer `--bg-2` → analysis), with the spec Part 4 text for every element. Specifics the mockup leaves open:
- Nav: "전체"/"All machines" first; then families in order unified, vram, cpu, unknown; each item shows its `corpus.rows_used`; unranked items dimmed + "표본 부족"/"Not enough votes" tag; picking one shows `이 구간은 아직 표본이 부족해 전체 순위를 보여줍니다.` / `Not enough votes in this group yet — showing all machines.` above the overall board.
- Quality cell: `strength.toFixed(2)±((ci_high-ci_low)/2).toFixed(2)`; efficiency `Math.exp(rating).toFixed(1)+"×"` only when `efficiency_comparable`, else `—`.
- Drawer: most-beaten opponent = the pair in this scope with the highest `wins_self / (wins_self + wins_other)` among pairs with `battles ≥ 5`, shown as `<name> 상대 <pct>% 승 (<battles>전)`; omitted when none qualifies. Install line `$ omm install <exact ref>`; no install line for a model without `repo_id`. `raw_median_tok_s_per_gb` shown once, labelled per Global Constraints.
- States: `collecting` → `CollectingState` (hero KPIs with real counts, one sentence, per-group counts in the nav, `omm arena` CTA, no table/charts); `unpublished` → `BoardMessage` "아직 공개된 리더보드가 없습니다 — `omm arena`로 투표해 주세요"; `error` → `BoardMessage` with no numbers at all.
- Narrow screens: nav → horizontal scroller above the table; drawer → expands under the selected row.

- [ ] **Step 1: Write the failing tests** (`tests/leaderboard.test.ts`, rendering components with fixture data via `react-dom/server` `renderToStaticMarkup`): ranked fixture renders tier bands and no element for provisional models beyond the "+ 표본 부족 모델 N개" line; collecting fixture renders no `<table>`; error state renders no digits from the artifact; unknown group key renders its raw key; ko and en dictionaries have identical key sets.
- [ ] **Step 2: Run** `npm test` — FAIL. **Step 3: Implement.** **Step 4: Run** `npm test`, `npm run lint`, `npx tsc --noEmit` — PASS.
- [ ] **Step 5: Look at it.** `npm run dev` with `fetch` pointed at the fixtures (a dev-only env var `ARENA_FIXTURE_DIR` read in `source.ts` only when `NODE_ENV !== "production"`, test-key verified); screenshot `/leaderboard`, `/ko/leaderboard?scope=unified:16`, collecting and error states at 1440px and 390px; compare against mockup v4.
- [ ] **Step 6: Commit** `git commit -m "기능: 아레나 리더보드 페이지 (보드·상세·수집 중 상태)"`.

### Task 11: Analysis charts and QA

**Files:**
- Create: `src/components/leaderboard/{RankMatrix,WinRateHeatmap,QualityEfficiencyScatter}.tsx` (inline SVG, no chart library)
- Modify: `src/app/[locale]/leaderboard/page.tsx`, dictionaries
- Test: `tests/leaderboard-charts.test.ts`

**Interfaces:**
- Consumes: ranked `ArenaArtifact` (overall + groups + `pairs`), current scope key.

Rules (spec Part 4 §3): one full-width row each, text and chart alternating sides, one headline number each computed from the artifact (rank matrix: number of ranked scopes; heatmap: the largest real win rate with its battle count; scatter: number of plotted models). Rank matrix: rows = models ranked in ≥ 1 scope, columns = ranked scopes; cell = rank in that scope or `—`; each column's #1 cell `--accent` fill with `--accent-ink` text; current scope column on `--bg-2`. Heatmap: models of the current scope, cell = row's wins ÷ (row + column wins) from `pairs`, wins ≥ 50% on `--accent` at alpha steps 0.2/0.4/0.6/0.8, losses on `--bg-2`/`--bg-3`/`--line-1`, no pair → empty cell; `<title>` tooltip `"<a> vs <b>: <pct>% (<battles> battles)"`. Scatter: only `efficiency_comparable` models of the current scope, y = strength, x = rating, tier 1 `--accent`, tier 2 `--ink-1`, others `--ink-3`, every point labelled. Hidden when the current scope is collecting.

- [ ] **Step 1: Write the failing tests:** heatmap cell values equal the fixture pair math; scatter omits non-comparable models; matrix marks exactly one accent cell per ranked column; no hex/rgb literal in the three components (grep test: only `var(--…)`).
- [ ] **Step 2: Run** `npm test` — FAIL. **Step 3: Implement.** **Step 4: Run** `npm test && npm run lint && npx tsc --noEmit` — PASS.
- [ ] **Step 5: QA greps from DIRECTION.md §5** over `src/components/leaderboard src/app/[locale]/leaderboard`:

```bash
grep -rnE "#[0-9A-Fa-f]{3,8}\b|rgb\(|hsl\(" src/components/leaderboard src/app/\[locale\]/leaderboard   # expect none
grep -rnE "rounded-(2xl|3xl|full)|9999px|1\.5rem|backdrop-filter|box-shadow" src/components/leaderboard   # expect none
grep -rnP "[\x{1F300}-\x{1FAFF}\x{2600}-\x{27BF}]" src/components/leaderboard src/i18n/dictionaries   # expect none
grep -rniE "supercharge|seamless|effortless|unlock|revolutionize|game-chang|blazing|next-level" src/components/leaderboard src/i18n/dictionaries   # expect none
npm run build   # OpenNext build succeeds
```

- [ ] **Step 6: Commit** `git commit -m "기능: 리더보드 분석 차트 (기기별 순위·맞대결 승률·품질×효율)"`.

---

## Part 5 — wrap-up

### Task 12: Final verification and hand-off (no outward action without the user)

- [ ] **Step 1:** omm repo: `python -m pytest -q`, `cd cf-worker && npm test`, emulator rules test, `python scripts/check_docs_sync.py`; site: `npm test && npm run lint && npm run build`. Record exact results.
- [ ] **Step 2:** Request a whole-branch review (superpowers:requesting-code-review) for each repo; fix confirmed findings.
- [ ] **Step 3:** Update the memory file for #410 (what shipped, deploy ids, the six planning calls) and CLAUDE.md's Arena paragraph (`--leaderboard`, artifact v2, collecting status, groups).
- [ ] **Step 4: Stop and ask the user** about: pushing `feat/arena-leaderboard` → merge into `beta`; the beta → main PR that makes the nightly job publish (spec "Dependency on main"); pushing and deploying the site branch (`npm run deploy`). Each is a separate ask. PR bodies follow the Korean four-section template.
