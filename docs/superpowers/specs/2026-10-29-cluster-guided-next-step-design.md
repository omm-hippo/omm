# `omm cluster`: tell the user the next step, then run it

Date: 2026-10-29. Status: design, not implemented. Base branch: `beta`.

## Why

On 2026-10-29 a fourth Mac mini (Node2) was added to the three-Mac cluster
(node4 head, node1, node3). Working out what to do and in which order took
ssh sessions, `omm cluster status` on the head, the source code, and notes
from earlier rig sessions. A first-time admin has none of that. Every step
the admin needed is something omm can detect from data it already reads,
but today omm only prints a flat `Problems:` list, and part of that list was
wrong:

- Head status said `Node2의 Mac mini has not checked in; run omm cluster join
  <name> again on it`. The real cause: Node2's new exo was replaying the
  cluster's whole event log from index 0 (head at 1,667,908; Node2 at
  ~30-360 events/s, so hours). Until it catches up, Node2's local exo
  `/state` is an old view of the cluster: no entry for Node2 itself, old node
  ids, old IP addresses. `member_link.own_node_id` therefore finds nothing
  and logs `cluster_member_no_node` every 60 s. Running join again would not
  have helped.
- Node2 came with macOS 26.7.2 (25G332); the rest run 26.7.1 (25G241).
  Status reported the difference but not what to do (a Mac cannot be
  downgraded, so the others must be updated).
- The head's LAN address changed overnight (DHCP: 10.80.119.212 ->
  10.80.117.117). Nothing said so; every employee PC pinned to the old URL
  would have stopped working.

The user's words: "omm cluster에서 지금 뭐 해야하는지 안 나오니까 뭘 해야
하는지 찾아봐야 하잖아. ... 처음하면 삽질을 좀 할 것 같아서."

## Goals

1. Bare `omm cluster` looks at this Mac and the cluster, shows the next
   thing to do, and after the user agrees runs it, then looks again.
2. It works best in the two places the user named: a **new Mac** that is not
   in a cluster yet, and the **head**. A member gets the same behavior for
   its own steps.
3. Advice is never wrong in the way "join again" was: a state that resolves
   by waiting is shown as waiting, with progress.

## Non-goals

- No `omm cluster setup` command. The user agreed bare `omm cluster` is the
  feature (same pattern as bare `omm setting`). Help stays at
  `omm cluster --help`.
- No running anything on another Mac. When the fix belongs to another Mac,
  the head says which Mac and which command (user: "그럴 수 밖에 없잖아").
  The member link is not extended to carry new instructions (it may carry a
  new report, see "Member link reports catching up").
- omm does not set a fixed IP. A DHCP reservation is the network admin's
  job; a manual address (`networksetup -setmanual`) can collide with the
  router's pool.
- No automatic model reload when a Mac joins (the admin can still run
  `omm cluster load ... --macs N`; mentioning it as an optional hint is fine).

## Anti-goals (failure even if it "works")

- Telling the user to redo a step (join, init) when waiting is the answer.
- Running anything (sudo, restart, update, join) without an explicit yes.
- `omm cluster` and `omm cluster status` disagreeing about the same fact.
- A wizard that asks questions when the answer is detectable.

## Behavior

### Shape of every run

1. Detect the role: not in a cluster / head / member / employee PC
   (no `cluster.json` but `omm rag remote set` done; `chat.cluster_configured()`).
2. Collect facts (mostly what `commands.status()` already gathers).
3. Turn facts into an ordered list of **steps**. Each step has:
   - where: this Mac, or a named other Mac;
   - kind: `run` (omm can do it here), `manual` (a person must act on this
     Mac: screen prompt, Recovery mode, cable, macOS update), `wait`
     (resolves by itself; shows progress and, when known, an ETA),
     `tell` (must be done on another Mac or an employee PC);
   - one-line text and, for `run`/`tell`, the exact command.
4. Print a short header (role, cluster name, Mac count) and the steps, first
   step highlighted. If there are none: one line, "Nothing to do", plus the
   short summary `status` shows today.
