# Contributing to omm

Thank you for helping improve omm. Contributions can include bug reports,
documentation, tests, runner compatibility work, packaging, focused code
changes, and benchmark data.

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report suspected vulnerabilities
privately as described in [SECURITY.md](SECURITY.md), not in a public issue or
pull request.

## Choose a contribution

| You want to help with | Start here |
| --- | --- |
| A bug, installation problem, or feature request | [Issue templates](https://github.com/omm-hippo/omm/issues/new/choose) |
| Documentation or code | [Development setup](#development-setup), then [Pull request workflow](#pull-request-workflow) |
| Recommendation data from your hardware | [Contributing benchmark data](#contributing-benchmark-data-no-code-required) |
| A suspected vulnerability | [Private security reporting](SECURITY.md#reporting-a-vulnerability) |
| A community conduct concern | [Code of Conduct reporting](CODE_OF_CONDUCT.md#reporting-an-issue) |

## Contributing benchmark data (no code required)

`omm contribute` repeatedly installs, benchmarks, and uploads models that fit
your hardware, growing the dataset that trains the recommendation model. It is
opt-in. Benchmark records include model identifiers, available source and
file metadata, hardware characteristics, and measurement results. The hosted
benchmark dataset is publicly readable; do not treat contributions as private
model inventory. File paths and generated model text are excluded from the
payload. Usage statistics and crash reports have separate controls.

Runs on varied hardware help broaden the dataset. Before starting, review
[PRIVACY.md](PRIVACY.md#1-benchmark-telemetry--omm-setting-upload-benchmark)
and the [disk-space and daemon behavior](README.md#scripting). The command
downloads models and performs sustained local computation. Use
`omm setting upload benchmark --ask` to request consent for each contribution
session, or `--disable` to prevent benchmark uploads.

## Development setup

The package supports Python 3.10 or newer. The core CI test suite runs on
Python 3.12; a separate job installs the runtime-only package on 3.11.

```sh
git clone https://github.com/omm-hippo/omm.git
cd omm
python -m venv .venv
```

Activate the environment on macOS or Linux:

```sh
source .venv/bin/activate
```

Or in Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install the development and training-test dependencies used by CI:

```sh
python -m pip install --upgrade pip
python -m pip install -e ".[dev]" -r requirements-train.txt
```

Run the core checks with disposable home, model-hub, and cache directories.
Some update tests resolve paths from the home directory when Python imports
the CLI, so changing only `OMM_HOME` is not sufficient to protect an existing
installation. On macOS/Linux, run from the repository root:

```sh
test_root="$(mktemp -d)"
mkdir -p "$test_root/home" "$test_root/cache"
env HOME="$test_root/home" OMM_HOME="$test_root/home/.omm" \
  XDG_CACHE_HOME="$test_root/cache" PYTHONPATH="$PWD/src" \
  python -m pytest -q
env HOME="$test_root/home" OMM_HOME="$test_root/home/.omm" \
  XDG_CACHE_HOME="$test_root/cache" PYTHONPATH="$PWD/src" \
  python -m omm.cli --help
```

On Windows, use a disposable PowerShell session or CI environment and set
`HOME`, `USERPROFILE`, `OMM_HOME`, `APPDATA`, `LOCALAPPDATA`, and
`XDG_CACHE_HOME` to directories under a dedicated temporary root **before**
starting Python. Set `PYTHONPATH` to the checkout's `src` directory. Close
that session after testing so these overrides do not affect later commands.

The examples below assume this isolation is in place. Do not run install or
uninstall scripts against your daily installation as a test. Exercise local
fixtures, mocks, or a disposable VM for paths that install software, start
runners, or register background services.

## Project layout

- `src/omm/` — CLI, hardware detection, model management, runner integration,
  and trust logic
- `src/localfit_server/` — optional benchmark/telemetry collector
- `tests/` — Python pytest suite
- `cf-worker/` — hosted telemetry gateway and Firebase-facing Worker
- `packaging/npm/` — npm launcher and native-package definitions
- `scripts/` — release, training, validation, and maintenance tooling
- `published/` — generated recommendation artifacts, signed manifests, and
  candidate catalog
- `.github/workflows/` — CI, package, release, and training automation
- `docs/` — design notes and validation evidence

## Releasing

Merging to `main` does not publish anything. A maintainer pushes a signed
`vX.Y.Z` tag, and that tag push drives PyPI, npm, the GitHub Release, the
Windows portable build, and the Homebrew Tap notification. The step-by-step
runbook, the automatic pipeline, and what to do when a job fails are in
[docs/release-process.md](docs/release-process.md).

## Checks for the area you changed

Core CI runs the test suite on Python 3.12 across Windows, macOS, and Ubuntu,
plus a bare runtime install on 3.11, installer/uninstaller checks, a Linux
container build, and Firebase rules tests. Path-scoped workflows may also run
runner integration checks, npm packaging, Python/npm release builds, and the
Windows portable build.

Run the checks relevant to your change before opening a pull request:

```sh
# Python behavior
python -m pytest -q

# Cloudflare Worker (run from the repository root)
cd cf-worker
npm ci
npm test
npx tsc -p tsconfig.json
cd ..

# npm launcher and package contract
npm --prefix packaging/npm/launcher test
python scripts/npm_package.py validate
```

Documentation-only changes do not prove runtime behavior. In the pull request,
state exactly what you ran and separate these levels when they apply:

- **Implemented** — the code or documentation exists
- **Unit-verified** — focused automated tests passed
- **Simulator-verified** — a user path ran in a simulator or emulator
- **Physical-device-verified** — a user path ran on real hardware
- **Not verified / 미검증** — name the unexercised path and reason

## Command docs stay in sync

`src/omm/cli.py` is the single source of truth for the command surface. Three
places describe it to users, and all three are derived from that one file:

1. `omm <command> --help` — rendered by Click from the command objects, with a
   `More about omm <command>: https://omm.run/commands/...` footer added
   centrally (no per-command decorator to update).
2. `README.md` `## Usage` — hand-written, but checked against the CLI.
3. <https://omm.run/commands> — the website renders `docs/commands.json`, which
   is generated from the CLI and copied into the omm.run repository.

After adding, renaming, or re-documenting any command, regenerate the data and
verify all three:

```sh
python scripts/export_command_reference.py   # rewrites docs/commands.json
python scripts/check_docs_sync.py            # what CI job `docs-sync` runs
```

`check_docs_sync.py` fails when `docs/commands.json` no longer matches the CLI,
when the README `## Usage` section is missing a command, names a command that
does not exist, or passes a flag a command does not have. It also compares the
omm.run copy of the file, but only prints a warning for that one — the website
lives in another repository and is updated by its own pull request.

Commit the regenerated `docs/commands.json` together with the CLI change.

## Pull request workflow

1. Branch from the latest `main` and keep the change focused.
2. Add or update tests for behavior changes.
3. Do not hand-edit generated recommendation files such as
   `published/localfit-recommend-model.json`; use the owning script or workflow.
4. Run the relevant checks above and record the results in the PR description.
5. Explain user-visible behavior, compatibility impact, and remaining
   verification boundaries.
6. Write the PR description **in plain Korean first**, using the four
   headings from `.github/PULL_REQUEST_TEMPLATE.md` in this order:
   `## 한줄 요약`, `## 배경`, `## 무엇을 바꿨나`, `## 어떻게 확인했나`.
   Every contributor here develops with an AI agent, and an AI-written
   English body tells a teammate who was not in that session nothing about
   where the change came from. `## 배경` must give that context (the issue,
   the bug, the review, the conversation). English technical detail may
   follow the Korean sections. The `PR 설명 확인` check enforces the headings
   and a minimum amount of Korean text; bot PRs (`retrain/*`, `emergency-signal/*`,
   the `beta` → `main` sync) are exempt. Commit subjects may stay English.
7. Respond to review without mixing unrelated cleanup into the same PR.

## Trusted pull-request head

Branch protection validates the exact PR head commit using the verifier and
SSH allowed-signers file from the protected base branch. Direct pushes to
`main` remain disabled.

External contributors do not need a maintainer signing key. After review, a
maintainer supplies the final trusted SSH-signed tip before merge. Every
subsequent commit changes that tip and must pass the check again. To catch up
with the base branch, a maintainer merges it locally and signs the resulting
commit with an allowed SSH key. GitHub's **Update branch** button creates a
web-flow-signed commit that does not satisfy this repository's SSH trust
anchor.

Maintainers can update the PR branch locally, with their configured SSH
signing key:

```sh
git fetch origin main
git merge --no-ff -S origin/main
```

Verify the tip against the allowed signers from the protected base, rather
than a copy the PR could have changed (macOS/Linux example):

```sh
trusted_signers="$(mktemp)"
git show origin/main:src/omm/trust/allowed_signers > "$trusted_signers"
git -c gpg.format=ssh \
  -c gpg.ssh.allowedSignersFile="$trusted_signers" \
  verify-commit HEAD
rm "$trusted_signers"
```

The required GitHub check is `Trusted PR head / Trusted PR head` from
`.github/workflows/trusted-head.yml`. Do not bypass or weaken it to merge a
change.

If a web-flow-signed commit is already the tip, first inspect its changes,
then add a trusted SSH-signed follow-up commit and verify that exact head.
An empty endorsement commit is sufficient when no code changes are needed.
Preserve the shared branch history and let CI run on the new head.

## Commit messages

Use a short subject that explains why the change exists. Conventional prefixes
such as `feat:`, `fix:`, `docs:`, `refactor:`, and `test:` are encouraged but
not required.

## Bugs and feature requests

Use the GitHub issue templates. A useful bug report includes:

- operating system and architecture
- Python or Node.js version, depending on the installation path
- `omm --version` output and installation method
- affected local runner and version, if applicable
- exact command, expected behavior, actual behavior, and redacted logs

## License and Developer Certificate of Origin

Contributions are accepted under the project's [MIT License](LICENSE).
Downloaded models and third-party runner applications retain their own
licenses and terms.

This project uses the [Developer Certificate of Origin](DCO) (DCO 1.1) to
record that each contributor has the right to submit their contribution under
the MIT License. It is a lightweight assertion, not a copyright-assignment or
contributor-license agreement — you keep the copyright to your work.

Certify the DCO by adding a `Signed-off-by` trailer to every commit, using a
real name and an email address you can be reached at:

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` (or `git commit --signoff`) appends this line automatically
from your configured `user.name` / `user.email`. To sign off a branch of
commits you already made, use `git rebase --signoff <base>`. Amend the last
commit with `git commit --amend -s --no-edit`.

The DCO sign-off is separate from the SSH commit signature described under
[Trusted pull-request head](#trusted-pull-request-head): the signature proves
who pushed the commit, the sign-off records the licensing certification. A
maintainer may add a missing sign-off on your behalf before merge. This is
not currently enforced by a CI check.
