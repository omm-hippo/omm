# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## PR 설명은 한국어로, 사람을 위해 쓴다 (필수)

팀원 모두 AI 에이전트로 개발하고 PR을 올린다. AI가 쓴 영어 PR 본문은 그 세션에 없던 사람이 읽으면
무슨 맥락에서 나온 변경인지 알 수 없다. 그래서 PR 본문은 **반드시** 아래 네 항목을 이 순서로,
한국어로, 개발자가 아닌 팀원도 읽을 수 있게 먼저 쓴다. 영어 기술 세부는 그 뒤에 붙여도 된다.
CI 체크 `PR 설명 확인`(`.github/workflows/pr-description-check.yml`)이 이 구조와 한국어 분량을 검사한다.

- `## 한줄 요약` — 이 PR이 무엇을 하는지 한 문장.
- `## 배경` — 왜 이 변경이 나왔는지: 어떤 이슈·버그·대화·리뷰에서 시작됐는지 (맥락).
- `## 무엇을 바꿨나` — 바꾼 것을 쉬운 말로. 함수·파일 이름은 꼭 필요할 때만.
- `## 어떻게 확인했나` — 실행한 명령과 결과. 못 해본 경로는 "미검증"으로 적는다.

커밋 제목은 영어 conventional 형식 그대로 둔다. PR 본문은 커밋 메시지의 번역이 아니라 "이 세션에
없던 사람에게 하는 설명"이다. `gh pr create --body-file`로 올릴 때도 같은 구조를 쓴다
(`.github/PULL_REQUEST_TEMPLATE.md`가 그 틀이다).

## 명령어를 바꾸면 문서 3종을 같이 맞춘다

- `src/omm/cli.py` 가 명령어의 단일 진실 공급원이다. 명령어를 추가·이름변경·설명변경하면
  `python scripts/export_command_reference.py` 를 돌려 `docs/commands.json` 을 다시 만들고 함께 커밋한다.
- `README.md` 의 `## Usage` 절에도 그 명령어를 같은 `omm cmd [flags]  # 설명` 형식으로 넣는다.
- `python scripts/check_docs_sync.py` 로 확인한다 (CI 잡 `docs-sync` 가 같은 검사를 한다).

## What this is

`omm` (Open source Model Manager) — an apt/brew-style CLI package manager for local GGUF LLMs.
Downloads a GGUF once into a central hub (`~/.omm/`, override `OMM_HOME`) and links it into every
installed local runner (Ollama, LM Studio, Jan, AnythingLLM, Msty, text-generation-webui, KoboldCpp)
without duplicating the file. Also ranks models against live hardware and verifies real local
generation.

- PyPI package `omm-model`; import package `omm`; entry points `omm` and `localfit-server`.
- Working dir is `/Users/shinmingyu/Project/Localfit`; the GitHub repo name is `omm` (do not confuse).
- Python 3.10+; CI test job pins 3.12, bare-runtime-install job pins 3.11. Stack: Typer CLI,
  Hatch build, `questionary` TUI, `cryptography`, `filelock`. `scikit-learn` is CI/training-only,
  never a runtime dependency.

## Repo, branches, workflow — read before any git operation

- Canonical remote is `origin` = `github.com/omm-hippo/omm`. `legacy` = `github.com/minigu5/Omm` is a
  frozen dead mirror that **must stay alive** (stranded installs still fetch it until they update past
  `355c60a`). Always run `git remote -v` before any `gh --repo` / `gh issue` / `gh pr` command — a
  stale org name from memory has caused issues filed on the invisible legacy repo.
- `main` is branch-protected: PR + 6 required CI checks, `enforce_admins` on, **direct push rejected**.
  Flow is always: `git checkout -b` → commit → push branch → `gh pr create` → 6 green → merge.
  Merge strategy is "create a merge commit" only (squash/rebase disabled — flattening breaks the
  SSH-signature chain that `omm update` verifies). If CI job names change, update the branch-protection
  `required_status_checks` contexts to match.