5. In a terminal, for the first `run` step on this Mac: ask
   "Run it now? [Y/n]". Yes -> run it (it may ask for the sudo password like
   init/join do today) -> collect facts again -> repeat. Stop when the next
   step is not `run`, when the user says no, or when a step fails (show the
   error, keep the rest of the list).
   For a `manual` step on this Mac: say what to do, then
   "Press Enter when done" -> re-check. For `wait`: show progress and offer
   to keep watching (re-check every few seconds, Ctrl-C leaves).
6. Not a terminal (pipe, script): print the steps and exit 0; never prompt,
   never run anything (my call). `--json` prints the steps as data.

### Not in a cluster (new Mac)

Before asking anything, check this Mac: supported macOS, RDMA
(`rdma_ctl status`: `disabled` -> `manual` step with the existing
`preflight.RDMA_GUIDE`; missing binary -> macOS too old), omm up to date
with its channel.

Then ask: "Create a new cluster, or join an existing one?"

- Join: ask the user to paste the cluster name, saying where it is (bare
  `omm cluster` on the head prints it with the join command). Validate with
  `settings.NAMESPACE_RE`. Confirm, then run the existing `commands.join`.
- New: confirm, then run the existing `commands.init`.

After join/init the loop continues with the member/head steps below, so the
screen-prompt, cabling and restart steps follow in the same session:
on-screen Allow prompts (Gatekeeper and Local Network; existing
`LOCAL_NETWORK_FIX` text), cables (from exo's link list: which pairs have no
Thunderbolt link; full mesh means N-1 TB5 ports per Mac), then
`omm cluster restart` offered as a `run` step once cables changed.

### Member

Own steps only: the existing member-side checks (firewall, idle sleep, ALF,
member link running, Local Network/isolated exo) plus:

- **Catching up** (`wait`): this Mac's exo has not finished replaying the
  cluster's event log. Signals seen on Node2: local `/state` has no entry for
  this Mac's own node id (`GET /node_id` returns it) while other nodes are
  listed, `lastEventAppliedIdx` rising, `cluster-exo.log` lines
  `Requesting Event Log from N`. Show applied index and rate; ETA only when a
  target index is known (see open questions).
- Macs to fix elsewhere (e.g. macOS build differs) appear as `tell` steps
  naming that Mac.

### Head

`run` steps on the head: `omm update` when the head is behind its channel
(members follow the head, a newer member waits), `omm cluster restart`,
existing init re-run fixes (firewall, sleep, ALF, gateway stopped).

`tell`/`wait`/`manual` steps about other Macs:

- A Mac that has not checked in is split into: still catching up / within the
  existing `MEMBER_CHECKIN_GRACE_SECONDS` -> `wait`; truly silent -> `tell`
  with the command for that Mac. "join again" is only advised when the
  member's link is known not to be running, never as the default.
- macOS builds differ -> `manual` on each Mac that is not on the newest build
  ("update macOS on Node1 to 26.7.2 (25G332)"); newest because a Mac cannot
  go back (my call). Suggest one Mac at a time and a status check after each.
- RDMA disabled on a Mac -> `manual` on that Mac with `RDMA_GUIDE`.
- Missing Thunderbolt pairs -> `manual` cabling step naming both Macs.
- Member version ahead/blocked/manual -> existing texts, as steps.

**Head address changed.** Save the gateway URL the head last printed (in
`cluster.json`, next to `known_nodes`). When the current `gateway_url()`
differs: a `tell` step "The head's address changed from X to Y. On each
employee PC run `omm rag remote set https://Y:52440`" plus one line that a
DHCP reservation for the head (ask the network admin) prevents it. Init's
output and the head summary also say once that the address comes from
DHCP. Whether to offer the `.local` name instead of the IP is open (below).

### Employee PC

No steps to run. Show the existing employee status view and, when the head
is unreachable, say to ask the admin (and that the head's address may have
changed). Keep this small.

### `omm cluster status`

Stays, same output shape. Its problem texts come from the same checks as the
steps, so the two never disagree; in particular the "has not checked in;
run join again" text changes the same way as above.

## Constraints

- CLI startup speed (CLAUDE.md "CLI shape"): lazy imports inside the
  command; questionary only when prompting.
- Prompts follow existing helpers and their cancel semantics
  (`_ask_single_key`, `_ask_text`; see memory notes on Windows CI eager
  `questionary.select` construction).
