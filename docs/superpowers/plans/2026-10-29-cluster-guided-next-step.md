# Guided `omm cluster` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bare `omm cluster` shows the next step for this Mac and the cluster and, after a yes, runs it; members report "catching up" so the head never says "join again" for a Mac that only has to wait.

**Architecture:** Status problems become `Problem` objects (a `str` subclass carrying kind/where/fix, so every existing caller and test that treats them as text keeps working). A pure `guide.steps(report)` orders them into steps; the CLI callback on `cluster_app` loops collect -> show -> ask -> run narrow fix. The member link learns its own node id from exo `GET /node_id`, reports `catching_up` + `event_index`, and the head renders progress and an ETA.

**Tech Stack:** Python 3.10+, Typer, prompt helpers in `cli.py` (`_ask_confirm`, `_ask_text`, `_ask_single_key`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-cluster-guided-next-step-design.md`

## Global Constraints

- No `omm cluster setup`; bare `omm cluster` is the feature, help at `omm cluster --help`.
- Never run anything without an explicit yes; non-tty and `--json` only print.
- Never execute on another Mac; those are `tell` steps.
- Re-run fixes are narrow (`power.keep_awake`, `firewall.apply`, `appfw.allow`, `service.install`, `member_service.install`, `commands.restart`), never all of init/join.
- No restart offered on a member that is catching up unless cables changed in this session.
- Lazy imports in the command (CLI startup).
- Docs: `scripts/export_command_reference.py`, README `## Usage`, `scripts/check_docs_sync.py`, CLAUDE.md Cluster paragraph.

## Review Focus

- A member whose `/node_id` call fails (old exo) still behaves exactly as today.
- A head with an older omm receiving the new report fields ignores them (plain dict merge in `members.record`).
- A non-tty `omm cluster` (pipe) never prompts and exits 0.
- A cluster saved before this change (no `employee_addresses`) records the current set silently instead of warning.
- macOS build comparison with a missing build string falls back to the version alone.

---

### Task 1: Member link knows itself and reports catching up

**Files:** Modify `src/omm/cluster/exo_client.py`, `src/omm/cluster/member_link.py`; Test `tests/test_cluster_member_link.py`, `tests/test_cluster_exo_client.py`

**Produces:** `ExoClient.node_id(timeout=5) -> str | None`; `member_link.catching_up(raw, own) -> bool` (own id has no `nodeNetwork` entry while others do); report keys `catching_up: bool`, `event_index: int | None`; `member_link.last_head_path()` file holding the last proven head address, tried first by `find_head(..., extra=[...])`.

- [ ] Tests: own id from `/node_id` when IPv4 matching fails; catching-up member polls with `catching_up=True` and skips `version_follow.apply` and `fetch_snapshot`; `/node_id` failure -> `no_node` as today; last head address tried before stale addresses.
- [ ] Implement, run `pytest tests/test_cluster_member_link.py tests/test_cluster_exo_client.py -q`, commit.

### Task 2: Head renders catching-up members; status stops advising "join again"

**Files:** Modify `src/omm/cluster/members.py` (`record` keeps `event_rate` from the previous `event_index`/`seen_at`), `src/omm/cluster/commands.py` (`_member_rows`, member-side status); Test `tests/test_cli_cluster.py`

- [ ] Tests: a member record with `catching_up` seen < 10 min -> problem `"<Mac> is catching up on cluster history: <i> of <head> events (<p>%), about <m> min left"` of kind `wait`; silent Mac -> `"<Mac> has not checked in; run omm cluster on it to see why"` of kind `tell`; on a catching-up member, status lists only `"exo on this Mac is still catching up on cluster history (event <i>); wait, nothing to fix"` instead of stale-topology problems.
- [ ] Implement, run tests, commit.

### Task 3: Structured problems

**Files:** Create `src/omm/cluster/problems.py` (`Problem(str)` with `kind: Literal["run","manual","wait","tell"]`, `where: str | None`, `fix: str | None`, `command: str | None`); Modify `commands.status`; Test `tests/test_cluster_guide.py`

- [ ] Map every existing status problem to a kind and fix id (`keep_awake`, `firewall`, `appfw`, `gateway`, `member_link`, `restart`); texts for those say `run omm cluster on this Mac to fix it`; macOS mismatch names the Macs not on the newest build.
- [ ] Tests for mapping and mismatch text, commit.

### Task 4: Head address change

**Files:** Modify `src/omm/cluster/settings.py` (`employee_addresses: tuple[str, ...] = ()`), `commands.init` (save `tls._lan_ipv4s()` + DHCP line), `commands.status` (problem kind `tell`, fix `addresses_seen`); Test `tests/test_cli_cluster.py`

- [ ] Tests: empty saved set -> saved silently; changed set -> problem with old/new and `omm rag remote set https://<new>:52440`; `acknowledge_addresses()` replaces the set.
- [ ] Implement, commit.

### Task 5: `guide.py` + bare `omm cluster`

**Files:** Create `src/omm/cluster/guide.py` (`Step`, `steps(report, *, role) -> list[Step]`, `FIXES: dict[str, Callable[[Out], None]]`); Modify `src/omm/cli.py` (`cluster_app` callback `invoke_without_command=True`), README, `docs/commands.json`, CLAUDE.md; Test `tests/test_cluster_guide.py`, `tests/test_cli_cluster.py`

- [ ] Tests: ordering (this-Mac run, manual, wait, tell); non-tty prints and exits 0 without prompts; not-in-cluster flow asks new/join and validates the name; employee view on macOS offers setup.
- [ ] Implement, regenerate docs, `python scripts/check_docs_sync.py`, full cluster test files, commit.

### Task 6: Rig verification

- [ ] Install the branch as `omm_verify` on the rig per CLAUDE.md, check catching-up report on Node2, bare `omm cluster` on head/member/new Mac (leave + rejoin one Mac), address-change step; report unverified paths.
