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

## Who decides the Formula's resources: Homebrew, not this repository

Issue [#238](https://github.com/omm-hippo/omm/issues/238) tried to make the
Tap's `resource` stanzas a generated copy of `requirements-npm-binary.txt`, with
a `homebrew-formula` CI job in this repository failing whenever the two
differed. That could not stay green. The Tap's own `test-bot` job
(`.github/scripts/verify_python_resources.py`) requires the stanzas to equal
what `brew update-python-resources` resolves from the *published* `omm-model`
metadata, whose runtime dependencies are `>=` floors; this repository's
pins come from an unreleased `main`. Two independent resolvers can only agree by
luck, and every dependency release or `main` commit ahead of the last release
turned one side red (the Tap's open `omm 0.3.105` bump PR stalled for a month
that way).

So Homebrew is the single source of truth. The Tap's `autobump` workflow and
`brew test-bot` decide the `resource` stanzas; this repository no longer
compares the Formula against anything and has no CI job for it. A release only
sends the dispatch described above.

`scripts/homebrew_formula.py` stays as a manual recovery aid: the
`.github/workflows/release.yml` `render-homebrew-formula` job (tag releases
only) uploads an `omm.rb` rendered from the pins in
`requirements-npm-binary.txt`. It never pushes to the Tap. Its stanzas can
differ from what the Tap's `test-bot` resolves, so treat it as a starting point
and fix the Tap PR until `test-bot` is green, not the other way round.
