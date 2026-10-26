# omm cluster CLI redesign: model verbs that match omm

Date: 2026-10-26. Status: design, awaiting approval.
Builds on: `2026-10-22-exo-cluster-design.md`, local branch `feat/cluster-run` (2819b18, unpushed).

## Why

`omm cluster model` does three unrelated jobs behind one verb: with no argument it
recommends, with a model id it downloads *and* loads, with `--unload` it unloads. The user
found it confusing, and in particular could not see why "loading" is a step at all.

The reason is exo, not omm: exo never loads a model on demand. A request for a model
with no running instance fails. Loading means (1) on first use, downloading the whole model
on every Mac (hours for a big one), (2) placing layers into every Mac's unified memory,
(3) wiring the MLX ring. It takes minutes and then holds memory until unloaded.

The goal is that a cluster model is handled with the same verbs, in the same order, as a
local GGUF: find it, install it, run it. Loading becomes something `run` does for you,
with `load`/`unload` kept for the admin who wants to serve a model without chatting.

## Goals

- `omm cluster` model verbs mirror top-level `omm`: `recommend`, `search TEXT`,
  `install MODEL`, `uninstall MODEL`, `list`, `info MODEL`, `benchmark MODEL`, `run [MODEL]`.
- Separate download (`install`) from loading into memory (`run` auto-loads; `load`/`unload`
  explicit).
- `omm run` and `omm list` also understand cluster models, so a user does not have to
  remember which world a model lives in.
- Memory shortage obeys the existing `omm setting memory-guard` policy.

## Non-goals

