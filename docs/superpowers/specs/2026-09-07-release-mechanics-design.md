# Release mechanics: cut_release owns versioning, 2-review trunk

**Date:** 2026-09-07
**Status:** approved (design), implementation in progress
**Branch:** `redesign/release-mechanics` (stacked on `redesign/beta-trunk-main-release`, PR #286)
**Follows:** `2026-09-06-beta-trunk-main-release-design.md`

## Problem

The branch redesign (PR #286) made `beta` the trunk and `main` a fast-forward-only
release pointer moved by `scripts/cut_release.py`. Three gaps remain:

1. `scripts/pre-commit` bumps the patch version on every commit. On the fast-moving
   `beta` trunk that inflates the number meaninglessly (0.3.41 → 0.3.61 in one
   working session) and it was the source of the merge-commit
   `optionalDependencies` drift bug.
2. `cut_release.py` only fast-forwards + tags; it does not pick or set the release
   version, verify the target's CI is green, or show what is being promoted.
3. `beta` protection requires 1 review; the team is 3 people and wants every
   release-bound change to have broad sign-off.

## Design

### Versioning: `cut_release.py` owns the number

- **`scripts/pre-commit` is deleted.** `core.hooksPath = scripts` still serves
  `pre-push` (the SSH-signature gate). Nothing auto-bumps the version any more.
- Between releases every `beta` commit carries the **same** `X.Y.Z` — the last
  released version. `omm --version` already appends the commit hash and channel
  (`0.3.52 (a1b2c3d, beta)`, `cli.py:_version_summary`), so beta builds stay
  distinguishable. Beta is never published to PyPI/npm, so a repeated version
  string has no external consumer.
- `cut_release.py` computes the next release version from the **current
  `pyproject.toml` version**: `X.Y.Z` → `X.Y.(Z+1)`. Always +0.0.1, no flags, no
  prompt. It writes the bumped number to `pyproject.toml` and
  `packaging/npm/launcher/package.json` (top-level `version` **and** the five
  `@omm-hippo/omm-<platform>` `optionalDependencies`), in one signed commit
  titled `release: v<next>`.

### `cut_release.py` flow (rewritten)

1. Refuse a dirty work tree. `git fetch --no-tags origin beta main`.
2. Target = `origin/beta` tip. (The `--sha` option is dropped — you release the
   trunk as it stands.)
3. Verify the target is a fast-forward over `origin/main`; refuse if `origin/main`
   is already there.
4. **Verify the target's required checks are all green.** Read the beta branch's
   `required_status_checks.contexts` (`gh api repos/<slug>/branches/beta/protection`)
   and the target commit's check runs (`gh api repos/<slug>/commits/<sha>/check-runs`);
   every required context must have `conclusion == "success"`. Refuse otherwise.
   `--skip-ci-check` overrides (for a first cut before beta protection exists).
5. Compute `next` = pyproject `X.Y.(Z+1)`. Refuse if tag `v<next>` exists locally
   or on the remote.
6. **Print the promotion for review:** `git log --oneline --no-merges
   origin/main..<target>` and `git diff --stat origin/main..<target>`.
7. `--dry-run` stops here. Otherwise, unless `--yes`, prompt
   `Promote <N> commits and release v<next>? [y/N]`.
8. Create a signed commit on `<target>` bumping the two version files →
   `release: v<next>`.
9. `git push origin <newsha>:beta` (fast-forward; needs the bypass allowance —
   see runbook). The `pre-push` hook signs-checks it like any channel push.
10. `git push origin <newsha>:main` (fast-forward).
11. `git tag -s v<next> <newsha> -m "omm v<next>"`; `git push origin v<next>`.
12. The `v*` tag fires `release.yml` (PyPI + Homebrew dispatch), `npm-release.yml`,
    `windows-portable.yml`. Each PyPI/npm publish still pauses on its
    deploy-environment `required_reviewers` — **one approval click per release**,
    kept deliberately (supply-chain gate).

If `origin/beta` moves between step 2 and step 9 the push is rejected as non-FF;
`cut_release.py` aborts with "beta moved, re-run".

### `beta` branch protection (runbook)

- `required_approving_review_count`: 1 → **2**.
- `bypass_pull_request_allowances.users`: the release maintainers, so
  `cut_release.py`'s `release:` commit can be pushed straight to `beta` without a
  PR. (Verify this coexists with `enforce_admins: true`; GitHub treats the bypass
  list as an explicit grant independent of `enforce_admins`. If it does not, fall
  back to `enforce_admins: false` on `beta` — `required_status_checks` still
  apply to PR merges regardless — and document it.)

### Automation approvals (`train.yml`, `emergency-signal.yml`)

Both auto-merge a bot PR into `beta` after one bot approval. With 2 required
reviews they need a second approver:

- Approve with **two** PATs: the existing `LOCALFIT_APPROVAL_PAT` and a new
  `LOCALFIT_APPROVAL_PAT_2`.
- Runbook: create a third collaborator account, add `LOCALFIT_APPROVAL_PAT_2` as
  a repo secret. Until then these workflows' auto-merge step will block on the
  review count (loud, not silent).

### Docs

`CLAUDE.md`:
- version section: `cut_release.py` owns the number; `pre-commit` is gone; beta
  commits share the last release's version.
- branches section: beta needs 2 reviews; the `release:` commit bypasses the PR
  requirement.

## Testing

- `test_cut_release.py`: next-version computation from pyproject; the CI-green
  gate (stub `gh` via a fake `_gh` seam) passing and failing; promotion-diff
  output present; `--dry-run` writes nothing and pushes nothing; the
  commit→FF-beta→FF-main→tag path against the bare-repo fixture with `gh` stubbed.
- Delete any coverage tied to `scripts/pre-commit` (there is none today).
- `test_npm_package.py` / `test_package_metadata.py` / `distribution_versions`:
  unaffected — the version stays `X.Y.Z`.
- Full `pytest -q` green; `python scripts/npm_package.py validate` green.
