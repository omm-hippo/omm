# omm cluster: members follow the head's version, RAG backups, head handover

Date: 2026-10-27. Status: design, awaiting approval.
Builds on: `2026-10-22-exo-cluster-design.md`, `2026-10-26-cluster-cli-redesign-design.md`,
beta `4d0fe974` (gateway HTTPS #536, document-search readiness #534, multi-model autoload).

Three pieces, built in this order because each leans on the one before:

1. **Version follow** — members update themselves to the head's exact omm commit.
2. **RAG backup** — every member keeps a full copy of the head's document store.
3. **Head handover** — move the head role to another Mac already in the cluster.

All three share one new channel: a background service on every member that talks to the
head (the "member link", below).

## Why

- **Version follow.** Features landed on some Macs and not others, and the cluster broke in
  ways that were hard to see. Every member then had to be updated by hand over ssh. The user
  wants `omm update` on the head to be the whole job.
- **Head handover.** A stronger Mac arrives, or the admin wants a different Mac to be the one
  employees connect to. Today that means `leave` + `init` + `join` on every Mac, a new cluster
  name, new tokens, and the shared documents left behind on the old head.
- **RAG backup.** The shared documents live only on the head. The user asked for them to be
  backed up from the start, not only copied at handover time. With every member holding a
  copy, handover needs no "move the documents" step, and a dead head does not lose them.

## User decisions (verbatim intent, do not re-litigate)

- When members update after the head, a short interruption of the loaded cluster model is
  acceptable, **but the admin is warned** first.
- Employee PCs do **not** follow a head change automatically. After a handover, employees
  re-run `omm rag remote set <new address>` themselves.
- A handover transfers **settings only**. Anything security-sensitive (employee tokens, the
  gateway TLS CA key) is **not** transferred: the admin re-creates it on the new head by hand.
- RAG documents **are** carried over, via the backup in piece 2.
- RAG uses **full copies on every member** (not sharding). The user accepted that company
  documents then sit on every cluster Mac's disk.
- Promoting a member while the head is dead/off is **not** wanted.

## Non-goals

- Employee PCs following a head change, or employee PCs following the head's omm version.
- Promotion without a live head (disaster recovery). The backup copy exists, but no command
  turns it into a head; that is a later decision.
- Members ever serving the RAG copy to employees. It is a backup, read by nothing but a
  handover.
- Updating the exo app itself (`EXO.app`). Only omm's own code follows the head.
- Installs that are not the git-clone install (`~/.omm/src`, the one `install.sh` makes and
  `omm update` verifies). See "Install methods".

## The member link (shared by all three pieces)

A new per-user LaunchAgent on every member, `com.omm.cluster-member`, running a hidden
`omm _cluster-member-run` (same shape as `com.omm.cluster-gateway` / `_cluster-gateway-run`).
`join` installs it, `leave` removes it, `init` (and becoming head) removes it.

Every 60 s it calls the head's gateway on new routes under `/omm/member/`:

- `GET /omm/member/version` → the head's omm commit sha and update channel.
- `GET /omm/member/rag` → a change marker for the head's document store; when it differs from
  the local copy's marker, `GET /omm/member/rag/snapshot` returns a consistent SQLite copy.
- `GET /omm/member/role` → whether this Mac is the head, plus a handover instruction if one is
  pending for the calling Mac (piece 3).
- Each poll carries the member's own omm version and exo node id, so the head can show every
  Mac's version in `omm cluster status` and flag a mismatch.

**Security constraints (hard requirements):**

- `/omm/member/*` is a separate credential from employee tokens. Use a secret derived from
  the cluster namespace (`cluster.json` `namespace`, 128 random bits, already on every Mac).
  Derive it (e.g. HMAC/HKDF with a fixed label); never send the namespace itself — it is exo's
  pnet key.
- Both sides prove they hold it. A member must not accept a version or a document snapshot
  from something that merely answers on the head's address. (The gateway's TLS CA is per-head
  and changes on handover, so certificate pinning alone does not work across a handover; the
  builder chooses the mechanism, e.g. an HMAC over a request nonce + the presented cert's
  fingerprint.)
- Members prefer the head's Thunderbolt link-local addresses (169.254.x.x, which the pf anchor
  already trusts). A member with no direct Thunderbolt link to the head (a chain like
  node1–node3–node4) falls back to the head's office-LAN address, but only over the gateway's
  HTTPS with the mutual proof bound to the presented certificate, because the snapshot is the
  company's documents. Addresses come from exo's topology (`exo_client.parse_state`); the
  builder confirms which fields carry them on the real rig.
