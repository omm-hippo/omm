# npm distribution design

Issue: [#146](https://github.com/omm-hippo/omm/issues/146)

## Status and boundary

The recommended public launcher name is `@omm-hippo/omm`. A scoped package
belongs to an npm user or organization namespace, so it makes the official
project owner clearer than the unrelated PyPI distribution name.

The source manifests intentionally contain `"private": true`. They are design
and validation inputs, not publishable packages. Do not remove that guard until
the npm organization, package ownership, two-factor authentication, protected
GitHub Environment, and Trusted Publisher have been verified.

The npm package never runs Python, pip, pipx, a remote installer, or a network
request from `preinstall`, `install`, or `postinstall`.

## Package layout

`@omm-hippo/omm` is a small JavaScript launcher. Its exact-version
`optionalDependencies` select one standalone binary package:

| Target | npm package | Current support boundary |
|---|---|---|
| macOS Apple Silicon | `@omm-hippo/omm-darwin-arm64` | private local tarball path exercised; hosted CI and public package not yet verified |
| macOS Intel | `@omm-hippo/omm-darwin-x64` | validation job defined; hosted CI, physical device, and public package not yet verified |
| Linux x64 glibc | `@omm-hippo/omm-linux-x64-gnu` | validation job defined; hosted CI and public package not yet verified |
| Linux arm64 glibc | `@omm-hippo/omm-linux-arm64-gnu` | validation job defined; hosted CI and public package not yet verified |
| Windows x64 | `@omm-hippo/omm-win32-x64` | artifact work is owned by the separate Winget task |

An optional dependency is allowed to be absent when it does not match the
current operating system. The launcher must still fail clearly if the matching
package is absent, such as when npm was run with `--omit=optional`.

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
version drift, unexpected files, missing licenses, package identity drift, and
install lifecycle scripts. Staged packages remain private. A separate native
build job freezes the actual OMM command on macOS arm64/Intel and glibc Linux
x64/arm64, installs private launcher/platform tarballs, and exercises version,
help, npm-managed update guidance, and uninstall. Windows remains excluded
until the separate Winget task has a verified public portable artifact.

## Registry setup required later

At the time this design was added, local `npm whoami` returned `ENEEDAUTH`, the
`omm-hippo` organization lookup returned `Scope not found`, and both
`@omm-hippo/omm` and `omm-model` returned registry 404 responses. A 404 does not
reserve a name.

Once the organization exists, each launcher/platform package needs its own npm
Trusted Publisher configuration. The intended GitHub settings are:

- Organization: `omm-hippo`
- Repository: `omm`
- Workflow filename: a future `npm-release.yml`
- Environment: `npm`
- Allowed action: publish, or staged publish if the release is staged

Trusted Publishing currently requires a GitHub-hosted runner, `id-token: write`,
Node.js 22.14 or newer, and npm 11.5.1 or newer. Publishing through it creates
provenance automatically. The publish job must receive OIDC only after all
platform packages have been built and verified, and the launcher must be made
available only after the matching platform versions exist.

## Verification levels

- **Implemented:** launcher, target contract, private staging packager, npm
  install-source detection, and validation-only workflow exist.
- **Unit-verified:** focused Node and Python tests, package allowlist checks,
  workflow lint, and workflow security audit pass locally.
- **Simulator-verified:** requires clean installation, execution, update, and
  uninstall using built private package tarballs on the four native hosted
  OS/CPU paths. This does not prove public registry publishing.
- **Physical-device-verified:** the private local tarball path has been exercised
  on an Apple Silicon Mac: install, `omm --version`, `omm --help`, npm-managed
  update guidance without Git mutation, and uninstall. This does not verify the
  public registry path.
- **Not verified / 미검증:** npm organization ownership, Trusted Publisher,
  public publishing, provenance destination results, hosted multi-platform jobs,
  public install/upgrade/uninstall, Intel Mac, and Linux remain unverified.
