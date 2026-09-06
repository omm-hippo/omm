# Homebrew release synchronization

OMM's Python release workflow requests a Homebrew Formula update only after all
of the following have succeeded for a signed `v*` release:

1. TestPyPI publishing and destination-side verification.
2. PyPI publishing, file digest checks, and provenance verification.
3. Public `pipx` install, command execution, and uninstall on Ubuntu, macOS,
   and Windows.

The final asynchronous `sync-homebrew` job sends a `pypi_release_verified` repository
dispatch to `omm-hippo/homebrew-omm`. The Tap validates the source repository,
tag, commit, and public PyPI release before running `brew bump`. Homebrew's
upstream release cooldown remains authoritative; scheduled Tap runs retry after
the cooldown and open a Formula PR rather than pushing directly to `main`.
Because repository dispatch is asynchronous and the cooldown may still apply,
the source release workflow does not require immediate PyPI/Homebrew version
parity. The Tap workflow and its eventual Formula PR own that destination-side
verification.

## Required repository secret

Configure `HOMEBREW_TAP_DISPATCH_TOKEN` in the `omm-hippo/omm` repository.
Use either a GitHub App installation token or a fine-grained personal access
token restricted to the `omm-hippo/homebrew-omm` repository with only the
`Contents: Read and write` repository permission. The token is used only to
create the repository-dispatch event.

The release job fails explicitly if this secret is absent. That failure occurs
after PyPI's public installation checks and means the package is published but
the Tap was not notified. After restoring the secret, rerun only the failed
`Request asynchronous Homebrew Formula synchronization` job.

## The Formula is a generated artifact of `pyproject.toml`

Issue [#238](https://github.com/omm-hippo/omm/issues/238): `brew install
omm-hippo/omm/omm` repeatedly drifted from the dependency table
`pyproject.toml` actually pins — the Tap's `resource` stanzas were a second,
hand-maintained copy of the closure that a plain `brew bump` never touched.
`scripts/homebrew_formula.py` removes that second copy. It reads the frozen
runtime dependency closure straight out of `[project].dependencies`, resolves
each pin's sdist URL and SHA-256 from PyPI, and can render or verify
`omm.rb`:

- `render --version X [--output PATH]` — build `omm.rb` text for OMM version
  `X` from the current `pyproject.toml`.
- `check --formula PATH --version X [--allow-version-lag]` — compare an
  existing Formula against what `render` would produce; exits non-zero with a
  readable diff of any drifted pin or hash. `--allow-version-lag` compares
  only the dependency pin set and hashes, not the OMM version/main sdist —
  the Tap's own release cadence (see above) is allowed to lag `main`.
- `pypi-latest` — print the latest published, non-yanked `omm-model` version.

A dependency guarded by an environment marker that Homebrew's declared
interpreter (`python@3.14`) does not satisfy — e.g. `tomli; python_version <
'3.11'` — is left out of the generated resource list with an explanatory
comment. A marker shape the script does not understand fails loudly instead
of being silently dropped.

Two CI hooks keep the Formula from drifting again:

- `.github/workflows/ci.yml`'s `homebrew-formula` job checks out the Tap
  read-only and runs `check --allow-version-lag` on every push and pull
  request to `main`, so a `pyproject.toml` change that the Tap has not caught
  up to yet is visible immediately, without needing a release to notice.
- `.github/workflows/release.yml`'s `render-homebrew-formula` job (tag
  releases only, after the public PyPI install path is verified) renders
  `omm.rb` for the released version and uploads it as a workflow artifact.
  It never pushes to the Tap directly — a maintainer opens the Tap PR from
  that artifact when the dispatch-driven flow above needs a manual assist.