- No change to `init`, `join`, `status`, `restart`, `token`, `leave` (user: these are fine).
- No new gateway routes that let an employee token install, load, unload or benchmark.
- No upload of cluster benchmark numbers to telemetry. Cluster rows have no
  `featurize.py` shape and would poison the single-machine training set. (Claude's call.)
- No hidden alias for `omm cluster model`. It is deleted outright (repo rule: when
  consolidating, fully delete the old command).

## Anti-goals (would be a failure even if it "works")

- Silently unloading a model an employee is using. Never unload without either the
  `ask` policy's explicit yes or an explicit `unload`/`uninstall`.
- `omm run` / `omm list` getting slow or failing on a PC that has nothing to do with a
  cluster. The cluster lookup must be skipped entirely when this machine is neither in a
  cluster nor has `omm rag remote` set, and must time out fast and degrade to "local only"
  otherwise.
- Changing `omm list --json` output in any way.

## Who can run what

| Command | Cluster Mac (head or member) | Employee PC (token via `omm rag remote set`) |
|---|---|---|
| `recommend`, `search`, `install`, `uninstall`, `load`, `unload`, `info`, `benchmark` | yes | no: clear error "run this on a cluster Mac" |
| `omm cluster list` | yes, everything | yes, loaded models only |
| `omm cluster run [MODEL]` | yes, auto-loads if installed | yes, loaded models only; not loaded -> "ask the cluster admin to run `omm cluster load MODEL`" |
| `omm run MODEL`, `omm list` | local + cluster | local + loaded cluster models |

Employee-side data comes only from the gateway's existing `ALLOWED_ROUTES` plus
`GET /omm/loaded-models` (added on `feat/cluster-run`). Decided by the user.

Mutating commands (`install`, `uninstall`, `load`, `unload`, `benchmark`, and `run`'s
auto-load) are **head-only**, like `token`. Reason: the reboot-reload target
(`ClusterSettings.model`) lives only on the head (`load_model` saves it only there,
`autoload.check_once` runs only there), so an unload from a member Mac would be undone by
the head's autoload within minutes. A member Mac gets `run this on the head Mac (<name>)`.
Read-only commands (`recommend`, `search`, `list`, `info`, `run` on a loaded model) work on
any cluster Mac. (Claude's call.)

The gateway's `GET /omm/loaded-models` (unpushed, `feat/cluster-run`) is extended from
`{"models": [id, ...]}` to `{"models": [{"model", "size_bytes", "nodes_installed",
"nodes_total", "state"}]}` so an employee's `omm cluster list` / `omm list` show the same
columns. Same route, same auth, still only fields derived from `/state`. `chat.loaded_models`
is updated in the same change. (Claude's call.)

## Commands

All read the local exo API (`http://127.0.0.1:52415`) through `exo_client.ExoClient`.
Model ids are exo/HF ids such as `mlx-community/Qwen3-0.6B-4bit`.

### `omm cluster recommend`
What `omm cluster model` with no argument does today (`commands.candidates`, `recommend.rank`):
the biggest exo models whose best placement fits under `HEADROOM` of free memory. Mark each
row installed / loaded. Last line: `Install one with: omm cluster install <model>`.

### `omm cluster search TEXT`
exo `GET /models/search?query=TEXT&limit=20` (HF search, seen in the EXO.app dashboard
bundle). Show id, size, quantization, and whether it would fit right now. A result that is
not in exo's built-in catalog (`GET /models`) must be added with `POST /models/add` before
it can be installed; `install` does this itself, `search` does not change anything.
Response shape is unverified: capture a live fixture first.

### `omm cluster install MODEL`
Download to every cluster Mac without loading. Show the same progress lines `load_model`
prints today (slowest Mac's bytes, per-Mac failure). On success:
`Installed MODEL on N Macs. Chat with it: omm cluster run MODEL`.

exo API: `POST /download/start {targetNodeId, shardMetadata}` per node, progress from
`/state` `downloads` (`exo_client.load_progress` already parses these entries).
**Verified on the rig 2026-10-26 (exo on node4, 3 Macs):** `/state` `downloads` lists a
`DownloadPending` entry for every catalog model on every node, each carrying the full-range
`shardMetadata` (`startLayer 0`, `endLayer nLayers`, `worldSize 1`, camelCase `modelCard`).
POSTing that entry's `shardMetadata` with `targetNodeId` to `/download/start` (via the head's
loopback API) downloaded the model on node1 only (Pending -> Ongoing -> Completed in ~30 s
for 0.7 GB). `DELETE /download/{nodeId}/{url-encoded model id}` turned it back into Pending
and removed `~/.exo/models/<org>--<name>` on node1. So "installed on a Mac" = a
`DownloadCompleted` entry. Original note: Live `/state` (fixture
`downloads-live.json`) shows exo downloading the whole model per node as a
`PipelineShardMetadata` with `startLayer: 0`, `endLayer: nLayers`, `worldSize: 1` and the
full camelCase `modelCard`. Verify that a full-range shard per node (a) is accepted,
(b) yields a download that a later instance creation reuses without re-downloading. If (b)
fails, stop and report; do not fall back to "create instance, wait, delete instance"
without asking the user.

Already installed on every Mac -> say so, exit 0. Installed on some Macs -> download on the
rest. A Mac that joins later does not get it automatically; `run`/`load` already download
on demand (existing exo behaviour), and `info` shows which Macs are missing it.

### `omm cluster uninstall MODEL`
`DELETE /download/{nodeId}/{model}` for every node that has it. If the model is loaded,
unload it first; this is an explicit request so no memory-guard check, but if a terminal is
attached, confirm "MODEL is loaded and may be in use; unload and delete it?" (same
`_ask_confirm` as local uninstall). Non-TTY without `--yes`: refuse.

### `omm cluster list`
One row per installed model: id, size, Macs that have it (`3/3`), state
(`loaded` / `loading` / `installed` / `downloading NN%`). Employee PC: loaded models only.
`--json` emits a list of objects with fixed keys (`model`, `size_bytes`, `nodes_installed`,
`nodes_total`, `state`).

### `omm cluster info MODEL`
Model card from `/models` (family, base model, quantization, context length,
capabilities, size), per-Mac download state, whether it is loaded and with which sharding,
and the best placement now (`recommend.choose_placement` over `/instance/previews`): fits /
does not fit, peak per-Mac memory. `--json` supported.

### `omm cluster load MODEL` / `omm cluster unload MODEL`
`load` = today's `commands.load_model` (placement choice, progress, stall timeout,
remembers the model for `autoload.py`), plus the memory-guard check below. Not installed
-> it still works (exo downloads first), but print one line that `install` would have done
the download separately. `unload` = today's `commands.unload_model`.

### `omm cluster run [MODEL]`
Existing `feat/cluster-run` chat (`cluster/chat.py`), plus: on a cluster Mac, if MODEL is
installed but not loaded, run `load` (with memory guard) first, then chat. Installed on only some Macs ->
auto-load anyway (exo downloads the rest) after one line saying how many Macs still need
the download. Not installed on any Mac ->
error `MODEL is not installed; run: omm cluster install MODEL` (do not start an hours-long
download from a chat command; Claude's call, mirrors `omm run` refusing uninstalled models).
No MODEL -> existing `chat.pick_model` (the only loaded model, else ask).

### `omm cluster benchmark MODEL`
Must be loaded (or auto-load like `run`). Send a fixed prompt through the local exo
`/v1/chat/completions` (streaming), measure time to first token and decode tok/s, report
sharding and Mac count. Local display only, no upload (see Non-goals). Reuse
`benchmark.py`'s prompt/measurement helpers where they are engine-agnostic.

## Memory guard (user decision)

Before `load` / `run` auto-load / `benchmark` auto-load creates an instance, "fits" means
`recommend.choose_placement` finds a usable placement in `/instance/previews`. Verified on
the rig 2026-10-26 with Qwen3.5-9B loaded: exo builds previews from each Mac's current
`ramAvailable` (which already excludes loaded models) and marks every row of a model that
does not fit with `error: "No cycles found with sufficient memory"`. If it fits, go.
To decide which loaded models to offer for unloading, omm estimates: free memory summed over
Macs plus the sizes of the models it would unload must reach the new model's size; after
unloading it asks exo for previews again. If it
does not and other models are loaded, apply `memory_guard_policy`:

- `block`: refuse. `Memory Guard blocked the load: MODEL needs X GiB on the busiest Mac;
  loaded: A (Y GiB). Unload one with: omm cluster unload <model>`.
- `ask` (default): TTY -> "Unload A to make room for B? A may be in use by other people."
  With several loaded models, offer the fewest (largest first) whose memory makes B fit,
  named in one prompt. yes -> unload them, re-check placement, load B. No TTY -> refuse like `block`.
- `observe`: print the warning, try anyway, and show exo's refusal as-is if it refuses
  (user picked this over auto-unloading). Note: exo refuses a placement that does not fit
  rather than swapping (MLX buffers are wired, not pageable), so in practice this ends in
  exo's error. The exact error text exo returns is unverified; capture it on the rig.

Reuse `memory_guard.GuardPolicy` / `normalize_policy`; the planner itself is new because
exo, not `scan_hardware()`, knows per-Mac free memory.

## `omm run` and `omm list` (user decision: support both)

- `omm list`: after the local table, a `Cluster` section when this machine is in a cluster
  or has `omm rag remote` set. Same columns as `omm cluster list`. If the cluster cannot be
  reached within ~2 s, print one muted line `cluster unreachable` and keep the exit code 0.
  `--json` is unchanged: local entries only, exactly today's shape. Machine-readable
  cluster data comes from `omm cluster list --json`. Cluster rows get no numbered refs in
  `session_cache` (`omm run 3` stays local-only). (User decision.)
- `omm run MODEL`: resolve locally first (`registry` / `_lookup_entry`). Not found and the
  cluster is configured -> treat as `omm cluster run MODEL`. `omm run` with no MODEL keeps
  today's local picker; cluster models are listed in the picker only when no local model
  exists (Claude's call; keeps the common case unchanged).
- Tab completion for `omm run` stays local-only (no network on Tab).

## Autoload

`settings.ClusterSettings.model` (one id) stays the "reload after reboot" target. `load`
and `run`'s auto-load set it; `unload`/`uninstall` of that model clear it, as today. With
more than one model loaded only the last one comes back after a reboot. Extending it to a
list is out of scope (Claude's call).

## Removed

- `omm cluster model` command and every message that tells the user to run it
  (`commands.load_model` failure text, README, docs).

## Docs and checks (repo rules)

- `src/omm/cli.py` changes -> `python scripts/export_command_reference.py`, commit
  `docs/commands.json`; add every new command to `README.md` `## Usage` in
  `omm cmd [flags]  # description` form; `python scripts/check_docs_sync.py`.
- Update CLAUDE.md "Cluster" paragraph (`cluster.json model` is set by `load`/`run`,
  not `omm cluster model`).
- Base on `feat/cluster-run`, which itself is off `beta`.

## Verification

- Unit tests with fixtures in `tests/fixtures/exo/` for: install request body, download
  progress, uninstall order (unload then delete), every memory-guard branch, employee-PC
  refusal of mutating commands, `omm list` with cluster unreachable, `omm run` fallthrough.
- Live on the 3-Mac rig (`project_omm_cluster_three_mini_rig_2026-10-23`): install a small
  model, confirm `run` reuses it without re-downloading, uninstall it and confirm the files
  are gone on each Mac, trigger each memory-guard policy with two models that do not both
  fit, and record exo's actual refusal text into a fixture.
