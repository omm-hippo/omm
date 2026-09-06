# beta = trunk, main = stable release pointer

**Date:** 2026-09-06
**Status:** approved (design), implementation in progress
**Branch:** `redesign/beta-trunk-main-release`

## Problem

`main` and `beta` are both long-lived branches that each take commits from
independent inflows:

- Every PR merges into `main` (the branch-protected trunk).
- CLAUDE.md tells contributors "most feature work targets `beta`", so `beta`
  also receives commits that never went through `main`.
- `sync-beta.yml` force-ports `main` into `beta` on every push to `main`, one
  direction only.

The invariant `beta ⊇ main` is required for `omm update` signature verification
(`verify_update` in `src/omm/trust/__init__.py` walks first-parent forward from
a shared ancestor) and is enforced *after the fact* by
`branch-ancestry-check.yml`.

When a `beta`-only commit touches code that a later `main` commit also changes,
the one-directional auto-merge conflicts and a human must resolve it by hand.
On 2026-09-03 this started happening and went unresolved for ~10 consecutive
`main` pushes; divergence reached 30 commits. `git rerere`, failure
notifications, and the ancestry-check grace window are all scaffolding around
the same root problem: **two near-identical protected branches with separate
inflows, expressing a single bit of information (release cadence).**

## Design

### Branch roles swap

| | before | after |
|---|---|---|
| `beta` | superset mirror, unprotected | **protected trunk.** All PRs target it. Carries the 6 required CI checks, 1 review, required signatures, `enforce_admins`, `strict` — the protection currently on `main`. |
| `main` | protected trunk, all PRs | **stable release pointer.** Advances only by fast-forward to a commit that already exists on `beta`. Protected against force-push and deletion; the "require a PR" rule is removed because `main` now advances by a direct fast-forward push, not a merge. |

Because `main` only ever moves to a commit that is already on `beta`, `main` is
an ancestor of `beta` **by construction**. Divergence is structurally
impossible; nothing needs to detect or repair it.

`omm update` is unchanged. `_channel_branch()` (`src/omm/cli.py:1226`) still maps
`stable → main`, `beta → beta`. The `same_branch=True` monotonic check in
`verify_update` still holds for both: `main` only fast-forwards, `beta` only
advances by PR merge. A `stable ↔ beta` channel switch is still
`same_branch=False` and works as today.

### Release-cut mechanism

New `scripts/cut_release.py`:

1. Refuse a dirty work tree.
2. Take a target SHA (default: `origin/beta` tip). Verify it is contained in
   `origin/beta` (`git merge-base --is-ancestor <sha> origin/beta`).
3. Verify `<sha>` is a fast-forward over `origin/main`
   (`git merge-base --is-ancestor origin/main <sha>`); refuse otherwise.
4. `git push origin <sha>:main`.
5. Create and push a signed `v<version>` tag on `<sha>`, where `<version>` is
   read from `pyproject.toml`.

`release.yml` already fires on `v*` tag push and stays as-is.
`release_artifacts.py verify-release` already checks that the tag commit is an
ancestor of `origin/main` (`--main-branch` default `main`) — still true, the tag
commit *is* the new `main` tip.

### Published data artifacts decoupled from the code channel

`config.py` currently reads the recommendation model, its signed manifest, and
(via `predictor.extract_emergency_signal`) the emergency-update signal from
`raw.githubusercontent.com/omm-hippo/omm/main/published/*.json`. If `main` only
moves on a release cut, every user's recommendation model and — critically —
the emergency signal would go stale between releases.

These artifacts are **not** consumed through `omm update`; they are fetched over
plain HTTPS and independently verified by Ed25519 signature
(`catalog.py:verify_signed_artifact`). Their trust does not depend on which
branch serves them.

Change:

- `src/omm/config.py`: the four `raw.githubusercontent.com/omm-hippo/omm/main/`
  URLs → `.../omm/beta/`. (The two legacy mirror URLs at
  `minigu5/Localfit` and `minigu5/Omm` are left untouched — frozen dead
  mirrors.)
- `train.yml`: PR `--base main` → `--base beta`.
- `emergency-signal.yml`: PR `--base main` → `--base beta`.

`beta` is the always-current trunk, so the artifacts are as fresh as today. A
side effect is that the daily retrain PR stops churning `main`.

A dedicated `published` branch fully isolated from code was considered and
rejected as larger scope for no extra safety here; it stays a possible future
refinement.

### Workflow changes

