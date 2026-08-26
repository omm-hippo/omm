# Contributing to omm

Thank you for helping improve omm. Contributions can include bug reports,
documentation, tests, runner compatibility work, packaging, and focused code
changes.

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report suspected vulnerabilities
privately as described in [SECURITY.md](SECURITY.md), not in a public issue or
pull request.

## Development setup

The package supports Python 3.10 or newer. Python 3.11 matches the core CI
environment.

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

Run the core checks:

```sh
python -m pytest -q
omm --help
```

Use a temporary `OMM_HOME` for manual development checks so local models and
settings are not mixed with test state:

```sh
export OMM_HOME="$(mktemp -d)"  # macOS/Linux example
```

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

## Checks for the area you changed

Core CI always runs Python 3.11 on Windows, macOS, and Ubuntu, a bare runtime
install, installer/uninstaller checks, a Linux container build, and Firebase
rules tests. Path-scoped workflows may also run runner integration checks,
npm packaging, Python/npm release builds, and the Windows portable build.

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

## Pull request workflow

1. Branch from the latest `main` and keep the change focused.
2. Add or update tests for behavior changes.
3. Do not hand-edit generated recommendation files such as
   `published/localfit-recommend-model.json`; use the owning script or workflow.
4. Run the relevant checks above and record the results in the PR description.
5. Explain user-visible behavior, compatibility impact, and remaining
   verification boundaries.
6. Respond to review without mixing unrelated cleanup into the same PR.

## Trusted pull-request head

Branch protection validates the exact PR head commit using the verifier and
SSH allowed-signers file from the protected base branch. Direct pushes to
`main` remain disabled.

External contributors do not need a maintainer signing key. After review, a
maintainer supplies the final trusted SSH-signed tip before merge. Once that
tip is signed, do not use GitHub's **Update branch** button or add another
commit: either action changes the exact head and requires a new trusted
signature.

Maintainers can verify the current tip locally with:

```sh
git -c gpg.format=ssh \
  -c gpg.ssh.allowedSignersFile=src/omm/trust/allowed_signers \
  verify-commit HEAD
```

The required GitHub check is `Trusted PR head / Trusted PR head` from
`.github/workflows/trusted-head.yml`. Do not bypass or weaken it to merge a
change.

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

## License

Contributions are accepted under the project's [MIT License](LICENSE).
Downloaded models and third-party runner applications retain their own
licenses and terms.
