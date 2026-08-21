# omm — Open source Model Manager

`omm` is an apt/brew-style package manager for local LLMs (GGUF). It installs models into a central hub, links them into seven local AI runners automatically (Ollama, LM Studio, Jan, AnythingLLM, Msty, text-generation-webui, KoboldCpp), and can recommend a model that fits your hardware.

## Table of contents

- [Install](#install)
  - [Verified Git-source installer (macOS / Linux)](#verified-git-source-installer-macos--linux)
  - [PyPI (macOS, Linux, and Windows)](#pypi-macos-linux-and-windows)
  - [Homebrew Tap (macOS)](#homebrew-tap-macos)
  - [Verified Git-source installer (Windows PowerShell)](#verified-git-source-installer-windows-powershell)
  - [Not currently public installation paths](#not-currently-public-installation-paths)
- [Usage](#usage)
  - [Setup & discovery](#setup--discovery)
  - [Install & manage models](#install--manage-models)
  - [Verify & benchmark](#verify--benchmark)
  - [Update & configuration](#update--configuration)
  - [Scripting](#scripting)
- [Self-hosted benchmark data](#self-hosted-benchmark-data)
- [Signed recommendation data](#signed-recommendation-data)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Install

### Verified Git-source installer (macOS / Linux)

```sh
curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh | sh
```

This bootstraps `python3`, `git`, and `pipx` if missing, then installs `omm` as an isolated CLI via `pipx`. On macOS it uses Homebrew when Python 3.10+ or git is missing; if Homebrew is not installed, it bootstraps Homebrew with Homebrew's official installer. On Linux it supports `apt-get`, `dnf`, `yum`, `pacman`, and `apk` when the current user can install system packages. On macOS, the installer also persists pipx's executable directory in `~/.zprofile`, so a newly opened zsh finds `omm` automatically.

Homebrew requires a supported macOS and Apple's Xcode Command Line Tools. To require a pre-existing Homebrew installation instead of allowing the installer to bootstrap it, export `OMM_AUTO_INSTALL_HOMEBREW=0` before running the command.

### PyPI (macOS, Linux, and Windows)

```sh
# macOS / Linux (Python 3.10+ and pip must already be installed)
python3 -m pip install omm-model

# Windows (Python 3.10+ and pip must already be installed)
py -m pip install omm-model
```

This does not go through the signed-commit verification described below; it
relies on PyPI's own account security and TLS, the same trust model as
installing any other PyPI package. It is a package-manager path, not a
zero-prerequisite installer: install Python and pip first on a clean computer.

For an isolated command-line installation, `pipx` is recommended:

```sh
# If pipx is not installed yet, install it with the same Python first.
python3 -m pip install --user pipx
python3 -m pipx ensurepath
python3 -m pipx install omm-model
```

On Windows, use `py -m pip`, `py -m pipx`, and `py -m pipx ensurepath` instead.
If the operating system marks Python as externally managed, install pipx from
the operating system package manager or use the Git-source installer above.

The distribution name is `omm-model`; the installed command and Python import
remain `omm`. Upgrade and remove it with the same tool that installed it:

```sh
# macOS / Linux
python3 -m pip install --upgrade omm-model
python3 -m pip uninstall omm-model

# Windows
py -m pip install --upgrade omm-model
py -m pip uninstall omm-model

# Or, for pipx:
pipx upgrade omm-model
pipx uninstall omm-model
```

### Homebrew Tap (macOS)

```sh
brew install omm-hippo/omm/omm
```

Upgrade or remove the formula with Homebrew. Removing the formula preserves
downloaded models and settings under `OMM_HOME`:

```sh
brew upgrade omm-hippo/omm/omm
brew uninstall omm-hippo/omm/omm
```

The Homebrew formula and PyPI package can move on separate release schedules;
use `brew info omm-hippo/omm/omm` to see the version currently provided by the
Tap. `omm update` does not modify a Homebrew installation and instead prints
the matching `brew upgrade` command.

### Not currently public installation paths

The npm launcher and platform packages are still private release artifacts;
their public registry packages have not been published yet. The Windows
portable/Winget files are built and tested as release artifacts, but a public
Winget package is not currently documented or verified. Do not use either path
as a user installation command yet.

### Verified Git-source installer (Windows PowerShell)

```powershell
# This must run before irm: script-internal TLS settings are too late for its first download.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1 | iex
```

This bootstraps Python and git via [winget](https://learn.microsoft.com/en-us/windows/package-manager/winget/) if missing (built into Windows 10 2004+ and Windows 11 — on older Windows, install [Python 3.10+](https://www.python.org/downloads/) and [git](https://git-scm.com/downloads) manually first), then installs `omm` through that exact validated Python interpreter. Open a new PowerShell window afterward so your `PATH` picks up `omm`. On Windows, model exposure tries an unprivileged same-volume hard link first, then a symbolic link (Developer Mode or Administrator), then an owned copy. Before copying, omm checks destination free space and reports that the model now consumes additional bytes. File junctions do not apply because model targets are files, not directories.

Requirements: Python 3.10+. The optional NVIDIA detector is installed only when `nvidia-smi` indicates an NVIDIA driver.

### Supported platforms

`omm` is tested in CI on Windows, macOS, and Linux with Python 3.10+. Windows 10 22H2/11 is the supported Windows baseline because that matches Ollama's native Windows requirements. Hardware scan, install, linking, benchmark, update, and contribution flows are cross-platform; Ollama remains the only benchmark engine.

Both installers clone to a versioned staging directory, verify the signed commit against a bootstrap trust anchor, and only then switch pipx to it. Do not replace this with an unverified `git clone` plus `pipx install` if commit authenticity matters.

`omm update` updates only a canonical OMM Git-source installation. For a PyPI or
pipx installation it leaves files unchanged and prints the matching package
manager upgrade command. The Git-only beta channel is likewise unavailable to
package-managed installations.

### Package-channel verification

| Installation path | Highest verified level | Remaining limitation |
|---|---|---|
| PyPI / pipx | Simulator-verified on GitHub-hosted Windows, macOS, and Ubuntu runners using the public package | A real upgrade from the first public release remains a separate user-path check |
| Homebrew Tap | Physical-device-verified on an Apple Silicon Mac for public Tap install, `omm --version`, `brew test`, upgrade guidance, and uninstall | Intel Mac installation is not yet physical-device-verified |

Additional package-manager commands are added here only after their public
registry path has been installed and verified.

### Local AI runners

The first bare `omm` run on a fresh install (or `omm setup` any time after) shows a hardware summary and a checklist of local AI runners. Checking one that omm knows how to install runs its official installer with live progress in the terminal; checking one it doesn't yet automate on your platform prints a link instead. Automation coverage today:

| Runner | Automated on | Manual elsewhere |
|---|---|---|
| Ollama | macOS, Linux, Windows | — |
| LM Studio | macOS, Linux, Windows (headless `lms` CLI) | — |
| Jan | macOS (Homebrew), Windows (winget), Linux (Flatpak) | wherever that package manager isn't installed |
| AnythingLLM | macOS (Homebrew), Windows (winget) | Linux |
| Msty | macOS (Homebrew) | Windows, Linux |
| KoboldCpp | macOS (Apple Silicon), Linux (x86_64), Windows | Intel Mac, other architectures |
| text-generation-webui | macOS (any arch), Linux/Windows (x86_64) | ARM Linux/Windows |

Every currently-installed runner is also listed (marked as already installed, not selectable) rather than hidden, so the checklist always reflects what omm actually detects on the machine.

### Storage location

The model hub and omm state default to `~/.omm`. Set `OMM_HOME` before installation and on later runs to put them on another volume:

```powershell
[Environment]::SetEnvironmentVariable("OMM_HOME", "D:\omm", "User")
$env:OMM_HOME = "D:\omm"
```

```sh
export OMM_HOME=/mnt/models/omm
```

Ollama's own model location follows `OLLAMA_MODELS`. LM Studio follows its home pointer; set `OMM_LMSTUDIO_MODELS_DIR` when LM Studio uses a custom directory that omm cannot discover automatically.

### Completion and uninstall

Install native shell completion once, then restart the shell:

```powershell
omm --install-completion powershell
```

```sh
omm --install-completion bash  # or zsh/fish
```

For a PyPI installation, use `python -m pip uninstall omm-model`; for pipx,
use `pipx uninstall omm-model`. Both commands preserve downloaded models and
settings under `OMM_HOME`.

To remove a Git-source installer installation while preserving downloaded
models and settings:

```powershell
irm https://raw.githubusercontent.com/omm-hippo/omm/main/uninstall.ps1 | iex
```

```sh
curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/uninstall.sh | sh
```

Run a downloaded script with `-Purge` (PowerShell) or `--purge` (sh) to remove the model hub and settings too. Purge removes only known omm-owned paths and leaves unrelated files in a custom `OMM_HOME` untouched. Installers mark custom homes so uninstallers can refuse ambiguous or unsafe locations; shell profiles are never rewritten during uninstall.

## Usage

### Setup & discovery

```sh
omm setup  # First-run setup wizard: hardware scan + engine checklist (re-runnable any time)
omm scan [--json]  # Print a hardware, runner, and model summary (RAM, VRAM, OS)
omm recommend [--json]  # Suggest a model that fits this machine, then offer to install it
omm tune <name> [--json]  # Recommend context, GPU offload, threads, and batch size
omm search <query> [--json] [--skip-unfit] [--limit N] [--provider curated|huggingface|modelscope]  # Search curated models, cached candidates, and HuggingFace
omm help [command]  # Show help, same as --help
```

### Install & manage models

```sh
omm install <name> [--skip-unfit] [--upload/--no-upload] [--force] [--verify-runtime|--no-verify-runtime]  # Download, link, and optionally verify a model
omm import [directory] [--yes]  # Adopt GGUF files already sitting in Ollama/LM Studio (or a given directory) into the hub
omm uninstall <name> [--dry-run]  # Uninstall a model and clean up its symlinks/manifests (alias: rm)
omm uninstall all [--yes] [--dry-run]  # Uninstall every model installed via omm
omm list [--json] [--engine NAME]  # Show models installed via omm and their linked status (alias: ls)
omm info <name> [--json]  # Show a model's name, version, size, and linked-program run commands
omm upgrade <name> [--dry-run]  # Refresh a model against its source if it has changed since install (alias: up)
omm upgrade [--yes] [--dry-run]  # Check every installed model for updates
omm link [--engine NAME]  # Re-verify and repair every installed model's LM Studio/Ollama links
omm link <directory>  # Reuse central GGUF files; Windows warns if a real copy is required
omm autoremove  # Clean up broken symlinks and orphaned partial downloads
```

`install`, `uninstall`, `info`, and `upgrade` accept either a model name/reference or the numeric index shown by the last `omm search` or `omm list` run in that terminal. `search`/`install` mark models predicted not to run on this machine's hardware in red.

### Verify & benchmark

```sh
omm verify <name> [--engine ollama|lmstudio] [--keep-loaded]  # Prove local load + generation works
omm benchmark <name>...  # Local quality + speed smoke evidence for one or more installed models
omm contribute [--yes]  # Repeatedly install/benchmark/upload hardware-fit models to grow the dataset
```

`omm verify` checks more than a link: it asks before loading an unloaded model,
sends one short deterministic prompt to a server already running on this
computer, requires a non-empty answer, and releases only a model that OMM
loaded for the check. It never starts Ollama or LM Studio, deletes the model,
or stores the generated answer. LM Studio API authentication reads
`LM_API_TOKEN` from the process environment and never writes it to
`config.json`. Compatibility status is stored locally in `models.json` and is
shown by `omm info`.

### Update & configuration

```sh
omm update  # Update a canonical OMM Git-source install; package installs print their manager command
omm setting  # Interactive menu for telemetry, upload policy, error reports, version, theme, calibration, and catalog trust
omm setting version [--stable|--beta]  # Show or switch the update channel `omm update` pulls from
omm setting telemetry --endpoint <url>  # Configure where benchmark telemetry is sent
omm setting upload --enable|--disable|--ask  # Configure the benchmark-upload send policy
omm setting error-reports --enable|--disable|--ask  # Configure the opt-in crash/error-report send policy
omm setting memory-guard --policy ask|block|observe  # Protect Ollama loads from live memory pressure
omm setting theme [--set NAME]  # Show or change omm's output color theme
omm setting calibrate <name>  # Locally correct predicted speed with an installed Ollama model
omm setting catalog-trust --manifest-url <url> --public-key <key>  # Require signed recommendation downloads
omm setting catalog-status  # Show signed recommendation data and rollback snapshots
omm setting catalog-rollback  # Restore the most recent different recommendation snapshot
```

`omm verify` checks more than a link: it asks before loading an unloaded model,
sends one short deterministic prompt to a server already running on this
computer, requires a non-empty answer, and releases only a model that OMM
loaded for the check. It never starts Ollama or LM Studio, deletes the model,
or stores the generated answer. LM Studio API authentication reads
`LM_API_TOKEN` from the process environment and never writes it to
`config.json`. Compatibility status is stored locally in `models.json` and is
shown by `omm info`.

### Scripting

All errors, warnings, and confirmation prompts print to stderr; `--json` output (supported on `search`/`list`/`info`/`benchmark`/`tune`/`scan`/`recommend`) is the only thing written to stdout, so it's safe to pipe (e.g. `omm list --json | jq .`). Any command that would otherwise prompt for confirmation fails fast with a non-zero exit code when there's no terminal attached instead of hanging — pass `--yes`/`-y` (works on every command that has a confirmation prompt) or the relevant flag (`install --skip-unfit`, `install --upload`/`--no-upload`) to run it unattended.

Four global flags work either before or after the subcommand name (`omm --json search foo` and `omm search foo --json` are equivalent):

- `--json` — structured output, where supported (see above)
- `--yes` / `-y` — skip confirmation prompts
- `--quiet` / `-q` — suppress progress bars and background status/hint lines (e.g. download progress, "Verifying checksum...", scan's "Run: omm link" nudge); errors, warnings, and the result of what you asked for still print
- `--no-color` — disable ANSI colors on omm's own console output and its download progress bar; the `NO_COLOR` environment variable does the same

Passing `--json` or `--yes` to a command that doesn't use them prints a warning to stderr instead of silently doing nothing. Exit codes are consistent across every command: `0` success, `1` failure, `2` usage error (bad flag/argument).

`rm`, `ls`, and `up` are short aliases for `uninstall`, `list`, and `upgrade`.

Set `OMM_HOME` to store everything (models, config, catalog history) under a different directory instead of `~/.omm` — useful when `$HOME`'s filesystem doesn't have room for GGUF models, e.g. `OMM_HOME=/mnt/data/omm omm contribute --yes`.

`omm contribute` refuses to start unless every model volume has at least 10 GiB free. Before each download it also budgets the central GGUF, a worst-case full Ollama import copy, any required Windows cross-volume copies, and safety headroom. Each model evaluation prints a heartbeat every 30 seconds and is terminated after an absolute 10-minute deadline instead of hanging an unattended session indefinitely.

Localfit does not assume all installed memory belongs to the model. A live
scan subtracts memory currently used by other applications, keeps at least
2 GB (or 10% of RAM) for the OS and newly opened apps, and applies total-memory
caps. Recommendation fit and `omm tune` use this safe budget, so rerunning a
command adapts after memory-heavy applications are opened or closed.

`omm benchmark` runs a versioned eight-item bilingual arithmetic smoke pack
against models already installed in Ollama. It stores parsed answers,
correctness, pinned model metadata, and fixed-length timings under
`~/.omm/evaluations/`; it stores no generated text. Opt-in telemetry sends a locally
computed CPU chip score (and GPU chip score, when a GPU is present) plus
architecture and core counts — never the raw CPU/GPU model name — so speed
predictions can distinguish otherwise identical Linux `x86_64` machines.
Results are uploaded only after explicit opt-in. The pack is intentionally
small and is not a leaderboard.

On Windows, Ollama is detected by its HTTP API first, so a freshly installed
tray app works even before the current terminal receives the new `PATH`.
When the daemon is stopped, omm also checks Ollama's documented
`%LOCALAPPDATA%\Programs\Ollama` location. It only stops daemon processes it
started itself. Before deleting a contribution model, omm requests an Ollama
unload, waits for `/api/ps` to confirm handle release, and uses bounded retries
for Windows file locks. Real-time antivirus can still delay a first load; the
benchmark uses repeated samples and reports their median. Do not disable your
antivirus for omm.

## Self-hosted benchmark data

Benchmark results are never uploaded without explicit per-run consent or an
`always` policy. New installations include the hosted Firebase endpoint as the
default destination, while existing local-only configurations stay local. To
run the bundled FastAPI + SQLite collector instead:

```sh
pip install -e ".[server]"
export LOCALFIT_DB_PATH="$PWD/localfit.db"
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
localfit-server
```

Explicitly configure the endpoint and opt in before uploading:

```sh
omm setting telemetry --endpoint http://127.0.0.1:8000/v1/benchmarks
omm setting upload --enable
```

Training can consume the authenticated export directly:

```sh
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
python scripts/train_model.py \
  --telemetry-url http://127.0.0.1:8000/v1/benchmarks/export
```

Firebase Realtime Database JSON endpoints remain supported. An official
`*.firebaseio.com` or
`*.firebasedatabase.app` `.json` URL can be read without an admin token;
self-hosted raw export requires `LOCALFIT_ADMIN_TOKEN`. Exact duplicate events
are ignored.

Automated retraining is fail-closed. Configure
`LOCALFIT_TELEMETRY_EXPORT_URL`; configure `LOCALFIT_ADMIN_TOKEN` as well for a
self-hosted export (it is optional for an official Firebase JSON URL). The
scheduled job otherwise stops without changing the published artifact. It
requires at least 100 distinct valid v6/v7 configurations with explicit
runtime and CPU metadata (legacy rows do not satisfy this minimum), rejects datasets with more
than 25% invalid rows, and reserves a deterministic 20% holdout. A 64-tree v4
candidate replaces the incumbent only when both holdout RMSLE and P90 absolute
percentage error stay within the configured regression limits. Selection is
evaluated on whole hardware/request contexts, so sibling model variants never
leak across training and holdout sets. Publishing also requires at least three
multi-model selection groups plus complete top-1, regret, balanced-fit, and
false-positive evidence. Missing evidence fails the gate. The artifact records
the complete candidate/baseline evaluation report.

The same gate can validate an exported local dataset without contacting the
collector:

```sh
python scripts/train_model.py --offline \
  --telemetry-file benchmarks.jsonl \
  --quality-gate --minimum-real-configurations 100 \
  --baseline published/localfit-recommend-model.json \
  --output candidate.json --quality-report quality-report.json
```

Synthetic bootstrap training remains available for local development, but the
scheduled publishing workflow never uses it as a substitute for missing real
benchmark data.

## Signed recommendation data

`omm setting catalog-trust --manifest-url <https-url> --public-key <base64-key>`
enables Ed25519 verification for future recommendation downloads. Existing
artifacts are snapshotted before replacement and `omm setting catalog-rollback`
restores the most recent different snapshot.

## Development

```sh
pip install -e ".[dev]"
pytest
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
testing, and PR conventions, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for
community expectations. Report security issues per [SECURITY.md](SECURITY.md)
rather than as a public issue.

## License

MIT — see [LICENSE](LICENSE). Third-party dependency licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
