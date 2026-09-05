# npm distribution design

Issue: [#146](https://github.com/omm-hippo/omm/issues/146)

## Status and boundary

The recommended public launcher name is `@omm-hippo/omm`. A scoped package
belongs to an npm user or organization namespace, so it makes the official
project owner clearer than the unrelated PyPI distribution name.

The source manifests intentionally contain `"private": true`. They are design
and validation inputs, not publishable packages. Release automation creates
separate publishable copies only after checking the signed release identity,
file allowlists, package metadata, and native binary formats. The checked-in
guard is never removed.

The npm package never runs Python, pip, pipx, a remote installer, or a network
request from `preinstall`, `install`, or `postinstall`.

## Package layout

`@omm-hippo/omm` is a small JavaScript launcher. Its exact-version
`optionalDependencies` select one standalone binary package:

| Target | npm package | Current support boundary |
|---|---|---|
| macOS Apple Silicon | `@omm-hippo/omm-darwin-arm64` | published at 0.3.41 with registry signature and provenance; hosted CI install/run/update/uninstall verified from the registry; no physical-device registry run yet |
| macOS Intel | `@omm-hippo/omm-darwin-x64` | published at 0.3.41 with registry signature and provenance; hosted CI install/run/update/uninstall verified from the registry; physical device not yet verified |
| Linux x64 glibc | `@omm-hippo/omm-linux-x64-gnu` | published at 0.3.41 with registry signature and provenance; hosted CI install/run/update/uninstall verified from the registry; physical device not yet verified |
| Linux arm64 glibc | `@omm-hippo/omm-linux-arm64-gnu` | published at 0.3.41 with registry signature and provenance; hosted CI install/run/update/uninstall verified from the registry; physical device not yet verified |
| Windows x64 | `@omm-hippo/omm-win32-x64` | published at 0.3.41 with registry signature and provenance; verified by tarball smoke in CI and manually on a physical Windows 11 x64 machine (install, `omm --version`, update guidance, uninstall, `npm audit signatures`); the CI registry-probe step (`Verify public npm path on win32-x64`) was broken by pwsh var expansion (issue #237), fixed in PR #249, not yet exercised by a real tag release |

An optional dependency is allowed to be absent when it does not match the
current operating system. The launcher must still fail clearly if the matching
package is absent, such as when the platform package directory is deleted,
blocked, or the platform is unsupported. `--omit=optional` does not reproduce
this: on a bare global install spec, npm 11.12.1 and 11.19.0 both still install
the matching platform package, since optional deps of a dependency are not
pruned by that flag. `packaging/npm/launcher/test/launcher.test.js` simulates
absence directly by deleting the platform package directory.

The launcher checks package name, OMM version, OS, CPU, libc where relevant,
target identifier, and binary containment before starting the executable. It
passes the verified package root to OMM. OMM independently checks the npm
manifest and confirms that the current executable is the declared binary
before reporting the installation source as npm.

## Update policy

An npm-managed executable never switches itself to a Git or pipx installation.
`omm update` exits without modifying files and prints:

```text
npm update --global @omm-hippo/omm
```

## Validation before publishing

The validation-only workflow tests Node.js 22 and 24, the current LTS lines, on
GitHub-hosted Windows x64, macOS arm64, macOS Intel, Ubuntu x64, and Ubuntu
arm64 runners. It runs launcher tests, inspects `npm pack --dry-run`, and checks
the Python/npm metadata contract. It has read-only permissions and contains no
`npm publish` command or OIDC write permission.

`scripts/npm_package.py` rejects symlinked binaries, wrong executable formats,
binaries whose header declares a different CPU architecture than the target
(Mach-O cputype, ELF `e_machine`, PE `Machine`, plus universal Mach-O files),
version drift, unexpected files, missing licenses, package identity drift, and
install lifecycle scripts. The validation workflow stages private packages. A
separate native build job freezes the actual OMM command on macOS arm64/Intel
and glibc Linux x64/arm64, installs private launcher/platform tarballs, and
exercises version, help, npm-managed update guidance, and uninstall.

The separate `npm-release.yml` workflow builds all five native targets from an
exact OMM source revision, creates the launcher last, verifies the complete
six-package bundle and its SHA-256 manifest, and exercises install, execution,
npm-managed update guidance, and uninstall on every target before publishing.
Release tags must be signed, match `pyproject.toml`, and point into `main`.

The publish job is disabled unless the repository variable
`NPM_TRUSTED_PUBLISHING` is exactly `enabled`. It also uses the protected `npm`
GitHub Environment. Only that job receives `id-token: write`; no npm token is
accepted by the workflow. Platform packages publish before the launcher. A
rerun accepts an existing version only when the registry copy matches its own
published integrity and every file it packs is byte-identical to the freshly
built, validated tarball; the gzip envelopes are allowed to differ because
`npm pack` is not byte-reproducible across hosts. Destination jobs then install from npmjs, verify
registry signatures and provenance, run OMM, check update guidance, and remove
the package on all five hosted targets.

## Registry setup

The `omm-hippo` organization has been created and its owner enabled account
two-factor authentication. All six packages (`@omm-hippo/omm` and the five
platform packages) have completed the one-time bootstrap: they exist on the
public registry at 0.3.41 (and earlier at 0.3.33) with correct os/cpu/libc
metadata, registry signatures, and SLSA provenance attestations, published by
the `npm-release.yml` OIDC job on 2026-09-01. `main` is currently at 0.3.44,
untagged and unpublished — the next tag push exercises the bootstrap-completed
path again, not a first-time one.

Each launcher/platform package now has its own npm Trusted Publisher
configuration, evidenced by the OIDC job publishing successfully for tags
v0.3.33 and v0.3.41. The GitHub settings are:

- Organization: `omm-hippo`
- Repository: `omm`
- Workflow filename: `npm-release.yml`
- Environment: `npm`
- Allowed action: publish, or staged publish if the release is staged

Trusted Publishing currently requires a GitHub-hosted runner, `id-token: write`,
Node.js 22.14 or newer, and npm 11.5.1 or newer. Publishing through it creates
provenance automatically. `npm-release.yml` pins npm 11.19.0 for publishing.

The one-time bootstrap (npm requires a package to exist before a Trusted
Publisher can be configured, and staged publishing cannot create a brand-new
package) is done for all six packages. It still applies as a rule for any
future new package: a brand-new package name needs an explicitly approved,
2FA-gated bootstrap publish before its Trusted Publisher can be configured.
The repository variable `NPM_TRUSTED_PUBLISHING` gates the protected OIDC job
for all publishes after bootstrap. The launcher must not become public before
every matching platform package exists.

## Verification levels

- **Implemented:** launcher, target contract, private and publishable staging
  packagers, npm install-source detection, validation workflow, and gated
  release workflow exist.
- **Unit-verified:** focused Node and Python tests, package allowlist checks,
  workflow lint, and workflow security audit pass locally.
- **Simulator-verified:** clean installation, execution, update, and
  uninstall using built private package tarballs on the four native hosted
  OS/CPU paths.
- **Release-verified (hosted CI, tags v0.3.33 and v0.3.41):** first package
  bootstrap, Trusted Publisher configuration, the protected `npm` Environment,
  public publishing, provenance attestation, and every release-workflow hosted
  job (build, bundle, tarball smoke on 5 targets, publish, `verify-registry`)
  passed — except the win32-x64 registry-verify leg (issue #237, fixed in PR
  #249, not yet exercised by a real tag release). Public install, run,
  `omm update` guidance (exit 1, no `OMM_HOME/src` mutation), `npm audit
  signatures`, and uninstall are verified from the registry on all four hosted
  POSIX targets (macOS arm64/Intel, Linux x64/arm64 glibc).
- **Physical-device-verified:** public registry install verified on a physical
  Windows 11 x64 machine (Node 24.11, npm 11.12.1): install, `omm --version`,
  `omm update` guidance, `npm audit signatures` (2 verified signatures, 2
  verified attestations), and uninstall.
- **Upgrade-verified (local, win32-x64):** the real upgrade path between two
  published versions — install 0.3.33 from the registry into a global
  prefix, confirm `omm --version` reports it, `npm install --global
  @omm-hippo/omm@0.3.41` into the *same* prefix, confirm the version and the
  resolved platform package (`@omm-hippo/omm-win32-x64`) both moved to
  0.3.41 and 0.3.33 no longer appears in `npm ls --global --all --json` —
  has been exercised locally on Windows today via `scripts/npm_release.py
  smoke-registry --previous-version`. The `verify-registry` CI job now
  passes `--previous-version auto` on every target, so the next real tag
  release exercises this same upgrade path in hosted CI automatically, not
  just locally.
- **Not verified / 미검증:** the win32-x64 registry-verify leg in CI after
  #249 (no real tag release has exercised it yet); physical Intel Mac;
  physical Linux; the upgrade smoke above running in hosted CI (wired for
  the next release, not yet exercised by a real tag push).
