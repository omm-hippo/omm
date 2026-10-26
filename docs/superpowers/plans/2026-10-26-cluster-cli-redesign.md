# omm cluster CLI redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `omm cluster model` with omm-style verbs (recommend/search/install/uninstall/list/info/load/unload/benchmark), make `omm cluster run` auto-load, and let `omm run` / `omm list` reach cluster models.

**Architecture:** exo wire shapes stay in `cluster/exo_client.py`; new `cluster/models.py` holds install/uninstall/inventory/info/search/memory-guard logic (cli.py only renders); `cluster/bench.py` measures one streamed answer. The gateway's `/omm/loaded-models` grows from ids to rows so employee PCs can list.

**Tech Stack:** Python 3.10+, Typer, pytest; exo 1.0.71 loopback API.

**Spec:** `docs/superpowers/specs/2026-10-26-cluster-cli-redesign-design.md`

## Global Constraints

- `omm cluster model` is deleted outright; no alias. Every user-facing string naming it is rewritten.
- Mutating commands (`install`, `uninstall`, `load`, `unload`, `benchmark`, `run` auto-load) are head-only: `commands.require_head()`-style check with message `run this on the head Mac`.
- `omm list --json` output is byte-for-byte unchanged; cluster JSON only via `omm cluster list --json`.
- Memory guard policy comes from `load_config()["memory_guard_policy"]` via `memory_guard.normalize_policy`.
- exo refusal text: `No cycles found with sufficient memory` (rig 2026-10-26).
- No benchmark upload. No new gateway routes.
- `questionary`/`requests` stay lazy-imported; cluster modules imported inside command functions.
- After CLI changes: `python scripts/export_command_reference.py`, README `## Usage`, `python scripts/check_docs_sync.py`.
- Tests: `PYTHONPATH=$PWD/src python -m pytest ...` from the worktree (shared venv imports the main checkout otherwise).

## Review Focus

1. Employee PC with `omm rag remote` set but head unreachable: `omm list` must still print local models and exit 0 within ~2 s (test in Task 6).
2. Machine with no cluster and no remote: `omm list` / `omm run` must make zero network calls (test in Task 6).
3. `uninstall` of the model the head autoloads: must clear `cluster.json` `model` or autoload reloads it (test in Task 2).
4. Head runs an older gateway that returns `{"models": ["id", ...]}`: employee `list`/`run` must still work (test in Task 5).
5. `ask` policy without a TTY: refuse, never unload (test in Task 3).

---

### Task 1: exo_client download/search/add APIs

**Files:**
- Modify: `src/omm/cluster/exo_client.py`
- Create: `tests/fixtures/exo/downloads-mixed.json` (one node with Pending+Completed entries, scrubbed from rig `/state`), `tests/fixtures/exo/search.json`, `tests/fixtures/exo/previews-nomem.json`
- Test: `tests/test_cluster_exo_client.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) NodeDownload(node_id: str, model_id: str, kind: str, done_bytes: int, total_bytes: int, shard_metadata: dict)`; `kind` is exo's tag without `Download` (`Pending|Ongoing|Completed|Failed`).
  - `parse_downloads(data: dict) -> list[NodeDownload]`
  - `@dataclass(frozen=True) SearchHit(model_id: str, downloads: int, tags: tuple[str, ...])`; `parse_search(data) -> list[SearchHit]`
  - `ExoModel` gains `card: dict = field(default_factory=dict, hash=False, compare=False)` (raw `/models` row).
  - `ExoClient.start_download(node_id: str, shard_metadata: dict) -> None` (`POST /download/start {targetNodeId, shardMetadata}`)
  - `ExoClient.delete_download(node_id: str, model_id: str) -> None` (`DELETE /download/{quote(node)}/{quote(model, safe='')}`)
  - `ExoClient.search(query: str, limit: int = 20) -> list[SearchHit]`
  - `ExoClient.add_model(model_id: str) -> None` (`POST /models/add {model_id}`)
  - `NO_MEMORY = "No cycles found with sufficient memory"`

