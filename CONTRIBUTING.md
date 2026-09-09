# Contributing to omm

Thank you for helping improve omm. Contributions can include bug reports,
documentation, tests, runner compatibility work, packaging, focused code
changes, and benchmark data.

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report suspected vulnerabilities
privately as described in [SECURITY.md](SECURITY.md), not in a public issue or
pull request.

## Contributing benchmark data (no code required)

`omm contribute` repeatedly installs, benchmarks, and uploads models that fit
your hardware, growing the dataset that trains the recommendation model. It is
opt-in and anonymous: model names, file paths, and IP addresses are never sent.
The recommendation model currently learns from a narrow range of machines, so
runs on uncommon hardware (older GPUs, ARM boards, high-core-count CPUs, large
unified-memory systems) are especially valuable. See [PRIVACY.md](PRIVACY.md)
for the exact fields and [README.md](README.md) for disk-space and daemon
handling.

## Development setup

The installed CLI supports Python 3.10 or newer. **Until 2026-09-06 the
dependencies are version-frozen to the project's contest submission, and the
`dev`/`server` extras in that frozen set require Python 3.12+** (`numpy==2.5.2`
and its peers publish no 3.11 wheel). Use a 3.12+ interpreter for development
during the freeze; the runtime-only install still works on 3.10/3.11.

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

Core CI runs the test suite on Python 3.12 across Windows, macOS, and Ubuntu
(3.11 until the 2026-09-06 dependency freeze lifts), plus a bare runtime install
on 3.11, installer/uninstaller checks, a Linux container build, and Firebase
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
6. Write the PR description **in plain Korean first**, using the four
   headings from `.github/PULL_REQUEST_TEMPLATE.md` in this order:
   `## 한줄 요약`, `## 배경`, `## 무엇을 바꿨나`, `## 어떻게 확인했나`.
   Every contributor here develops with an AI agent, and an AI-written
   English body tells a teammate who was not in that session nothing about
   where the change came from. `## 배경` must give that context (the issue,
   the bug, the review, the conversation). English technical detail may
   follow the Korean sections. The `PR 설명 확인` check enforces the headings
   and a minimum amount of Korean text; bot PRs (`retrain/*`, the
   `beta` → `main` sync) are exempt. Commit subjects may stay English.
7. Respond to review without mixing unrelated cleanup into the same PR.

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