- `beta` is unprotected and should stay roughly caught up with `main`, but there is no automated
  enforcement — `sync-beta.yml` (auto-merge on push to `main`) and `branch-ancestry-check.yml` (the
  post-hoc ancestry gate) were removed 2026-09-13 because teammates push straight to `main` often
  enough that the auto-merge kept losing to version-bump conflicts (`pyproject.toml` /
  `packaging/npm/launcher/package.json` patch numbers diverging) and going red on every push. Port
  `main` into `beta` by hand with a local `git merge origin/main` → push when you notice drift.
  Most feature work targets `beta`.
- **Committing freely is fine; pushing is always a separate explicit ask.** Wait for it every time.
- The user runs multiple Claude sessions against this checkout at once. Re-check `git log -5` /
  `git status` right before committing. Only ever `git add <your own filenames>` — never `-A` / `.`.
  Never blind `git stash pop`/`drop` — `git stash list` and diff first; the stash stack is shared.
- `core.hooksPath = scripts`. `scripts/pre-commit` auto-bumps the patch version in `pyproject.toml`
  and `packaging/npm/launcher/package.json` on every commit (unless the commit already edits the
  `version` line). This is expected on every commit, not concurrent-session noise. Only `--no-verify`
  skips it — ask the user first.
- `scripts/pre-push` rejects pushing a commit to `beta` or `main` whose own tip is not SSH-signed by
  a key in `src/omm/trust/allowed_signers`. **Never sync a channel with GitHub's web UI** ("Sync
  fork" / "Update branch" / merging a PR on the site) — it produces a web-flow-GPG-signed merge that
  strands every `omm update` client and fails the "Trusted PR head" check. Merge locally instead
  (`git merge origin/main`, auto-SSH-signed). If a web-flow merge already landed as the head, add an
  SSH-signed endorsement commit on top — never force-push a shared channel.

## Working style (explicit user preferences)

- Ask clarifying questions before coding anything ambiguous; propose the approach and get a go-ahead
  before any non-trivial UX or architecture rework (a one-line mechanical fix does not need this).
- Verify against the real running thing, not specs — external runner formats/dirs have repeatedly
  turned out different from docs. Read-only inspection of the user's real environment is encouraged.
- CLI surface stays minimal (brew/apt feel). Prefer nesting config-shaped commands under the existing
  `omm setting` group over new top-level verbs. When consolidating, fully delete the old command —
  no hidden back-compat aliases.
- Telemetry, benchmark upload, and error reports are all opt-in and must stay opt-in.

## Never touch the user's installed `omm`

The user's daily-driver `omm` is a separate pipx install (`~/.local/bin/omm`, its venv, and the
`~/.omm/src` editable clone). **Only edit files in this repo checkout.** To verify a change that
affects real pipx/git/subprocess behavior, use an isolated sandbox:
`pipx install --force --editable <clone> --suffix _verify` run with `HOME=<scratch>/fake-home`
prefixed, then `pipx uninstall omm_verify` and `rm -rf` the scratch dirs.

- Never run `install.sh` / `install.ps1` directly, even from a throwaway clone with a flag you expect
  to short-circuit — both hardcode `$HOME`/`$env:USERPROFILE` and `rm -rf ~/.omm/src`. Extract the
  one function under test into a standalone file instead.
- `cli.SRC_DIR` is bound once at import from the real `Path.home()`. A few tests in
  `tests/test_cli_update.py` exercise the real `shutil.rmtree(SRC_DIR)` / git paths and have wiped
  the real `~/.omm/src` during an ordinary `pytest` run. If that dir exists on the machine, treat
  full-suite runs touching that file with the same caution as manual verification.

## Commands

