# Cluster version follow, RAG backup, head handover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Members follow the head's omm commit, hold a backup of the head's document store, and the head role can move to another member with `omm cluster handover <mac>`.

**Architecture:** A new per-member LaunchAgent (`com.omm.cluster-member`, `_cluster-member-run`) polls the head's gateway on new `/omm/member/*` routes, authenticated both ways with a key derived from the cluster namespace. The head answers with its commit, its document-store marker and any pending handover; the member updates itself through the existing `omm update` trust path, pulls a SQLite snapshot, or promotes itself. Handover is driven from the old head's CLI; a head that finds a peer head with a higher epoch steps down.

**Deviations from the spec's wording (same intent):** the spec's three GET routes (version, rag, role) are folded into one `POST /omm/member/poll` that also carries the member's report; the spec's "wait for the target's RAG copy to match" pre-check is replaced by freezing document writes on the old head while a handover is pending and having the target fetch one final snapshot whose marker must match the instruction.

**Tech Stack:** Python 3.10+, stdlib `hmac`/`hashlib`/`http.client`/`ssl`/`sqlite3`, launchd, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-27-cluster-handover-version-sync-design.md`

## Global Constraints

- Gateway port stays `52440` (`gateway.GATEWAY_PORT`); exo API `127.0.0.1:52415`.
- Member key = `HMAC-SHA256(key=namespace.encode(), msg=b"omm-cluster-member-link-v1")`. The namespace itself is never sent.
- Member poll interval `60` s; request clock window `120` s; handover instruction expiry `300` s; old head waits for the new head up to `180` s; stale-head epoch check every `300` s and at gateway start.
- Members move only forward (`trust.verify_update(..., same_branch=True)`); a channel switch uses `same_branch=False`. No dev bypass of the trust check.
- Member traffic: Thunderbolt link-local (`169.254.`) addresses first, then other IPv4s, always HTTPS, every head reply proven over the leaf certificate fingerprint and the body hash.
- Employee tokens never open `/omm/member/*`; the member credential never opens employee routes.
- Backup copy path `OMM_HOME/cluster/rag-backup/library.sqlite3`, mode `0600`, written atomically. A member's own `OMM_HOME/documents` is never touched except at promotion (moved aside to `library.sqlite3.before-handover-<YYYYmmddHHMMSS>`, with `-wal`/`-shm`).
- Handover never transfers `tokens.json` or `OMM_HOME/cluster/tls/`.
- New hidden command `_cluster-member-run` goes into every set that already lists `_cluster-gateway-run` in `src/omm/cli.py` (currently lines ~2434, 3108, 3238, 3245, 3254).
- Lazy imports only (`requests`, `questionary`, `importlib.metadata`, and `omm.cli` from cluster modules) — keep `omm help` fast.
- User-facing strings in English like the rest of `omm cluster`.

## Review Focus

1. **A head reply forged by someone on the office LAN** (member falls back to LAN): the member must refuse the snapshot/version/instruction unless the head proof over nonce + leaf fingerprint + body hash verifies → Task 1 `test_head_proof_rejects_other_certificate`, Task 5 `test_member_rejects_unproven_reply`.
2. **Employee writes a document while a handover runs**: the write must be refused (503) rather than lost when the store moves → Task 4 `test_rag_writes_refused_during_handover`.
3. **Member ahead of the head** (someone ran `omm update` on a member): must not downgrade, must report `ahead` → Task 2 `test_decide_member_ahead_never_downgrades`.
4. **Interrupted handover re-run** (target went offline after the instruction): old head stays head, instruction expires, re-run works → Task 7 `test_handover_times_out_and_stays_head`, `test_handover_rerun_after_expiry`.
5. **Old member that was joined before this feature** (no member service, firewall without 52440): handover refuses it with the re-join hint; status shows it as not following → Task 6 `test_status_flags_member_without_link`, Task 7 `test_handover_refuses_unprepared_target`.

---

### Task 1: Member-link authentication

**Files:**
- Create: `src/omm/cluster/member_auth.py`
- Test: `tests/test_cluster_member_auth.py`

**Interfaces:**
- Produces:
  - `member_key(namespace: str) -> bytes`
  - `HEADER_NODE = "X-Omm-Node"`, `HEADER_TIME = "X-Omm-Time"`, `HEADER_NONCE = "X-Omm-Nonce"`, `HEADER_PROOF = "X-Omm-Proof"`, `HEADER_HEAD_PROOF = "X-Omm-Head-Proof"`
  - `sign_request(key: bytes, method: str, path: str, node_id: str, *, now: float, nonce: str) -> dict[str, str]` — the four request headers. Proof = hex `HMAC(key, f"member|{method}|{path}|{node_id}|{int(now)}|{nonce}")`.
  - `class NonceCache: seen(nonce: str, now: float) -> bool` (True when already used inside the window; prunes older entries; thread-safe).
  - `verify_request(key: bytes, method: str, path: str, headers, *, now: float, nonces: NonceCache) -> str | None` — node id or None (bad proof, time outside ±120 s, reused nonce, missing header). `hmac.compare_digest` only.
  - `head_proof(key: bytes, nonce: str, leaf_sha256: str, body_sha256: str) -> str` = hex `HMAC(key, f"head|{nonce}|{leaf_sha256}|{body_sha256}")`.
  - `verify_head(key: bytes, nonce: str, leaf_sha256: str, body_sha256: str, proof: str) -> bool`
  - `leaf_sha256(der: bytes) -> str` (lowercase hex).

- [ ] **Step 1: Write the failing tests**

```python
def test_member_key_is_not_the_namespace():
    ns = "omm-" + "a" * 32
    assert member_key(ns) != ns.encode() and len(member_key(ns)) == 32
    assert member_key(ns) == member_key(ns) != member_key("omm-" + "b" * 32)

def test_signed_request_verifies_once():
    key, cache = member_key("omm-" + "a" * 32), NonceCache()
    h = sign_request(key, "POST", "/omm/member/poll", "12D3node", now=1000, nonce="n1")
    assert verify_request(key, "POST", "/omm/member/poll", h, now=1010, nonces=cache) == "12D3node"
    assert verify_request(key, "POST", "/omm/member/poll", h, now=1010, nonces=cache) is None  # replay

def test_request_outside_window_or_other_path_fails():
    key = member_key("omm-" + "a" * 32)
    h = sign_request(key, "POST", "/omm/member/poll", "n", now=1000, nonce="x")
    assert verify_request(key, "POST", "/omm/member/poll", h, now=1121, nonces=NonceCache()) is None
    assert verify_request(key, "GET", "/omm/member/rag/snapshot", h, now=1000, nonces=NonceCache()) is None

def test_other_cluster_key_fails():
    h = sign_request(member_key("omm-" + "b" * 32), "POST", "/p", "n", now=1, nonce="x")
    assert verify_request(member_key("omm-" + "a" * 32), "POST", "/p", h, now=1, nonces=NonceCache()) is None

def test_head_proof_rejects_other_certificate():
    key = member_key("omm-" + "a" * 32)
    proof = head_proof(key, "n1", "aa" * 32, "bb" * 32)
    assert verify_head(key, "n1", "aa" * 32, "bb" * 32, proof)
    assert not verify_head(key, "n1", "cc" * 32, "bb" * 32, proof)
    assert not verify_head(key, "n1", "aa" * 32, "dd" * 32, proof)
    assert not verify_head(key, "n2", "aa" * 32, "bb" * 32, proof)
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_member_auth.py -q` — expect ImportError.
- [ ] **Step 3:** Implement `member_auth.py` per the Interfaces block (stdlib only).
- [ ] **Step 4:** Run the same command — expect all pass.
- [ ] **Step 5:** Commit `feat(cluster): member-link request and head-reply proofs`.

---

### Task 2: Install an exact head commit through the trust path

**Files:**
- Modify: `src/omm/cli.py` — `_git_update_src` (~4336), `_perform_update` (~4389), `_perform_update_unlocked` (~4405)
- Create: `src/omm/cluster/version_follow.py`
- Test: `tests/test_cluster_version_follow.py` (do **not** add to `tests/test_cli_update.py`; see CLAUDE.md about it touching the real `~/.omm/src`)

**Interfaces:**
- Produces in `cli.py`: `_git_update_src(branch: str = "main", *, same_branch: bool = True, target_commit: str | None = None)`, `_perform_update(branch, *, same_branch=True, target_commit=None)`, `_perform_update_unlocked(branch, *, same_branch=True, target_commit=None)`. With `target_commit`: after the fetch, require `git merge-base --is-ancestor <target> origin/<branch>` (else returncode 1, stderr `"commit <short> is not on <branch>"`), verify with `trust.verify_update(SRC_DIR, current, target, anchor, same_branch=...)`, then `checkout -B <branch> <target> --force --quiet`. Without it, behaviour is unchanged.
- Produces in `version_follow.py`:
  - `local_version() -> dict` → `{"commit": str | None, "branch": "main" | "beta", "git": bool}` (lazy `from omm import cli`; `cli._src_head_commit()`, `cli._channel_branch()`, `package_metadata.install_source() is InstallSource.GIT`).
  - `decide(local: dict, head: dict, *, is_ancestor: Callable[[str, str], bool | None]) -> str` — one of `"same"`, `"update"`, `"switch"` (branch differs), `"ahead"` (head commit is an ancestor of local), `"manual"` (either side not git or head commit None).
  - `apply(head: dict) -> tuple[str, str | None]` — runs `decide`, then for `update` calls `cli._perform_update(branch, target_commit=c)`, for `switch` calls `cli._perform_update(head_branch, same_branch=False, target_commit=c)` then `config.update_config(update_channel="beta" if head_branch == "beta" else "stable")`. Returns `(state, error)` where state ∈ `same|updated|switched|ahead|manual|blocked` and error is the first stderr line when blocked.

- [ ] **Step 1: Write the failing tests**

```python
def test_decide_same_and_update():
    head = {"commit": "b" * 40, "branch": "main", "git": True}
    assert decide({"commit": "b" * 40, "branch": "main", "git": True}, head, is_ancestor=lambda a, d: True) == "same"
    assert decide({"commit": "a" * 40, "branch": "main", "git": True}, head, is_ancestor=lambda a, d: False) == "update"

def test_decide_member_ahead_never_downgrades():
    head = {"commit": "a" * 40, "branch": "main", "git": True}
    assert decide({"commit": "b" * 40, "branch": "main", "git": True}, head, is_ancestor=lambda a, d: a == "a" * 40) == "ahead"

def test_decide_switch_and_manual():
    assert decide({"commit": "a" * 40, "branch": "main", "git": True}, {"commit": "b" * 40, "branch": "beta", "git": True},
                  is_ancestor=lambda a, d: False) == "switch"
    assert decide({"commit": None, "branch": "main", "git": False}, {"commit": "b" * 40, "branch": "main", "git": True},
                  is_ancestor=lambda a, d: False) == "manual"
    assert decide({"commit": "a" * 40, "branch": "main", "git": True}, {"commit": None, "branch": "main", "git": False},
                  is_ancestor=lambda a, d: False) == "manual"

def test_apply_reports_blocked_with_first_stderr_line(monkeypatch):
    # patch cli._perform_update to return CompletedProcess([], 1, "", "commit abc is not on main\nmore")
    assert apply(head) == ("blocked", "commit abc is not on main")

def test_apply_switch_sets_channel(monkeypatch):
    # _perform_update called with ("beta", same_branch=False, target_commit=c); update_config(update_channel="beta")

def test_git_update_src_refuses_commit_off_branch(monkeypatch, tmp_path):
    # point cli.SRC_DIR at a tmp git repo (never the real one); fake _run_git so merge-base --is-ancestor returns 1
    result = cli._git_update_src("main", target_commit="c" * 40)
    assert result.returncode == 1 and "is not on main" in result.stderr

def test_git_update_src_checks_out_target(monkeypatch, tmp_path):
    # merge-base ok, trust.verify_update -> (True, ""); last _run_git argv == [..., "checkout", "-B", "main", "c"*40, "--force", "--quiet"]
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_version_follow.py -q` — expect failures.
- [ ] **Step 3:** Add `target_commit` to the three `cli.py` functions; create `version_follow.py`.
- [ ] **Step 4:** Run the file, then `python -m pytest tests/test_cli_update.py -q -k "not rmtree"` only if `~/.omm/src` does not exist on this machine (CLAUDE.md warning); otherwise skip and note it. Expect pass.
- [ ] **Step 5:** Commit `feat(cluster): install an exact signed head commit`.

---

### Task 3: Document-store snapshot and backup copy

**Files:**
- Create: `src/omm/cluster/rag_backup.py`
- Test: `tests/test_cluster_rag_backup.py`

**Interfaces:**
- Produces:
  - `live_path() -> Path` = `OMM_HOME/documents/library.sqlite3`; `backup_path() -> Path` = `OMM_HOME/cluster/rag-backup/library.sqlite3`
  - `store_marker(path: Path | None = None) -> str | None` — `None` when the file does not exist; else `f"{mtime_ns}:{size}"` of the db joined with the same for `-wal` if present (`"db|wal"`). Cheap; no SQLite open.
  - `snapshot() -> tuple[Path, str, str] | None` — `(file, sha256_hex, marker)`; builds with `sqlite3.Connection.backup` into `OMM_HOME/cluster/rag-snapshot/library.sqlite3`, reused while the marker is unchanged (marker + sha stored next to it in `snapshot.json`). Refuses symlinks like `DocumentStore.connection`.
  - `install_backup(tmp_file: Path, *, sha256: str, marker: str) -> None` — checks the sha, runs `PRAGMA integrity_check` on it, then atomically replaces `backup_path()` (mode 0600) and writes `backup.json` `{"marker", "sha256", "saved_at"}`.
  - `backup_marker() -> str | None` (from `backup.json`); `backup_saved_at() -> str | None`.
  - `adopt_backup_as_live(now: datetime) -> Path | None` — moves an existing live store (+`-wal`/`-shm`) aside to `library.sqlite3.before-handover-<YYYYmmddHHMMSS>` and returns that path (None when there was none), then moves the backup into `live_path()`.
  - `demote_live_to_backup() -> None` — checkpoints the live store (`PRAGMA wal_checkpoint(TRUNCATE)`), moves it to `backup_path()`, writes `backup.json` with its marker.
  - `delete_backup() -> None`

- [ ] **Step 1: Write the failing tests** (all with `OMM_HOME` monkeypatched to `tmp_path`, stores made through `documents.DocumentStore`)

```python
def test_marker_none_without_store(): assert store_marker() is None
def test_marker_changes_after_write(): m1 = store_marker(); <import a document>; assert store_marker() != m1
def test_snapshot_is_reused_until_marker_changes(): a = snapshot(); b = snapshot(); assert a == b; <write>; assert snapshot()[1] != a[1]
def test_install_backup_rejects_wrong_sha(): with pytest.raises(ClusterError): install_backup(f, sha256="0"*64, marker="m")
def test_install_backup_mode_and_marker(): install_backup(f, sha256=sha, marker="m"); assert backup_marker() == "m"; assert stat.S_IMODE(backup_path().stat().st_mode) == 0o600
def test_adopt_moves_own_store_aside(): <live store with doc "mine", backup with doc "company">; aside = adopt_backup_as_live(datetime(2026,10,27,12,0,0)); assert aside.name == "library.sqlite3.before-handover-20261027120000"; <live store lists "company">
def test_demote_then_adopt_round_trip(): demote_live_to_backup(); assert not live_path().exists(); assert backup_marker()
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_rag_backup.py -q` — expect ImportError.
- [ ] **Step 3:** Implement `rag_backup.py`.
- [ ] **Step 4:** Run — expect pass.
- [ ] **Step 5:** Commit `feat(cluster): document-store snapshot and member backup copy`.

---

### Task 4: Gateway member routes, member reports, write freeze

**Files:**
- Modify: `src/omm/cluster/settings.py` (add `head_epoch: int = 0` to `ClusterSettings`, load/save it; old files load as 0)
- Create: `src/omm/cluster/members.py` (head-side records)
- Modify: `src/omm/cluster/gateway.py` (`GatewayServer.__init__`, `_Handler._dispatch`)
- Test: `tests/test_cluster_gateway_member.py`, extend `tests/test_cluster_settings*.py` if one exists, else put the settings test in the new file

**Interfaces:**
- Consumes: Task 1 (`member_auth.*`), Task 2 (`version_follow.local_version`), Task 3 (`rag_backup.store_marker`, `rag_backup.snapshot`).
- Produces in `members.py`:
  - `record(node_id: str, report: dict, *, now: datetime) -> None` → `OMM_HOME/cluster/members.json` `{node_id: {**report, "seen_at": iso}}` (atomic).
  - `load() -> dict[str, dict]`; `clear() -> None`.
  - `pending_handover() -> dict | None` — `OMM_HOME/cluster/handover.json` when not expired: `{"to": node_id, "epoch": int, "expires_at": iso, "settings": {"models": [...], "known_nodes": [...], "gateway_port": int}, "rag_marker": str | None}`.
  - `start_handover(to: str, epoch: int, settings: dict, rag_marker: str | None, *, now: datetime) -> None` (expiry now+300 s); `clear_handover() -> None`.
- Produces in `gateway.py`:
  - `MEMBER_PREFIX = "/omm/member/"`; routes `("POST", "/omm/member/poll")` and `("GET", "/omm/member/rag/snapshot")`.
  - `GatewayServer(..., member_key: bytes | None = None)` — default from `settings.load().namespace` via `member_auth.member_key`, None when there are no settings; holds a `member_auth.NonceCache`.
  - Poll request body (JSON, ≤ 64 KiB): `{"version": {...local_version()}, "version_state": str, "version_error": str|None, "rag_marker": str|None, "prepared": bool, "documents_ready": bool}`. Head records it (`members.record`) and replies `{"head": true, "epoch": int, "version": local_version(), "rag_marker": store_marker(), "handover": <pending_handover() if its "to" == caller else null>}`.
  - Every member-route reply carries `X-Omm-Head-Proof` = `head_proof(key, request nonce, sha256 of the gateway's current leaf DER, sha256(body))`. Leaf DER is read from `tls_dir()/SERVER_FILE` at reply time.
  - Snapshot reply streams the file from `rag_backup.snapshot()` with `X-Omm-Rag-Marker` and `X-Omm-Rag-Sha256` headers; the proof's body hash is that sha256. 404 when the head has no store.
  - Member routes are checked **before** the employee-token check and return 401 for a bad/missing member proof; an employee bearer token on a member route is ignored (still 401). Member proof headers on any other route are ignored (that route still needs a token).
  - While `pending_handover()` is not None, `/rag/*` and `/mcp` answer 503 `{"type":"overloaded_error","message":"omm cluster: the head is moving to another Mac; try again in a few minutes"}`.

- [ ] **Step 1: Write the failing tests** (start a real `GatewayServer` on `127.0.0.1:0` with a tmp TLS dir like `tests/test_cluster_gateway.py` does; client uses a blind SSL context and `member_auth.sign_request`)

```python
def test_settings_head_epoch_round_trip_and_default(): ...  # old file without head_epoch loads 0
def test_member_poll_records_report_and_proves_reply(): # reply 200, members.load()[node]["version_state"] == "same", verify_head(...) is True
def test_member_route_rejects_employee_token(): # Authorization: Bearer <valid employee token>, no member headers -> 401
def test_member_headers_do_not_open_employee_routes(): # member headers on GET /omm/status -> 401
def test_replayed_member_request_rejected(): # same headers twice -> second 401
def test_handover_instruction_only_for_target(): # start_handover(to="A"); poll as "A" -> handover dict; poll as "B" -> null
def test_snapshot_route_streams_backup_with_marker(): # body sha256 == X-Omm-Rag-Sha256, proof verifies over it
def test_snapshot_404_without_store(): ...
def test_rag_writes_refused_during_handover(): # start_handover(...); POST /rag/... with a valid employee token -> 503
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_gateway_member.py tests/test_cluster_gateway.py -q` — new tests fail, old ones pass.
- [ ] **Step 3:** Implement `settings.head_epoch`, `members.py`, gateway changes.
- [ ] **Step 4:** Run both files — expect pass.
- [ ] **Step 5:** Commit `feat(cluster): member routes on the head gateway`.

---

### Task 5: Member link client, loop and service

**Files:**
- Create: `src/omm/cluster/member_link.py`, `src/omm/cluster/member_service.py`
- Modify: `src/omm/cli.py` — add hidden `_cluster-member-run` next to `_cluster-gateway-run` (~3029) and to every skip set listing `_cluster-gateway-run`
- Modify: `src/omm/cluster/launchd.py` — `install_agent(..., keep_if_same: bool = False)`: when True and the existing plist bytes equal the new ones and the job is loaded, do nothing
- Modify: `src/omm/cluster/exo_service.py` — `install(namespace, *, run=..., keep_if_same: bool = False)` passes it through
- Modify: `src/omm/cluster/document_search.py` — add `ready_for(model: str, base_url: str) -> bool` (Ollama running and model pulled, via `_ollama`)
- Test: `tests/test_cluster_member_link.py`, `tests/test_cluster_service.py` (member service plist), `tests/test_cluster_exo_service.py` (keep_if_same)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces in `member_service.py` (mirror `service.py`): `LAUNCHD_LABEL = "com.omm.cluster-member"`, `_SUBCOMMAND = "_cluster-member-run"`, wrapper `OMM_HOME/bin/omm-cluster-member`, log `OMM_HOME/logs/cluster-member.log`, `install()`, `uninstall()`, `is_installed()`, `is_running()`.
- Produces in `member_link.py`:
  - `POLL_SECONDS = 60`
  - `ordered_addresses(raw_state: dict, own_node: str | None) -> list[tuple[str, str]]` — `(node_id, address)` for every peer except `own_node`, `169.254.` addresses first, from `exo_client.node_ipv4s`.
  - `own_node_id(raw_state: dict, local_ipv4s: list[str]) -> str | None` — the node whose `node_ipv4s` contain one of this Mac's addresses (`tls._lan_ipv4s()` plus link-local); None if not exactly one match.
  - `class HeadReply(NamedTuple): node_id: str; address: str; epoch: int; body: dict`
  - `request(address: str, method: str, path: str, key: bytes, own_node: str, *, body: bytes | None = None, timeout: float = 5) -> tuple[int, bytes, dict]` — HTTPS with a blind context (`check_hostname=False`, `CERT_NONE`), signs with `sign_request`, reads the peer leaf DER from the socket, **raises `ClusterError("reply was not proven")`** unless `verify_head` passes. Returns `(status, body, headers)`.
  - `find_head(raw_state, key, own_node) -> HeadReply | None` — polls every address of `ordered_addresses` in parallel (≤16 workers, the first address per node that answers), keeps replies with `head: true`, returns the highest epoch.
  - `report() -> dict` — the poll body of Task 4 (`prepared` = `firewall.status(gateway_port=GATEWAY_PORT) in ("active","unchecked")` and `appfw.not_allowed([appfw.gateway_binary()]) == []`; `documents_ready` = `document_search.ready_for(*backup's embedding settings)` or True when there is no backup).
  - `run_once(*, now) -> str` — one cycle: settings must be role member; finish a pending step-down (Task 7 `handover.finish_step_down()` when its marker exists); `exo_service.install(namespace, keep_if_same=True)` on the first cycle only; `find_head`; if the reply has a handover for us → `handover.promote(reply)` (Task 7) and return `"promoted"`; else `version_follow.apply(reply.body["version"])` — if `updated`/`switched`, return that so the loop exits for launchd to restart on new code; else, if `reply.body["rag_marker"]` differs from `rag_backup.backup_marker()`, GET the snapshot into a tmp file and `rag_backup.install_backup`. Returns a short state string; logs to `omm.cluster.member` logger.
  - `run_member_link() -> int` — loop `run_once` every `POLL_SECONDS`; exit 0 after `updated`/`switched`/`promoted`; swallow and log any other exception per cycle.

- [ ] **Step 1: Write the failing tests**

```python
def test_ordered_addresses_puts_thunderbolt_first(): # node_ipv4s {"B": ["192.0.2.3", "169.254.9.9"]} -> [("B","169.254.9.9"),("B","192.0.2.3")]; own node excluded
def test_own_node_id_matches_local_address(): ...; def test_own_node_id_none_when_ambiguous(): ...
def test_member_rejects_unproven_reply(): # fake head server signs with another key -> request raises ClusterError
def test_find_head_prefers_higher_epoch(): # two fake heads epoch 3 and 4 -> epoch 4
def test_run_once_updates_and_exits(monkeypatch): # version_follow.apply -> ("updated", None); run_once == "updated"; no snapshot fetched
def test_run_once_pulls_snapshot_when_marker_differs(monkeypatch): # install_backup called with the reply's sha/marker
def test_run_once_skips_snapshot_when_marker_same(monkeypatch): ...
def test_run_once_promotes_on_instruction(monkeypatch): # handover.promote called once, returns "promoted"
def test_member_service_plist(tmp_path, monkeypatch): # label com.omm.cluster-member, argv ends with "_cluster-member-run", OMM_HOME env
def test_install_agent_keep_if_same_skips_bootout(): # identical plist + loaded -> no launchctl bootout call
def test_cluster_member_run_skips_update_check(): assert "_cluster-member-run" in cli._SKIP_UPDATE_CHECK_SUBCOMMANDS  # and the other sets
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_member_link.py tests/test_cluster_service.py tests/test_cluster_exo_service.py -q` — new tests fail.
- [ ] **Step 3:** Implement. `run_once` calls into `handover` lazily so Task 7 can land after; until then guard with `getattr`-free import inside the branch (the test monkeypatches it).
- [ ] **Step 4:** Run — expect pass. Run `python -m pytest tests/test_cli_cluster.py -q` too.
- [ ] **Step 5:** Commit `feat(cluster): member link service polls the head`.

---

### Task 6: join / leave / status / `omm update` on the head

**Files:**
- Modify: `src/omm/cluster/commands.py` — `join` (~133), `leave` (~511), `status` (~156–250), `exposure` (~250)
- Modify: `src/omm/cli.py` — `update()` (~4733) after the success line; `cluster_status_cmd` rendering (~2506)
- Test: `tests/test_cli_cluster.py` (status/join/leave), new `tests/test_cluster_update_hook.py`

**Interfaces:**
- Consumes: Task 4 `members.load()`, Task 5 `member_service.*`, Task 3 `rag_backup.delete_backup`.
- Produces:
  - `join`: `firewall.apply(gateway_port=GATEWAY_PORT)` (was the member variant), `appfw.allow([appfw.exo_binary(), appfw.gateway_binary()])`, then `member_service.install()`.
  - `leave` steps add `member_service.uninstall` (member) and `rag_backup.delete_backup`; `members.clear()` and `members.clear_handover()` on a head.
  - `status`: firewall check uses `gateway_port=current.gateway_port` for both roles; member also checks the gateway binary in appfw and `member_service.is_running()` (problem `"the member link is not running; run omm cluster join <name> again"`). Head result gains `"members": [{"name", "version", "version_state", "version_error", "rag_age_seconds", "prepared", "seen_seconds_ago"}]` built from `members.load()` joined to exo node names, and problems:
    - `"<name> runs omm <short> but the head runs <short>; it updates itself within a few minutes"` (state `update`/`switch` seen < 5 min ago is not a problem; older is),
    - `"<name> is newer than the head; run omm update on this Mac"` (state `ahead`),
    - `"<name> could not update: <error>"` (state `blocked`),
    - `"<name> is not a git install; update omm on it by hand"` (state `manual`),
    - `"<name> has not checked in; run omm cluster join <namespace> again on it"` (known node with no record, or seen > 10 min ago).
  - `exposure()`: gateway port listed for both roles; for a member, describe it as "closed until this Mac becomes head".
  - `cli.update()`: after `Ω Updated`, if `settings.load()` is a head: `launchd.restart_agent(service.LAUNCHD_LABEL)` (ignore `ClusterError` with a muted note) and, when `n = max(len(known_nodes) - 1, 0) > 0`, print the warning: `"{n} other Mac(s) in the cluster will update to this version within a few minutes. Cluster models may stop answering briefly while they do; this Mac loads them again automatically."` Same when already up to date? No — only after a real update.
  - Status rendering in `cli.py`: one line per member under a `Macs` heading: `name  omm <short>  <state>  backup <age>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_join_prepares_gateway_port_and_member_service(monkeypatch): # firewall.apply(gateway_port=52440), appfw.allow includes gateway binary, member_service.install called
def test_leave_member_removes_link_and_backup(monkeypatch): ...
def test_status_lists_member_versions(monkeypatch): # members.json with one "same" record -> status()["members"][0]["version_state"] == "same", no problem
def test_status_flags_member_ahead(monkeypatch): # "ahead" -> problem contains "run omm update on this Mac"
def test_status_flags_member_without_link(monkeypatch): # known node "node3" without record -> problem contains "omm cluster join"
def test_update_on_head_restarts_gateway_and_warns(monkeypatch, capsys): # role head, known_nodes 3 -> restart_agent(com.omm.cluster-gateway); output has "2 other Mac(s)"
def test_update_on_member_prints_no_cluster_warning(monkeypatch, capsys): ...
```

For the `update()` tests stub `cli._perform_update`, `_installed_commit`, `_remote_head_commit`, `_refresh_data` — never let it reach the real `SRC_DIR`.

- [ ] **Step 2:** Run `python -m pytest tests/test_cli_cluster.py tests/test_cluster_update_hook.py -q` — new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run both, then `python -m pytest tests -q -k cluster` — expect pass.
- [ ] **Step 5:** Commit `feat(cluster): members follow the head after omm update`.

---

### Task 7: Head handover

**Files:**
- Create: `src/omm/cluster/handover.py`
- Modify: `src/omm/cli.py` — `@cluster_app.command(name="handover")` next to `leave` (~3012)
- Modify: `src/omm/cluster/gateway.py` — `run_gateway()` starts the stale-head check thread
- Test: `tests/test_cluster_handover.py`, `tests/test_cli_cluster.py` (CLI wiring)

**Interfaces:**
- Consumes: Tasks 3–6.
- Produces in `handover.py`:
  - `WAIT_SECONDS = 180`, `STALE_CHECK_SECONDS = 300`
  - `preflight(target_name: str, *, now: datetime) -> tuple[str, list[str]]` — `(node_id, problems)`; problems (any stops the handover): not a head; target not in exo state; target is this Mac; no member record or seen > 2 min ago; record's version commit ≠ head's; `prepared` false (`"run omm cluster join <namespace> again on <name> first"`); head has documents (`document_search.check()[0]["in_use"]`) and record `documents_ready` false (`"<name> cannot answer document questions yet: install Ollama and run ollama pull <model> on it"`).
  - `start(node_id: str, *, now) -> int` — new epoch = `head_epoch + 1`; `members.start_handover(node_id, epoch, {"models": [...asdict], "known_nodes": [...], "gateway_port": ...}, rag_backup.store_marker(), now=now)`; returns epoch.
  - `wait_for_new_head(node_id: str, epoch: int, *, deadline: float, sleep=time.sleep) -> bool` — polls the target's addresses with `member_link.request(..., "POST", "/omm/member/poll", ...)` until a proven reply says `head: true` and `epoch == epoch`.
  - `step_down(new_epoch: int) -> list[str]` — on this Mac: `service.uninstall()`; old token names = `[r.name for r in tokens.list_tokens()]`; delete `tokens.tokens_path()`; `members.clear_handover()`; `members.clear()`; save settings `role="member"`, `head_epoch=new_epoch`, `models=()`; write `OMM_HOME/cluster/stepped-down.json`; `member_service.install()`. Returns the old token names.
  - `finish_step_down() -> None` — if `stepped-down.json` exists: `rag_backup.demote_live_to_backup()` (when a live store exists), then delete the marker. Called by `member_link.run_once`.
  - `promote(reply: member_link.HeadReply) -> None` — on the target: GET the snapshot from the old head once more and require its `X-Omm-Rag-Marker` == `reply.body["handover"]["rag_marker"]` (skip when that is None); `rag_backup.install_backup(...)`; `rag_backup.adopt_backup_as_live(now)` (log the aside path); save settings `role="head"`, carried `models`/`known_nodes`/`gateway_port`, `head_epoch = instruction epoch`; `service.install()` (gateway makes its own TLS CA); `member_service.uninstall()` last.
  - `stale_head_check(*, sleep=time.sleep, stop: threading.Event) -> None` — at start and every `STALE_CHECK_SECONDS`: `member_link.find_head` among peers; if a proven head with epoch > own `head_epoch`: `step_down(that epoch)` (its own process is the gateway, so `service.uninstall()` ends it; do the settings write and `member_service.install()` **before** `service.uninstall()`).
- CLI `omm cluster handover MAC [--yes/-y]`: `commands.require_head_for("handover")`; `preflight` → print problems and exit 1; confirm (prompt text below, `--yes` skips; non-tty without `--yes` exits 1 with "pass --yes"); `start`; spinner while `wait_for_new_head` (deadline `WAIT_SECONDS`); on timeout `members.clear_handover()`, print `"<MAC> did not take over within 3 minutes; this Mac is still the head. Check omm cluster status, then run omm cluster handover <MAC> again."`, exit 1; on success `step_down`, then print:

```
<MAC> is now the cluster head. This Mac is a member.
Next, on <MAC>:
  omm cluster token add <name>      for each employee: <old names, comma-separated>
  (use the same names: document ownership and shares follow the name)
Then each employee runs:
  omm rag remote set https://<MAC address>:52440
  omm setting agent cluster          (only if they used it before)
```

Confirmation text: `"Move the cluster head to <MAC>? Employee tokens are not moved: you will issue new ones on <MAC>, and every employee runs omm rag remote set again. Cluster models may stop answering for a minute."`

- [ ] **Step 1: Write the failing tests**

```python
def test_preflight_refuses_unprepared_target(): # record prepared False -> problem with "omm cluster join"
def test_preflight_refuses_version_mismatch(): ...
def test_preflight_refuses_documents_not_ready_when_head_has_documents(): ...
def test_start_bumps_epoch_and_carries_models(): # head_epoch 2 -> pending handover epoch 3 with models + known_nodes
def test_handover_times_out_and_stays_head(monkeypatch): # wait_for_new_head False -> exit 1, settings still head, handover.json removed
def test_handover_rerun_after_expiry(monkeypatch): # expired handover.json does not block a new start
def test_step_down_keeps_tls_and_removes_tokens(tmp_path): # tls dir untouched, tokens.json gone, role member, stepped-down marker written, returns old names
def test_promote_adopts_backup_and_becomes_head(monkeypatch): # role head, head_epoch == instruction epoch, service.install then member_service.uninstall (order)
def test_promote_refuses_stale_snapshot(monkeypatch): # marker mismatch -> ClusterError, settings still member
def test_stale_head_steps_down_for_higher_epoch(monkeypatch): # find_head returns epoch 5 > own 4 -> step_down(5); install member before uninstall gateway
def test_stale_head_ignores_equal_or_lower_epoch(monkeypatch): ...
def test_cli_handover_requires_yes_without_tty(): ...
```

- [ ] **Step 2:** Run `python -m pytest tests/test_cluster_handover.py -q` — expect failures.
- [ ] **Step 3:** Implement `handover.py`, the CLI command, and the stale check thread in `run_gateway()` (daemon thread, stopped in `finally`).
- [ ] **Step 4:** Run `python -m pytest tests -q -k "cluster or gateway"` — expect pass.
- [ ] **Step 5:** Commit `feat(cluster): omm cluster handover moves the head role`.

---

### Task 8: Docs and full verification

**Files:**
- Modify: `README.md` (`## Usage`: `omm cluster handover MAC  # Move the cluster head to another Mac in the cluster`), `docs/commands.json` (regenerate), `CLAUDE.md` (Cluster section: member link, `com.omm.cluster-member`, `/omm/member/*`, member key derivation, backup path, `head_epoch`, handover, gateway port open on every cluster Mac), `PRIVACY.md` only if it describes where cluster documents are stored.

- [ ] **Step 1:** `python scripts/export_command_reference.py` then `python scripts/check_docs_sync.py` — expect OK.
- [ ] **Step 2:** `python -m pytest -q` — expect only the known pre-existing `FORCE_COLOR` failures (compare against `git stash`-free baseline run on `beta` if counts look off). If `~/.omm/src` exists, run with `--deselect` for the `tests/test_cli_update.py` rmtree tests and say so.
- [ ] **Step 3:** `omm --help` and `omm cluster --help` from the venv — `handover` listed, `_cluster-member-run` hidden.
- [ ] **Step 4:** Commit `docs(cluster): member link, RAG backup and handover`.
- [ ] **Step 5 (manual, user's rig — ask before touching it):** on node1/3/4 with isolated installs per CLAUDE.md: re-join members; add a document on the head and watch `omm cluster status` show fresh backups; `omm cluster handover node1` and back; an employee PC re-runs `omm rag remote set`. Version follow end-to-end only after merge into `beta` (spec: no dev bypass). Record results as "verified"/"미검증" for the PR.