| file | change |
|---|---|
| `sync-beta.yml` | **delete.** No more one-directional port. |
| `branch-ancestry-check.yml` | **keep, slimmed.** Still asserts `main` is an ancestor of `beta` on pushes to either branch, as a cheap catch for operator error during a botched release cut. The 3-minute grace-window poll can drop to a single check since there is no longer a sync race. |
| `pr-description-check.py` | remove `EXEMPT_HEAD_BRANCHES = {"beta"}` — there is no `beta → main` sync PR any more. Keep the `retrain/` head-prefix exemption (bot PRs now target `beta`). |
| `train.yml` | `--base beta`; the "branch protection blocks direct pushes to main" comment updates to `beta`. |
| `emergency-signal.yml` | `--base beta`; same comment update. |
| `ci.yml`, `ci-engine-*.yml`, `npm-package.yml` | `push:` triggers already list both `main` and `beta`; keep both. `pull_request:` has no branch filter, so PRs to `beta` are already covered. No change needed, but audited. |
| `trusted-head.yml` | uses `pull_request_target` with `github.event.pull_request.base.sha` — base-branch-agnostic. No change. |

### Installer changes

`install.sh:611` clones without `--branch`, taking the repo default branch.
`install.ps1` already honours `$env:OMM_INSTALL_BRANCH`. With the repo default
branch changed to `beta` (see runbook), both installers must pin stable:

- `install.sh`: clone with `--branch "${OMM_INSTALL_BRANCH:-main}"` (add the env
  hook for parity with `install.ps1`).
- `install.ps1`: default `$Branch` to `main` when `$env:OMM_INSTALL_BRANCH` is
  unset, and pass it to the clone.

The duplicated **signature-verification** logic in both scripts is untouched —
only branch selection changes.

### GitHub admin cutover runbook (irreversible, executed with per-step approval)

Ordered. In-repo PR (everything above) merges first, then:

1. **Merge PR #285** (`beta → main`) so `main` content == `beta` content:
   a clean fast-forwardable starting point.
2. Merge the `redesign/beta-trunk-main-release` PR into `beta`.
3. Fast-forward `main` to that `beta` tip once by hand
   (`git push origin <beta-sha>:main`) so `main` carries the new workflow set
   too.
4. Add branch protection to `beta` mirroring `main`'s current protection.
   Read the live config first
   (`gh api repos/omm-hippo/omm/branches/main/protection`) and copy it verbatim:
   `required_status_checks` (strict, the exact context list it currently has),
   `required_pull_request_reviews` (1, `require_last_push_approval: false`),
   `enforce_admins: true`, `required_signatures: true`,
   `allow_force_pushes: false`, `allow_deletions: false`.
5. Change the repo **default branch** to `beta`.
6. Relax `main` protection: keep `allow_force_pushes: false`,
   `allow_deletions: false`, and a push restriction to the release role; remove
   the required-PR / required-review rule so `cut_release.py` can fast-forward.
   Keep `required_signatures: true`.
7. Delete `sync-beta.yml` is already done in the PR; confirm no scheduled run is
   mid-flight.
8. Update the branch-protection `required_status_checks` contexts on `beta` if
   any job name differs from what `main` had.

### Rollback

Before step 4, everything is a normal revert. After step 4–6, rollback is:
restore `main` protection, set default branch back to `main`, re-add
`sync-beta.yml` from git history, revert the `config.py` URL commit. No client
is stranded at any point because `main` never rewrites history and never stops
being a valid signed channel — it only moves less often.

## Testing

- `scripts/cut_release.py`: unit tests for the FF check, the "not on beta"
  refusal, the dirty-tree refusal, and the non-fast-forward refusal, using a
  temp git repo fixture (same style as `tests/test_release_artifacts.py`).
- `pr-description-check.py`: update `tests/` for the dropped `beta` exemption;
  add a case that a `beta`-head PR is now checked.
- `config.py`: existing tests that assert the catalog URLs — update expected
  host path to `/beta/`.
- Full `pytest -q` green.
- `python scripts/npm_package.py validate` green.
- Manual: dry-run `cut_release.py` against a scratch clone (no push).

## Docs

Rewrite the "Repo, branches, workflow" section of `CLAUDE.md`:

- `beta` is the trunk; all PRs target it; it is branch-protected.
- `main` is the stable release pointer; it only fast-forwards via
  `scripts/cut_release.py`; never open a PR against `main`.
- Delete the `sync-beta.yml` / ancestry-race paragraphs; replace with the
  one-line structural invariant.
- Update `project_omm_sync_beta_workflow` memory and the release-process memory.