- [ ] Step 1: tests `test_parse_downloads_reads_every_kind` (fixture: Completed has `total_bytes == done_bytes == 712593855`, Pending carries `shard_metadata["PipelineShardMetadata"]["endLayer"] == 16`), `test_parse_search_reads_ids_and_tags`, `test_start_download_posts_target_and_shard` / `test_delete_download_quotes_model_id` (fake `_http.request` records method, path, payload; expect path `/download/n1/mlx-community%2FLlama-3.2-1B-Instruct-4bit`).
- [ ] Step 2: run, expect FAIL (names undefined).
- [ ] Step 3: implement. Reuse `_download_entry` tagging logic for `parse_downloads`.
- [ ] Step 4: `PYTHONPATH=$PWD/src python -m pytest tests/test_cluster_exo_client.py -q` PASS.
- [ ] Step 5: commit `feat(cluster): read exo downloads and add download/search API calls`.

### Task 2: models.py inventory, install, uninstall, info, search

**Files:**
- Create: `src/omm/cluster/models.py`
- Modify: `src/omm/cluster/commands.py` (move `candidates`, `load_model`, `unload_model` callers' head check; add `require_head_for(action: str)`)
- Test: `tests/test_cluster_models.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `@dataclass(frozen=True) ModelRow(model: str, size_bytes: int | None, nodes_installed: int, nodes_total: int, state: str)`; `state` ∈ `loaded|loading|installed|partial|downloading|failed`. `as_dict()` returns exactly those five keys.
  - `inventory(client) -> list[ModelRow]`: every model with ≥1 non-Pending download or an instance; sorted by model id.
  - `install(model_id: str, out: Out, client=None) -> int` returns Macs newly started; adds via `add_model` when id not in `client.models()`, re-reads `/state`, POSTs each node's Pending entry; waits with progress like `load_model`'s report (slowest Mac %, 5 % steps) until every node is Completed; failure -> ClusterError naming the Mac; stall `commands.MODEL_STALL_TIMEOUT`.
  - `uninstall(model_id: str, out: Out, client=None) -> int` unloads instances first (`commands.unload_model`, which also clears `cluster.json model`), then `delete_download` on every node with a non-Pending entry; returns node count; 0 nodes and no instance -> ClusterError `MODEL is not installed on this cluster`.
  - `info(model_id: str, client=None) -> dict` keys: `model, card, size_bytes, nodes: [{name, state}], loaded, sharding, fits, peak_node_bytes`.
  - `search(query: str, client=None) -> list[dict]` keys `model, downloads, in_catalog, size_bytes, fits`.
  - `commands.require_head_for(action)` raises UsageProblem `"{action} runs on the head Mac; this Mac is a member"` / not in cluster message.

- [ ] Step 1: tests with a fake client: `test_inventory_states` (loaded/partial/installed), `test_install_starts_only_missing_nodes`, `test_install_adds_unknown_model_first`, `test_install_reports_failed_mac`, `test_uninstall_unloads_then_deletes_and_clears_kept_model` (Review Focus 3), `test_uninstall_not_installed_errors`, `test_info_fits_from_previews`, `test_search_marks_catalog_and_fit`.
- [ ] Step 2: run FAIL.
- [ ] Step 3: implement.
- [ ] Step 4: PASS.
- [ ] Step 5: commit `feat(cluster): install, uninstall, list and inspect cluster models`.

### Task 3: memory guard for cluster loads

**Files:**
- Modify: `src/omm/cluster/models.py`, `src/omm/cluster/commands.py` (`load_model` takes `guard`)
- Test: `tests/test_cluster_models.py`

**Interfaces:**
- Produces: `ensure_loaded(model_id: str, out: Out, *, policy: memory_guard.GuardPolicy, confirm: Callable[[str], bool] | None, client=None) -> str` returns the serving line. No usable placement and some row error contains `NO_MEMORY`:
  - `BLOCK`, or `ASK` with `confirm is None`: ClusterError `Memory Guard blocked the load: {model} does not fit next to {loaded list with GiB}. Free memory with: omm cluster unload <model>`.
  - `ASK`: pick victims = loaded models (excluding target) largest first until `sum(free) + sum(victim sizes) >= target size`; `confirm("Unload A, B to make room for X? They may be in use by other people.")`; yes -> `unload_model` each, then previews again; still no placement -> ClusterError.
  - `OBSERVE`: out warning line, then create the instance anyway with exo's error surfaced: ClusterError `exo refused: No cycles found with sufficient memory`.
  - No error rows at all but no placement -> existing `does not fit` error.
  - Already loaded -> returns at once (existing wait path).
- [ ] Step 1: tests `test_guard_block_refuses`, `test_guard_ask_without_tty_refuses` (Review Focus 5), `test_guard_ask_unloads_largest_first_then_loads`, `test_guard_ask_declined_keeps_models`, `test_guard_observe_shows_exo_error`.
- [ ] Step 2–4: FAIL, implement, PASS.
- [ ] Step 5: commit `feat(cluster): apply memory guard before loading a cluster model`.

### Task 4: CLI verbs; delete `cluster model`; `run` auto-load

**Files:**
- Modify: `src/omm/cli.py` (cluster section ~2545-2630), `src/omm/cluster/chat.py` (`pick_model` message), `src/omm/cluster/gateway.py` (`_not_loaded_message`), `src/omm/cluster/commands.py` (failure texts), `src/omm/cluster/settings.py` comment, `src/omm/cli.py:798`
- Test: `tests/test_cli_cluster.py` (rewrite `test_model_*` to new verbs), `tests/test_cluster_chat.py`, `tests/test_cluster_gateway.py`

**Interfaces:**
- Commands (all `@global_flags`, `--json` where listed):
  - `recommend` (--json), `search TEXT` (--json), `install MODEL`, `uninstall MODEL [--yes/-y]`, `list` (--json), `info MODEL` (--json), `load MODEL`, `unload MODEL`, `run [MODEL]`, `benchmark MODEL` (Task 7).
  - Help one-liners avoid any word ending in "." mid-sentence (click truncation).
  - `run` on a cluster Mac: model given, installed (≥1 node Completed), not loaded -> head check, `ensure_loaded`, then chat. Not installed anywhere -> exit 1 `MODEL is not installed; run: omm cluster install MODEL`.
  - `uninstall` loaded model: TTY confirm; non-TTY without `--yes` -> exit 2.
  - Not-loaded hints everywhere become `omm cluster load <model>`; recommend footer `Install one with: omm cluster install <model>`; install success `Installed MODEL on N Mac(s). Chat with it: omm cluster run MODEL`.
- [ ] Step 1: tests: `test_cluster_model_command_is_gone` (exit 2, "No such command"), `test_recommend_lists_candidates`, `test_install_cli_progress`, `test_load_prints_gateway_url`, `test_unload_deletes`, `test_list_json_keys`, `test_member_refuses_install` ("runs on the head Mac"), `test_run_autoloads_installed_model`, `test_run_refuses_uninstalled_model`, `test_uninstall_loaded_needs_yes_without_tty`; update chat/gateway message assertions to `omm cluster load`.
- [ ] Step 2–4: FAIL, implement, PASS on `tests/test_cli_cluster.py tests/test_cluster_chat.py tests/test_cluster_gateway.py`.
- [ ] Step 5: commit `feat(cluster)!: split omm cluster model into install, load and friends`.

### Task 5: employee-side list via gateway rows

**Files:**
- Modify: `src/omm/cluster/gateway.py` (`_reply_loaded_models` -> `{"models": [ModelRow.as_dict() for loaded rows]}`), `src/omm/cluster/chat.py` (`loaded_models` accepts str or dict rows; new `loaded_rows(point) -> list[ModelRow]`)
- Test: `tests/test_cluster_gateway.py`, `tests/test_cluster_chat.py`

- [ ] Step 1: tests `test_loaded_models_route_returns_rows`, `test_chat_accepts_old_id_list` (Review Focus 4), `test_employee_cluster_list_shows_loaded_rows`.
- [ ] Step 2–4: FAIL, implement, PASS.
- [ ] Step 5: commit `feat(cluster): show loaded cluster models on employee PCs`.

### Task 6: `omm list` cluster section and `omm run` fallthrough

**Files:**
- Modify: `src/omm/cli.py` (`list_models` text path only; `run`), `src/omm/cluster/chat.py` (`cluster_configured() -> bool` = `settings.load() is not None or rag_remote.load() is not None`, no network)
- Test: `tests/test_cli_cluster.py`

- `omm list` (non-JSON): if `cluster_configured()`, print `Cluster` heading + rows from `chat.loaded_rows` on a remote / `models.inventory` on a cluster Mac, with `LIST_TIMEOUT` 2 s for this call; any ClusterError -> one muted line `Cluster: unreachable (<reason>)`, exit unchanged. The empty-local-registry early return must not skip the cluster section.
- `omm run MODEL`: `_lookup_entry` miss and `cluster_configured()` -> delegate to the same body as `cluster_run_cmd`. Extract that body into `_cluster_chat(model: str | None)`.
- [ ] Step 1: tests `test_list_json_unchanged_with_cluster`, `test_list_shows_cluster_section`, `test_list_cluster_unreachable_exit_0` (Review Focus 1), `test_list_no_cluster_no_network` (monkeypatch `chat.loaded_rows`/`models.inventory` to raise AssertionError; Review Focus 2), `test_run_falls_through_to_cluster`.
- [ ] Step 2–4: FAIL, implement, PASS; also `python -m pytest tests/test_cli*.py -q -k "list or run"`.
- [ ] Step 5: commit `feat(cli): show and run cluster models from omm list and omm run`.

### Task 7: `omm cluster benchmark`

**Files:**
- Create: `src/omm/cluster/bench.py`
- Modify: `src/omm/cli.py`
- Test: `tests/test_cluster_bench.py`

**Interfaces:**
- `measure(point: chat.Endpoint, model: str, prompt: str = PROMPT, clock=time.monotonic) -> dict` keys `model, ttft_seconds, decode_tokens_per_second, output_tokens`; streams `/v1/chat/completions` with `stream_options: {include_usage: true}`; token count from the final `usage.completion_tokens`, else count of content+reasoning pieces (note which in key `token_source`: `usage|pieces`).
- CLI: head-only; auto-load via `ensure_loaded`; prints `TTFT 0.42 s, 38.1 tok/s (412 tokens, Pipeline on 3 Macs)`; `--json`. Never uploads.
- [ ] Step 1: tests with a fake stream: usage path and pieces fallback, TTFT from first piece.
- [ ] Step 2–4: FAIL, implement, PASS.
- [ ] Step 5: commit `feat(cluster): add omm cluster benchmark`.

### Task 8: docs

**Files:** `README.md` (`## Usage` cluster lines), `docs/commands.json` (regenerated), `CLAUDE.md` Cluster paragraph, `docs/demo-scenarios.md` C5 row, `docs/pitch-message.md` command name.
- [ ] `python scripts/export_command_reference.py && python scripts/check_docs_sync.py` -> OK.
- [ ] `git grep -n 'cluster model' -- ':!docs/superpowers'` -> no hits.
- [ ] Full suite: `PYTHONPATH=$PWD/src python -m pytest -q` (FORCE_COLOR failures pre-existing; compare with base).
- [ ] commit `docs(cluster): document the new cluster model commands`.

### Task 9: rig verification (node4 head, real exo)

Rsync the branch to a scratch dir on node4 (never touch `~/omm-verify/src` someone else owns: use `~/omm-verify/cli-redesign` + its own venv, `OMM_HOME=~/omm-verify/home` copy of cluster.json), then:
- `install mlx-community/Qwen3-0.6B-4bit` (on no Mac yet, 327 MB) -> 3/3 Completed. Use only this model for install/uninstall so models others downloaded stay.
- `list`, `info`, `search qwen3`, `recommend`.
- `run` with stdin `hi\n/bye` -> auto-loads without re-download (no Ongoing entries appear).
- `benchmark` the same model.
- memory guard: with policy `block`, `load` a model that does not fit (`mlx-community/Step-3.5-Flash-6bit`) -> refused, nothing unloaded.
- `uninstall` it -> Pending on all nodes; leave Qwen3.5-9B instance as found.
Record outcomes in the PR body.
