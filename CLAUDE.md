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

## What this is

`omm` (Open source Model Manager) — an apt/brew-style CLI package manager for local GGUF LLMs.
Downloads a GGUF once into a central hub (`~/.omm/`, override `OMM_HOME`) and links it into every
installed local runner (Ollama, LM Studio, Jan, AnythingLLM, Msty, text-generation-webui, KoboldCpp)
without duplicating the file. Also ranks models against live hardware and verifies real local
generation.

- PyPI package `omm-model`; import package `omm`; entry points `omm` and `localfit-server`.
- Working dir is `/Users/shinmingyu/Project/Localfit`; the GitHub repo name is `omm` (do not confuse).
- Python 3.10+; CI test job pins 3.12, bare-runtime-install job pins 3.11 (was all 3.11 —
  moved while deps are frozen to the contest report through 2026-09-06; see the freeze note in
  `pyproject.toml`). Stack: Typer CLI, Hatch build, `questionary` TUI, `cryptography`,
  `filelock`. `scikit-learn` is CI/training-only, never a runtime dependency.

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
- `beta` is unprotected and **must always be a superset of `main`**. `.github/workflows/sync-beta.yml`
  auto-merges `origin/main` into `beta` on every push to `main`, SSH-signed by the retrain bot key
  (in `allowed_signers`) so beta `omm update` clients verify it. It only needs a human when that
  merge hits a conflict — the job fails loudly and you resolve it with a local `git merge origin/main`
  → push. `branch-ancestry-check.yml` stays as the post-hoc safety net; it polls through a ~3-minute
  grace window on a `main` push so it only goes red when `sync-beta.yml` genuinely couldn't catch up.
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
through the `cf-worker/` Cloudflare Worker gateway. `database.rules.json` enforces the schema per
`benchmark_version` (currently v8/v9; older versions grandfathered) and is emulator-tested in CI.
`src/localfit_server/` (FastAPI) is an optional self-hostable collector — not the primary path.
Three separate opt-in outbound channels share the `cf-worker/` PoW gateway, each its own
RTDB node + `database.rules.json` block + `validate.ts` validator: `telemetry` (benchmark
rows, world-readable), `error_reports` (`error_report.py`, private), and `usage`
(`usage.py` — anonymous daily batch: random `~/.omm/client-id`, `client_version`, OS/arch,
bucketed RAM/VRAM, GPU vendor, and a `<command> <outcome>` tally; **never** model names,
paths, args, or IP). All three are configured under `omm setting upload {benchmark,usage,crash}`
(bare `omm setting upload`, or `omm setting` → "Upload", opens an interactive picker over the
three channels; `cli.py:_upload_channel_menu`). The `omm setup` data-sharing prompt covers
`usage` + `crash` (`onboarding.run_data_sharing_step`); `benchmark` is not in it — its policy
defaults to "ask" and is prompted per run. `PRIVACY.md` is the user-facing spec; keep it,
the `omm setup` consent text, `validate.ts`, and `database.rules.json` in sync when fields change.

**Run log.** `runlog.py` attaches a JSON-lines handler to the `omm` logger for one process;
`cli.main()` brackets `app()` with `runlog.start()`/`finish()`. Every invocation writes
`~/.omm/logs/<ts>_<pid>_<cmd>.jsonl` plus a `history.log` block (`omm log` reads it). **Local
only** — never uploaded, and the outbound channels above do not read it. `OMM_DEBUG=1` adds
subprocess/HTTP detail. Domain modules emit events via `logging.getLogger("omm.<module>")`.
`runlog.py`/`usage.py` swallow their own errors and stay import-side-effect-free (read
`config.OMM_HOME` at call time). `linker.link_file` / `downloader.download_file` are thin
logging wrappers over `_link_file_impl` / `_download_file_impl`.

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
  `trusted-head.yml` / `branch-ancestry-check.yml` (branch protection), `github-release.yml`
  (asset-backed reusable Release publisher), release/npm/portable.
- `demo/model-visualizer/` — standalone React demo of the RandomForest walk; not shipped.