```sh
python -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]" -r requirements-train.txt   # dev + training-test deps CI uses

python -m pytest -q                                # full suite (run from repo root; pythonpath=".")
python -m pytest tests/test_foo.py::test_bar -q    # single test
omm --help                                         # smoke-check the entry point

# Cloudflare Worker (telemetry gateway)
cd cf-worker && npm ci && npm test && npx tsc -p tsconfig.json

# npm launcher + package contract
npm --prefix packaging/npm/launcher test
python scripts/npm_package.py validate

# Firebase RTDB rules (needs Java + node)
npx --yes firebase-tools emulators:exec --only database --project demo-localfit \
  "node scripts/test_firebase_rules.mjs"

gh workflow run train.yml --repo omm-hippo/omm     # trigger nightly retrain manually
```

Tests requiring `scikit-learn` skip cleanly without the training deps — not a real failure.
There is no separate lint step. Use a temporary `OMM_HOME=$(mktemp -d)` for manual CLI checks.

## Architecture (the parts that span files)

**Hub + link model.** `downloader.py` fetches a GGUF into the hub; `registry.py` tracks it in
`~/.omm/models.json`; `linker.py` exposes it to each runner, trying same-volume hardlink → symlink →
owned copy, and records ownership so uninstall never deletes a file another engine still needs.
`scan_import.py` adopts GGUFs an external engine already has (sha256 dedup against the hub).

**Runners vs. sources.** `src/omm/engines/` (`ollama.py`, `lmstudio.py` over `base.py`) talk to a
runner's local API to actually load a model and measure generation. `src/omm/providers/`
(`huggingface.py`, `modelscope.py` over `base.py`) are the search/download sources.

**Recommendation ML.** `omm recommend` ranks candidate GGUFs by predicted tokens/sec.
`mltree.py` holds a RandomForest serialized as **plain JSON, never pickle** (untrusted-download ACE
risk; also keeps sklearn out of the runtime). `predictor.py` loads `published/recommend-model.json`
+ `published/candidates.json` from `raw.githubusercontent.com/omm-hippo/omm/main/`. `.github/workflows/train.yml`
retrains nightly from telemetry (`scripts/train_model.py`, gated by `scripts/model_quality_gate.py`)
and commits the artifacts straight into the repo. `featurize.py` turns raw hardware into model
features; `rules.py` holds the old heuristic thresholds used as synthetic bootstrap rows.

**Arena leaderboard scoring.** `scripts/arena_score.py` turns uploaded votes into
`published/arena-leaderboard.json`: a weighted Bradley-Terry quality fit (with a
fixed-strength phantom opponent, so an undefeated model neither diverges nor outranks a
well-supported one) grouped into confidence-interval tiers, plus a separate efficiency
rating fitted from **within-battle** `tok/s ÷ GiB` log-ratios so a model is never
credited for running on a bigger GPU. Efficiency only breaks ties inside a quality tier,
and only among `efficiency_comparable` models (the scope's component 0 - ratings from
different components mean nothing side by side); the two numbers are never blended. One
`(client_id, model pair)` contributes at most `CLIENT_PAIR_VOTE_CAP` effective votes.
Artifact schema 2 (sub-project D): the same scoring runs on the whole corpus and once per
memory group (`memory_kind:memory_bucket`, from `memory_group.py`), each scope carrying
pairwise `pairs` counts. `scripts/arena_quality_gate.py:publish` gates every scope on its
own (groups have lower bars) and merges with the last published file: a ranked scope is
never blanked by a blocked run, and before any scope ever ranked the job publishes a signed
`status: "collecting"` file with real counts. Writes happen only when content other than
`generated_at` changed;
`scripts/aggregate_arena_votes.py` is the CLI and `.github/workflows/arena-aggregate.yml`
runs it nightly (04:00 UTC, an hour after `train.yml`), signing with the same Ed25519
catalog key as the recommendation model. The math lives in `scripts/`, never in
`src/omm/` — the runtime only reads the artifact. Note the deliberate asymmetry with
telemetry: the `votes` RTDB node is `.read: false`, so the aggregator sends an
`Authorization: Bearer` access token it mints each run from the read-only service-account
key in secret `LOCALFIT_VOTES_SERVICE_ACCOUNT_JSON` (a stored token would expire in an hour), while
`train_model.py:fetch_real_rows` refuses to send one to the world-readable `telemetry`
node. Artifact fields and thresholds:
`docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md` and
`2026-10-16-arena-public-leaderboard-design.md`.

