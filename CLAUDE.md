# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`omm` (Open source Model Manager) — an apt/brew-style CLI package manager for local GGUF LLMs.
Downloads a GGUF once into a central hub (`~/.omm/`, override `OMM_HOME`) and links it into every
installed local runner (Ollama, LM Studio, Jan, AnythingLLM, Msty, text-generation-webui, KoboldCpp)
without duplicating the file. Also ranks models against live hardware and verifies real local
generation.

- PyPI package `omm-model`; import package `omm`; entry points `omm` and `localfit-server`.
- Working dir is `/Users/shinmingyu/Project/Localfit`; the GitHub repo name is `omm` (do not confuse).
- Python 3.10+, CI pins 3.11. Stack: Typer CLI, Hatch build, `questionary` TUI, `cryptography`,
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
- `beta` is unprotected and **must always be a superset of `main`**. After anything lands on `main`,
  check `git log origin/beta..origin/main --oneline --no-merges` and port every result to `beta`.
  Most feature work targets `beta`.
- **Committing freely is fine; pushing is always a separate explicit ask.** Wait for it every time.
- The user runs multiple Claude sessions against this checkout at once. Re-check `git log -5` /
  `git status` right before committing. Only ever `git add <your own filenames>` — never `-A` / `.`.
  Never blind `git stash pop`/`drop` — `git stash list` and diff first; the stash stack is shared.
- `core.hooksPath = scripts`. `scripts/pre-commit` auto-bumps the patch version in `pyproject.toml`
  and `packaging/npm/launcher/package.json` on every commit (unless the commit already edits the
  `version` line). This is expected on every commit, not concurrent-session noise. Only `--no-verify`
  skips it — ask the user first.

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
`release.yml` (PyPI + Homebrew dispatch) and `npm-release.yml` fire on it, and `github-release.yml`
creates the GitHub Release itself: it re-verifies the tag signature against `trust/allowed_signers`,
matches the tag to `pyproject` version, requires the tag to point into `main` history, then runs
`gh release create --generate-notes`. No reviewer-approval gate — pushing a valid signed tag is
enough. Windows portable ZIP and the
`whl`/`tar.gz`/`SHA256SUMS` assets are still attached manually (`windows-portable.yml` dispatch).

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
and consented to by one prompt in `omm setup`. `PRIVACY.md` is the user-facing spec; keep it,
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
  (auto GitHub Release on a signed tag), release/npm/portable.
- `demo/model-visualizer/` — standalone React demo of the RandomForest walk; not shipped.