- `omm cluster` with no args currently prints help (beta 227b9489, the
  `_DocsEpilogGroup` bare-group rule). `cluster_app` becomes a group with a
  default action (`invoke_without_command=True` callback), like `setting_app`.
- Docs: `docs/commands.json` regenerated, README `## Usage` line for
  `omm cluster`, `python scripts/check_docs_sync.py`; CLAUDE.md Cluster
  paragraph updated.
- Real-hardware verification on the Mac minis is expected for: catching-up
  detection, head address change, new-Mac join flow (CLAUDE.md "verify
  against the real running thing").

## Member link reports catching up (user decision 2026-10-29)

The user chose that a member keeps reporting to the head while its exo
replays the event log, so the head can tell "catching up" from "silent".
Today `member_link.run_once` stops at `no_node` before it polls, and even
with an id `find_head` dials addresses from the member's stale `/state`.
The builder must:

- get the member's own node id from exo `GET /node_id` (Node2 returned it
  while its `/state` had no entry for itself) instead of only matching
  local IPv4s against `/state`;
- reach the head even when the member's `/state` lists old addresses (for
  example: also try the head address the member last reached, saved
  locally, and/or the `.local` name; the builder picks and verifies on the
  rig);
- send a poll body that says `catching_up` with `lastEventAppliedIdx`;
  `member_auth` signing stays as is (it signs over the node id, which
  `/node_id` now supplies). No new trust: same namespace-derived key, same
  routes;
- on the head, record it in `members.json` and render `wait` with the
  member's index against the head's own `lastEventAppliedIdx` (this also
  answers open question 1: ETA from the two indices and the member's rate).

A member that is catching up skips version follow and RAG snapshot pulls
(my call: a replaying exo and an `omm update` restart should not compete).

## Verification (user decision 2026-10-29)

Claude runs the real-hardware checks on the Mac mini rig (ssh, sudo with the
password the user gives in chat, never stored), including taking one Mac out
with `omm cluster leave` and walking the new-Mac path back in. Paths not run
are reported as unverified.

## Calls made after the builder check (author's calls, not the user's)

- **Re-run fixes are narrow.** A `run` step calls only the one fix
  (`power.keep_awake()`, `firewall.apply`, `appfw.allow`, `service.install`,
  `member_service.install`), never all of `init`/`join`: `_prepare_this_mac`
  restarts exo, and on a catching-up member that may throw away replay
  progress. `status()` texts change to name the narrow fix.
- **Never offer `omm cluster restart` on a Mac that is catching up**, unless
  cables changed in this session.
- **Address change:** save the set of LAN addresses the gateway certificate
  covers (`tls.py`'s helper), not `lan_ipv4()` alone, so a VPN or second
  interface does not trigger it. The warning stays until the admin answers
  "employees updated" in a terminal; then the saved set is replaced. Non-tty
  runs only show it.
- **Version checks:** synchronous `git ls-remote` with a short timeout
  (about 5 s) only in a terminal run; offline or timeout -> the step is
  skipped with one "could not check for updates" line, never an error.
- **macOS with no `cluster.json` but `omm rag remote set` done:** show the
  employee view, then offer "Set this Mac up as a cluster Mac?" leading to the
  new-Mac path. Other platforms get the employee view only.

## Open questions

1. **ETA for catching up.** Answered by the member report above; the head
   computes it. A member's own `omm cluster` shows rate only unless the poll
   reply carries the head's index (builder's choice).
2. **`.local` name for employees.** The gateway certificate already covers
   the head's `.local` names. Not verified: `omm rag remote set` accepting a
   hostname, and Windows employee PCs resolving `.local`. If both work,
   init could print the `.local` URL and the address-change step would rarely
   fire.
3. **Why replay is so slow.** 1.67M events at 30-360/s means hours for every
   new Mac on a long-lived cluster, with ~74 master elections in 12 minutes
   on Node2. Possibly an exo-side issue (no snapshot); out of scope here but
   worth a separate issue.

## Left to the builder

- Module layout (suggested: `src/omm/cluster/guide.py` with a pure
  `steps(facts) -> list[Step]` function, tested without hardware; the CLI
  loop in `cli.py`).
- Whether `status()` is refactored to emit structured problems that both
  views render, or the guide builds on its own check functions shared with
  `status()`.
- Exact wording, colors, the JSON field names.