**Signing.** The recommendation artifact is Ed25519-signed by `scripts/sign_catalog.py` in the
nightly job and verified by `catalog.py:verify_signed_artifact` (called from `predictor.py` and
`search.py`). Public key is baked into `config.py` `DEFAULT_CONFIG["catalog_public_key"]`; the
private half lives only as the GH secret `LOCALFIT_CATALOG_SIGNING_KEY` on `omm-hippo/omm`.

**Trust / self-update.** `omm update` git-pulls `~/.omm/src` and verifies the new HEAD's SSH
signature against `src/omm/trust/allowed_signers` (`trust/__init__.py:verify_commit`). Two-parent
merge commits are resolved to their second parent (the signed PR tip) — this only handles the exact
2-parent case, not octopus merges. `install.sh` / `install.ps1` carry a **duplicated copy** of this
verify logic (no Python available on a fresh machine) — keep them in sync. Changing the verify
algorithm strands every already-installed client; such users need a manual
`cd ~/.omm/src && git fetch origin && git reset --hard origin/main` bridge.

**Releases.** Pushing a signed `v<version>` tag into `main` history is the release trigger.
`auto-release.yml` does that nightly (21:00 UTC) with the retrain bot key whenever `main` moved
past the last tag with green checks and the version is unpublished; it can also be dispatched by
hand, and a maintainer can still tag manually (`docs/release-process.md`).
`release.yml` (PyPI + asynchronous Homebrew dispatch), `npm-release.yml`, and
`windows-portable.yml` fire on it. All release paths use `release_artifacts.py verify-release` to
check the allowed tag signature, exact project version and checkout, and `main` ancestry.
The Python and Windows workflows each call the reusable `github-release.yml` only after their own
validation gates pass. They add wheel, sdist, `SHA256SUMS`, Windows ZIP, and ZIP checksum to one
draft; it is published only after all five remote assets pass checksum verification. Existing
asset bytes are never replaced by a rerun. The reusable publisher verifies that checksums cover
every expected filename. Its Windows job tests installation and uninstallation using a local
WinGet manifest pointing to the public archive, regardless of which asset set arrives last.
This does not submit the manifest to the WinGet community repository.

**Telemetry.** `benchmark.py` measures real tokens/sec via Ollama's `/api/generate`;
`contribute.py` runs an unattended benchmark loop (auto start/stop of the Ollama daemon under
`--yes`) and uploads rows. Data goes to Firebase RTDB project `localfit-8ab57`, `telemetry` node,
through the `cf-worker/` Cloudflare Worker gateway. `cf-worker/src/validate.ts` is what enforces the
telemetry schema at runtime (the Worker writes with a rules-bypassing service token, tested by the
cf-worker vitest job in CI); `database.rules.json` keeps `.write` denied and is the documented
source of truth to diff against - its emulator test only proves the direct write path is closed.
`src/localfit_server/` (FastAPI) is an optional self-hostable collector — not the primary path.
Four separate opt-in outbound channels share the `cf-worker/` PoW gateway, each its own
RTDB node + `database.rules.json` block + `validate.ts` validator: `telemetry` (benchmark
rows, world-readable), `error_reports` (`error_report.py`, private), `usage`
(`usage.py` — anonymous daily batch: random `~/.omm/client-id`, `client_version`, OS/arch,
bucketed RAM/VRAM, GPU vendor, and a `<command> <outcome>` tally; **never** model names,
paths, args, or IP), and `votes` (`arena_upload.py`, private — one row per `omm arena`
vote: winner, both models' provider/repo/filename/sha256/quant, each side's elapsed time,
tokens, and measured speed/memory, a coarse marketed-size memory group
(`memory_kind`/`memory_bucket` from `memory_group.py`, both or neither), plus the same
`client-id`; **never** the prompt the
user typed or anything derived from it — `validateVoteEvent` rejects a `prompt` key by
name). All four are configured under `omm setting upload {benchmark,usage,crash,votes}`
(bare `omm setting upload`, or `omm setting` → "Upload", opens an interactive picker over the
four channels; `cli.py:_upload_channel_menu`). The `omm setup` data-sharing prompt covers
`usage` + `crash` (`onboarding.run_data_sharing_step`); `benchmark` and `votes` are not in it —
both default to "ask" and are prompted in context (`benchmark` per run, `votes` at the end of
an `omm arena` session). `PRIVACY.md` is the user-facing spec; keep it,
the `omm setup` consent text, `validate.ts`, and `database.rules.json` in sync when fields change.