- An employee token never authorizes `/omm/member/*`, and the member credential never
  authorizes employee routes.

**Trust model.** Every Mac holding the namespace is trusted equally: a member can claim to be
head, and nothing beyond the namespace proves otherwise. This is deliberate. The namespace
already lets a Mac join exo and receive every model, and every member already receives a full
copy of the company documents (a user decision). A head-only signing key would have to move on
handover, which the user ruled out for keys. Consequences the builder must keep: a head steps
down only for a strictly higher epoch, and stepping down never destroys anything that cannot
be re-created by hand (tokens can; documents are kept as the backup copy).

**Finding the head.** A member does not store the head's address. On each poll it asks every
peer it can see in exo's topology (`/omm/member/role`) and uses the one that says it is head.
If two answer "head" (only possible mid-handover), the one with the higher `head_epoch` wins
(see piece 3).

## Piece 1 — version follow

**Behavior**

- A member that is behind the head moves forward to exactly the head's commit. A member that
  is **ahead** of the head is never moved backward: `trust.verify_update(..., same_branch=True)`
  refuses older commits on purpose (rollback protection), and the fix for that case is to run
  `omm update` on the head, which then pulls the members along. `status` says exactly that.
- A member uses the head's update channel (stable=`main` / beta). If its own
  `update_channel` setting differs, the member link changes it to the head's, through the same
  channel-switch verification `omm update` uses, and the head's `status` shows that it did.
- The member fetches the commit from GitHub (the same remote `omm update` uses), never from
  the head. The head only says *which* commit.
- The commit must pass the existing trust check (`trust.verify_commit` against
  `src/omm/trust/allowed_signers`, same rules as `omm update`, including the 2-parent merge
  case) **and** be reachable from the member's update channel branch. Fail closed: a commit
  that does not verify is not installed, and the member reports `version_blocked: <reason>`
  to the head on its next poll.
- After switching the clone, the member re-runs whatever `omm update` does after a pull
  (dependency reinstall when needed), then re-installs its cluster services so plist changes
  apply, then restarts itself. exo is restarted only when its LaunchAgent content changed —
  that is the interruption the user accepted.
