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