**Arena.** `omm arena` (`arena.py` + `cli.py:_arena_session`) compares two installed models
blind on one prompt and records the vote to `~/.omm/arena/votes.jsonl`, local-only. Blindness
is the feature: slot order is redrawn every round — including a `--keep` session and a pair
given positionally — because the post-vote reveal otherwise tells the user which side is which
for every later round. Generation reuses `quality.py`'s `_generate`/`_generate_lmstudio` with
`generation=None` (the model's own sampling defaults) and the same load → generate → unload
lifecycle `benchmark.py` uses. `arena_upload.py` is the separate opt-in upload channel (see
Telemetry above); it reads `votes.jsonl` rows only after the user consents and never writes
that file. `omm arena --leaderboard` (`arena_board.py`) reads the published artifact, verifies
its signature before showing anything (live, else a cache that still verifies; a 404 means
"not published yet"), shows this machine's memory group or falls back to overall, and
records numbered refs in `session_cache` (an `arena-unsourced:` ref turns into a plain error
in `_resolve_ref`). The web board is a separate page, `omm.run/leaderboard` (omm-site
`src/lib/leaderboard/`); the older schema-1 `/arena` page on the site is a teammate's and is
left alone. Specs: `docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md` (local CLI),
`2026-09-26-arena-vote-upload-channel-design.md` (upload) and
`2026-10-16-arena-public-leaderboard-design.md` (leaderboard); roadmap issues #407-#411.

**Run log.** `runlog.py` attaches a JSON-lines handler to the `omm` logger for one process;
`cli.main()` brackets `app()` with `runlog.start()`/`finish()`. Every invocation writes
`~/.omm/logs/<ts>_<pid>_<cmd>.jsonl` plus a `history.log` block (`omm log` reads it). **Local
only** — never uploaded, and the outbound channels above do not read it. `OMM_DEBUG=1` adds
subprocess/HTTP detail. Domain modules emit events via `logging.getLogger("omm.<module>")`.
`runlog.py`/`usage.py` swallow their own errors and stay import-side-effect-free (read
`config.OMM_HOME` at call time). `linker.link_file` / `downloader.download_file` are thin
logging wrappers over `_link_file_impl` / `_download_file_impl`.

**Auto-import.** `omm setting auto-import enable` (off by default) registers a
per-user background service (`watch_service.py` - launchd/systemd --user/Task
Scheduler) that runs `omm _auto-import-run`, a hidden subcommand blocking in
`watch.run_watch_loop()`. It watches every supported local AI app's model directory
(`watch.watch_target_dirs()`) with `watchdog`, debounces bursts of filesystem events,
waits for each candidate file's size to stop changing, then runs the same
`scan_import.find_external_models` -> `group_by_hash` -> `adopt_group` pipeline
`omm import` already uses (see Hub + link model above) and desktop-notifies via
`notify.py`. `watchdog`/`plyer` are the `watch` optional extra - never a runtime
dependency of a plain `omm` install.

**Cluster.** `omm cluster` (`src/omm/cluster/`, #503) turns Thunderbolt-5-linked Macs into
one exo cluster. omm never manages MLX files; exo owns its models and omm only drives its
loopback API (`exo_client.py`, field mapping verified against exo 1.0.71 fixtures in
`tests/fixtures/exo/`). The bundled backend stalls `launched-suspended` when exec'd from
inside `EXO.app`, so `preflight.stage_runtime` copies `Contents/Resources/exo` to
`OMM_HOME/cluster/exo-runtime`, and `exo_service.py` runs that copy as LaunchAgent
`com.omm.cluster-exo`. The agent sets both `EXO_LIBP2P_NAMESPACE` and
`EXO_ZENOH_NAMESPACE`, passes `--libp2p-port 52416`, and puts `/usr/sbin` in PATH.
`firewall.py` loads pf sub-anchor `com.apple/omm-cluster` through root LaunchDaemon
`com.omm.cluster-pf`. The anchor trusts lo0 and Thunderbolt and blocks exo's API (52415) plus
every TCP port in 49153-65535 from elsewhere, because the MLX ring binds a random high port
on 0.0.0.0. exo's libp2p port 52416 is deliberately open to the office network: exo dials a
peer's addresses in sorted order and skips the rest while one is pending, so the office-network
address always goes first, and with it blocked Macs met only by luck. Only a node with the pnet
key can finish that handshake, and exo derives the key from the cluster name, so
`settings.new_namespace()` makes it 128 random bits (`omm-` + 32 hex); 32-bit names from older
omm still load but `join` refuses them and `init` replaces them. The blocks are `block return` (RST), never `block drop`: exo's libp2p dials
every address a peer advertises, and a silently dropped SYN on a blocked address stalled
Thunderbolt peer discovery for 40 s or forever. TCP blocks match only a SYN (`flags S/SA`) so
connections the Mac opened before the rules loaded keep working, and the loader saves its
`pfctl -E` token so a re-run and `leave` give the pf enable reference back (`pfctl -X`);
otherwise pf stayed enabled after leave. `netconfig.py` replaces macOS's Thunderbolt
Bridge (no spanning tree: a 3-Mac triangle floods it with 38k+ packets per 5 s) with one DHCP
service per Thunderbolt port (`omm Thunderbolt N`, link-local 169.254.x.x addresses), and
init/join run it before exo starts and before the pf rules are written; `leave` restores the
bridge by editing preferences.plist (order matters, see its docstring). `power.py` sets `pmset -a sleep 0` in init/join (original value saved for
`leave`): an idle-slept Mac only DarkWakes ~45 s per packet and keeps dropping out. `appfw.py`
adds exo (and the gateway's real Python binary on the head) to the macOS Application Firewall:
with ALF on, an unknown binary's inbound connections wait forever for an on-screen Allow click. The
head Mac's `gateway.py` (LaunchAgent `com.omm.cluster-gateway`, hidden
`_cluster-gateway-run`, port 52440) is the only LAN-reachable entry. It serves HTTPS only (#536,
`cluster/tls.py`): the head makes its own CA once (`OMM_HOME/cluster/tls/`, kept by re-init and
`leave`) and the gateway re-issues the server certificate from it when the Mac's LAN IPv4/`.local`
names change; a plaintext request gets a 400 pointing to https before any byte of it is parsed.
`GET /omm/ca.pem` is the one route without a token: `omm rag remote set` fetches it, proves the
leaf chains to it, shows its SHA-256 fingerprint (TOFU, `--yes` when stdin is not a tty) and pins
it as `OMM_HOME/rag-remote-ca.pem`; every employee-side caller (`rag_remote.Client`, `chat._open`,
`agent_cluster`, web `shared_ai`) trusts only that file. Harnesses: Claude Code gets
`env.NODE_EXTRA_CA_CERTS` in settings.json (applied but not in the journal's field list, so
`agent_cluster.restore` drops it after a merged restore); Codex (`CODEX_CA_CERTIFICATE`) and pi
(`NODE_EXTRA_CA_CERTS`) only read it from their launch environment, so omm prints the launch line. It verifies hashed
tokens from `tokens.py` (fail closed) and forwards four inference routes; it answers
`GET /omm/loaded-models` itself (exo's `/v1/models` lists every known model, not the loaded
ones), which `omm cluster run` (`chat.py`) uses on an employee PC. It also folds
Claude Code's `role: "system"` messages into the top-level `system` field, because exo
answers those with 422. exo keeps instances only in memory, so the gateway process also runs
`autoload.py`: every 30 s it reloads every model `omm cluster load`/`run` loaded on the head
(`cluster.json` `models`, each with the admin's `--sharding`/`--macs` choice or automatic; an old
single `model` string still loads; `unload`/`uninstall` and Memory Guard victims drop their entry), one at
a time and never unloading anything, after waiting up to 10 min for every known Mac.
`load --sharding tensor|pipeline --macs N` filters exo's `/instance/previews` (RDMA `MlxJaccl` first, then
least memory per Mac); exo offers exactly one node set per sharding/backend/count, so Macs cannot be
named. Without flags `recommend.choose_placement` picks (Tensor over RDMA when it fits).
`info` shows every preview as a table (`recommend.placement_rows`; Mac names only on the head).
Model verbs mirror top-level omm (`cluster/models.py`): `install` downloads to every Mac with exo's
`/download/start` (each Mac's Pending entry carries the whole-model shard) without loading, `uninstall`
uses `DELETE /download/{node}/{model}`, `load`/`run` apply `memory_guard_policy` (exo marks a
non-fitting preview with `No cycles found with sufficient memory`). Mutating ones are head-only
(`commands.require_head_for`). `omm list`/`omm run` also reach cluster models (`chat.cluster_configured`;
`omm list --json` stays local-only). On an employee PC (no `cluster.json`, `omm rag remote set`),
`status`/`recommend`/`search`/`info` come from the gateway's read-only `GET /omm/{status,recommend,search,info}`
(`gateway.VIEW_ROUTES`; counts only, never Mac names or firewall details), `benchmark` measures a
loaded model through `/v1/chat/completions`, and admin verbs say to ask the head's admin.
On the head, `status` also reports document search (RAG) readiness (`cluster/document_search.py`, #534):
Ollama installed/running and the `omm rag embedding-model` pulled, with the fix command as a problem only
once the head has documents or its own collections. It never installs, starts or pulls anything.
Spec: `docs/superpowers/specs/2026-10-26-cluster-cli-redesign-design.md`.
Bare `omm cluster` (`cluster/guide.py`, the `cluster_app` callback) is the guided setup: a Mac in no
cluster checks macOS/RDMA, then asks join (pasted name) or init and walks cables -> restart; head and
member turn `status()`'s problems into steps and run a narrow fix after a yes (never a whole init/join,
which restarts exo). Problems are `problems.Problem` (a `str` carrying kind run/manual/wait/tell, `where`,
`fix`), so `status` and the guide never disagree; non-tty and `--json` only print. A new Mac on a
long-lived cluster replays exo's whole event log first (hours for 1.67M events) and its `/state` lacks
itself meanwhile, so the member link gets its id from exo `GET /node_id`, tries `cluster/last-head.txt`,
and reports `catching_up` + `event_index`; the head shows progress/ETA (`members.json` `event_rate`)
instead of advising a re-join. The head warns when its LAN address left `cluster.json`
`employee_addresses` (DHCP). Spec: `docs/superpowers/specs/2026-10-29-cluster-guided-next-step-design.md`.
Employee PCs route a harness to the gateway with `omm setting agent cluster` (`agent_cluster.py`,
head URL + token from `omm rag remote set`); it reuses `agent_connection.py`'s journal and field
list unchanged, so `agent cloud` restores it. Specs: `docs/superpowers/specs/2026-10-22-exo-cluster-design.md`
and `2026-10-22-exo-cluster-spike-findings.md`.
Member link (`member_link.py`, LaunchAgent `com.omm.cluster-member`, hidden `_cluster-member-run`,
installed by `join`): every 60 s a member polls the head's `POST /omm/member/poll` (and
`GET /omm/member/rag/snapshot`) on the gateway port, Thunderbolt addresses first. These routes take no
employee token: with a key derived from the namespace (never the namespace itself), `member_auth.py`
signs each request and each reply over the nonce, the TLS leaf the member saw (the first block of
`tls/server.pem`) and the body, because members do not pin the head's CA and a relay must not replay a
request to the head. Every Mac with the
namespace is trusted equally (spec decision). The member follows the head's exact commit through
`cli._perform_update(..., target_commit=)` (signed, on the channel branch, never backward: a member ahead
waits for `omm update` on the head; it switches to the head's channel), then exits for launchd to restart
it; the head's `omm update` restarts its gateway and warns that members follow. It also keeps the head's
document store as `OMM_HOME/cluster/rag-backup/library.sqlite3` (`rag_backup.py`, never served). The head
records reports in `cluster/members.json` and `status` lists each Mac's version and backup. `join` now
opens the gateway port in pf and ALF on every member, so `omm cluster handover MAC` (`handover.py`, run on
the head) needs no sudo: it freezes `/rag/*` and `/mcp` (503), writes `cluster/handover.json` with
`head_epoch + 1` and the kept models; the target's link pulls a final snapshot, swaps it in as its live
store (its own store moved aside to `library.sqlite3.before-handover-<ts>`) and starts its gateway; the old
head then steps down (tokens deleted, TLS CA kept, its store becomes its backup via `stepped-down.json`).
Tokens and the CA are never moved; employees re-run `omm rag remote set`. A gateway that finds a peer head
with a higher (epoch, node id) steps down (`handover.stale_head_loop`), and a gateway started on a
member removes itself. The target re-checks the instruction just before taking over, and a failed
promote rolls back to a working member. Spec:
`docs/superpowers/specs/2026-10-27-cluster-handover-version-sync-design.md`.

**CLI shape.** `cli.py` is a ~9,300-line Typer monolith (entry `omm.cli:main`). Startup speed
matters: `questionary`, `requests`, `prompt_toolkit`, and `importlib.metadata` are lazy-imported
inside functions to keep `omm help` near ~140ms — do not hoist them back to module scope. Tests
monkeypatch these via the `sys.modules` singleton (`import requests` in the test, patch that object).

**Design docs.** Non-trivial feature batches follow brainstorm → spec → plan → implement, with
docs in `docs/superpowers/specs/YYYY-MM-DD-*.md` and `docs/superpowers/plans/YYYY-MM-DD-*.md`.
Check these before assuming undocumented intent behind a feature's shape.

## Other directories

- `scripts/` — release (`pypi_release.py`, `npm_release.py`), training, `sign_catalog.py`,
  `verify_trusted_head.py`, winget/portable build tooling.
- `packaging/npm/` — the npm-distributed native launcher (`launcher/lib/launcher.js`).
- `published/` — generated: recommend model, candidates, signed manifest. Never hand-edit; use the
  owning script.
- `.github/workflows/` — `ci.yml` (6 required checks), `train.yml`, per-runner `ci-engine-*.yml`,
  `trusted-head.yml` (branch protection), `github-release.yml`
  (asset-backed reusable Release publisher), release/npm/portable.
- `demo/model-visualizer/` — standalone React demo of the RandomForest walk; not shipped.