- `omm update` on the head (git-clone install, `cluster.json` role head, at least one known
  member) prints, after a successful update:
  "N other Mac(s) in the cluster will update to this version within a few minutes. Cluster
  models may stop answering briefly while they do; the head loads them again automatically."
  (Wording is the builder's; it must say *how many Macs* and *that models may drop briefly*.)
  autoload already reloads kept models once every known Mac is back.
- `omm cluster status` on the head lists each Mac's omm version and marks members that differ
  or are blocked.

**Install methods.** Version follow works only when both head and member are git-clone
installs (`cli.SRC_DIR` editable, as `omm update` already detects). A member on PyPI/brew
/portable never self-updates; it reports its version and status says "update omm on <Mac>
by hand". The head on a non-git install serves `version: null` and members leave themselves
alone.

## Piece 2 — RAG backup

- The head's document store is `OMM_HOME/documents/library.sqlite3` (WAL), including the ACL
  tables (`rag_acl.py`: collections, shares, teams).
- The snapshot is taken with SQLite's backup API (consistent while the gateway writes).
- A member stores it **separately** from its own store, at
  `OMM_HOME/cluster/rag-backup/library.sqlite3` (owner-only permissions, written atomically).
  A member's own local `omm rag` documents are never touched.
- The change marker must change on every write to the store and be cheap to compute (builder:
  e.g. SQLite `PRAGMA data_version` is per-connection and unsuitable; a counter or the max of
  modification timestamps in the tables, or a hash of the WAL-checkpointed file).
- `omm cluster status` on the head reports, per member, how old its copy is.
- `leave` deletes the member's backup copy (the Mac is no longer trusted with it).

## Piece 3 — head handover

**Command.** `omm cluster handover <mac>`, run on the **current head**. `<mac>` is the exo
node name shown by `omm cluster status`. The new Mac must already be a joined member.
(Naming is the designer's call — see "Calls made".)

**Prerequisite that removes sudo from the handover.** Becoming head today needs sudo twice:
pf must allow the gateway port, and the Application Firewall must allow the gateway's Python.
Neither can be done by a background service. So `join` prepares every member exactly like a
head for these two: pf allows port 52440 and ALF allows the gateway binary on every cluster
Mac. A member has nothing listening on 52440, so the open port only answers with a reset.
Existing members get this on their next `join` re-run; until then `status` reports their
firewall rules as drift (as it already does for any rule difference) and `handover` refuses
that Mac with "run omm cluster join again on <mac> first".

**Steps** (each must be safe to re-run after an interruption):

1. Pre-checks on the head: target is a known, online member; its version matches the head
   (piece 1); its RAG copy matches the head's current marker (piece 2) — if not, wait up to a
   short timeout for one more sync; it is prepared for the gateway (above); and when the head
   has documents, it reports document-search readiness (`cluster/document_search.py`:
   Ollama + embedding model). Any failure stops here with the fix, nothing changed.
2. Confirmation prompt that states what will happen and what the admin must redo
   (`--yes` skips it).
3. The head records a pending handover to `<mac>` with `head_epoch + 1` and the settings to
   carry: `models` (kept models with their sharding/macs), `known_nodes`, `gateway_port`.
4. The target's member link sees the instruction on its next poll (the head may shorten the
   poll interval by answering immediately). It installs the gateway service, swaps its RAG
   backup into place as its live `OMM_HOME/documents/library.sqlite3` (moving any existing
   local store aside to `library.sqlite3.before-handover-<timestamp>` and saying so in the
   handover log), writes `cluster.json` as head with the carried settings and the new epoch,
   removes its member service, and starts the gateway (which makes its own TLS CA, per #536).
5. The old head waits until the new head answers `/omm/member/role` as head with the new
   epoch, then steps down: stops its gateway service, deletes `tokens.json`, writes
   `cluster.json` as member, installs the member service. Its own `OMM_HOME/documents` store
   becomes its backup copy (moved to `cluster/rag-backup/`). Its TLS CA directory is kept
   (current `leave` behaviour).
6. Other members find the new head on their next poll (higher epoch wins).
7. The command prints what the admin must do now, in order:
   - on the new head: `omm cluster token add <name>` for each employee, **using the same names
     as before** (RAG ownership and shares are keyed by token name). Print the old names list.
   - give each employee the new address and token; each employee runs
     `omm rag remote set <new address>` (accepts the new CA fingerprint), then
     `omm setting agent cluster` again if they had used it.

**Failure handling.** If step 4 never completes (target went offline), the old head stays head
and the pending instruction expires after a timeout; the command says so. If the old head dies
between steps 4 and 5, the new head is already serving, and the old head comes back still
thinking it is head (a head runs no member link). So the **gateway of a head checks peers for
a higher epoch at start-up and every few minutes, and steps itself down if it finds one**.
It asks peers over Thunderbolt and the LAN alike. This is best effort: a head that can see no
other Mac at all keeps serving, because a one-Mac cluster and a briefly unplugged cable must
keep working. The leftover case (old head back with no cable *and* no LAN path to any peer)
is reported by `status` on the new head as "another head was seen" when it does reappear.

## Calls made by the designer (not the user — check these)

- Order: version follow → RAG backup → handover.
- Members poll the head (no new listening service on members), every 60 s.
- Members only move forward; a member ahead of the head waits for the head to update.
- Members switch to the head's update channel.
- Member credential derived from the namespace; all cluster Macs trust each other equally.
- Member traffic prefers Thunderbolt; LAN fallback only over HTTPS bound to the mutual proof.
- "Never two heads" is best effort (below).
- Handover is run on the old head, named `omm cluster handover <mac>`.
- Every Mac gets the gateway port + ALF entry at `join`, so the handover needs no sudo.
- Old tokens are deleted on step-down; new tokens must reuse old names.
- A member's backup copy lives apart from its own documents; a new head's previous own
  documents are moved aside, not merged.
- Non-git installs are reported, never auto-updated.

## Testing

- Unit tests with fakes for: member-credential derive/verify both directions; head discovery
  with two heads (epoch wins); version follow refusing an unsigned or off-channel commit;
  backward follow; snapshot marker change detection; handover step re-runs after an
  interruption at each step; step-down of a stale head on seeing a higher epoch.
- Real-rig verification on the 3-Mac mini rig (node1/3/4): update the head and watch both
  members follow (and the warning); add a document on the head and see the copies update;
  hand over node4 → node1 and back, with an employee PC re-running `omm rag remote set`.
  Follow the repo's isolated-install rules; never touch the user's daily-driver `omm`.
- Members only follow signed commits on the channel branch, so the end-to-end "head updates,
  members follow" check runs after the feature is merged into `beta`. There is no dev bypass
  (it would be a way around the trust check). RAG backup and handover can be verified on the
  rig from the feature branch, installed by hand.

## Docs to update when built

`README.md` Usage + `docs/commands.json` (new `cluster handover`), `CLAUDE.md` Cluster
section (member link, LaunchAgent `com.omm.cluster-member`, `/omm/member/*`, backup path),
`PRIVACY.md` if it describes where documents are stored.
