# omm — Open source Model Manager

`omm` is an apt/brew-style package manager for local LLMs (GGUF). It installs models into a central hub, links them into seven local AI runners automatically (Ollama, LM Studio, Jan, AnythingLLM, Msty, text-generation-webui, KoboldCpp), and can recommend a model that fits your hardware.

## Install

**macOS / Linux:**

```sh
curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh | sh
```

This bootstraps `python3`, `git`, and `pipx` if missing (Debian/Ubuntu via `apt`, or Homebrew on macOS), then installs `omm` as an isolated CLI via `pipx`. Open a new shell afterward so your `PATH` picks up `omm`.

**Windows (PowerShell):**

```powershell
# This must run before irm: script-internal TLS settings are too late for its first download.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1 | iex
```

This bootstraps Python and git via [winget](https://learn.microsoft.com/en-us/windows/package-manager/winget/) if missing (built into Windows 10 2004+ and Windows 11 — on older Windows, install [Python 3.10+](https://www.python.org/downloads/) and [git](https://git-scm.com/downloads) manually first), then installs `omm` through that exact validated Python interpreter. Open a new PowerShell window afterward so your `PATH` picks up `omm`. On Windows, model exposure tries an unprivileged same-volume hard link first, then a symbolic link (Developer Mode or Administrator), then an owned copy. Before copying, omm checks destination free space and reports that the model now consumes additional bytes. File junctions do not apply because model targets are files, not directories.

Requirements: Python 3.10+. The optional NVIDIA detector is installed only when `nvidia-smi` indicates an NVIDIA driver.

### Supported platforms

`omm` is tested in CI on Windows, macOS, and Linux with Python 3.10+. Windows 10 22H2/11 is the supported Windows baseline because that matches Ollama's native Windows requirements. Hardware scan, install, linking, benchmark, update, and contribution flows are cross-platform; Ollama remains the only benchmark engine.

Both installers clone to a versioned staging directory, verify the signed commit against a bootstrap trust anchor, and only then switch pipx to it. Do not replace this with an unverified `git clone` plus `pipx install` if commit authenticity matters.

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

To remove the CLI while preserving downloaded models and settings:

```powershell
irm https://raw.githubusercontent.com/omm-hippo/omm/main/uninstall.ps1 | iex
```

```sh
curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/uninstall.sh | sh
```

Run a downloaded script with `-Purge` (PowerShell) or `--purge` (sh) to remove the model hub and settings too. Purge removes only known omm-owned paths and leaves unrelated files in a custom `OMM_HOME` untouched. Installers mark custom homes so uninstallers can refuse ambiguous or unsafe locations; shell profiles are never rewritten during uninstall.

## Usage

```sh
omm setup  # First-run setup wizard: hardware scan + engine checklist (re-runnable any time)
omm scan [--json]  # Print a hardware, runner, and model summary (RAM, VRAM, OS)
omm recommend  # Suggest a model that fits this machine, then offer to install it
omm tune <name> [--json]  # Recommend context, GPU offload, threads, and batch size
omm benchmark <name>...  # Local quality + speed smoke evidence for one or more installed models
omm search <query> [--json] [--skip-unfit] [--limit N] [--provider curated|huggingface|modelscope]  # Search curated models, cached candidates, and HuggingFace
omm install <name> [--skip-unfit] [--upload/--no-upload] [--force]  # Download a model and link it into LM Studio / Ollama
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
omm contribute [--yes]  # Repeatedly install/benchmark/upload hardware-fit models to grow the dataset
omm update  # Git-pull the latest source into ~/.omm/src, then refresh rules/model data
omm setting  # Interactive menu for telemetry, upload policy, version channel, and catalog trust
omm setting version [--stable|--beta]  # Show or switch the update channel `omm update` pulls from
omm setting telemetry --endpoint <url>  # Configure where benchmark telemetry is sent
omm setting upload --enable|--disable|--ask  # Configure the benchmark-upload send policy
omm setting calibrate <name>  # Locally correct predicted speed with an installed Ollama model
omm setting catalog-trust --manifest-url <url> --public-key <key>  # Require signed recommendation downloads
omm setting catalog-status  # Show signed recommendation data and rollback snapshots
omm setting catalog-rollback  # Restore the most recent different recommendation snapshot
omm help [command]  # Show help, same as --help
```

`install`, `uninstall`, `info`, and `upgrade` accept either a model name/reference or the numeric index shown by the last `omm search` or `omm list` run in that terminal. `search`/`install` mark models predicted not to run on this machine's hardware in red.

### Scripting

All errors, warnings, and confirmation prompts print to stderr; `--json` output (supported on `search`/`list`/`info`/`benchmark`/`tune`/`scan`) is the only thing written to stdout, so it's safe to pipe (e.g. `omm list --json | jq .`). Any command that would otherwise prompt for confirmation fails fast with a non-zero exit code when there's no terminal attached instead of hanging — pass `--yes`/`-y` (works on every command that has a confirmation prompt) or the relevant flag (`install --skip-unfit`, `install --upload`/`--no-upload`) to run it unattended.

Four global flags work either before or after the subcommand name (`omm --json search foo` and `omm search foo --json` are equivalent): `--json` (structured output, where supported — see above), `--yes`/`-y` (skip confirmation prompts), `--quiet`/`-q` (suppresses progress bars and background status/hint lines — e.g. download progress, "Verifying checksum...", scan's "Run: omm link" nudge; errors, warnings, and the result of what you asked for still print), and `--no-color` (disable ANSI colors on omm's own console output and its download progress bar; the `NO_COLOR` environment variable does the same). Passing `--json` or `--yes` to a command that doesn't use them prints a warning to stderr instead of silently doing nothing. Exit codes are consistent across every command: `0` success, `1` failure, `2` usage error (bad flag/argument).

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
`~/.omm/evaluations/`; it stores no generated text. Opt-in v6 telemetry sends
CPU model, architecture, and core counts so speed predictions can distinguish
otherwise identical Linux `x86_64` machines.
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

Benchmark uploads are disabled and have no server endpoint by default. To run
the bundled FastAPI + SQLite collector locally:

```sh
pip install -e ".[server]"
export LOCALFIT_DB_PATH="$PWD/localfit.db"
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
localfit-server
```

Explicitly configure the endpoint and opt in before uploading:

```sh
omm setting telemetry --endpoint http://127.0.0.1:8000/v1/benchmarks --enable
```

Training can consume the authenticated export directly:

```sh
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
python scripts/train_model.py \
  --telemetry-url http://127.0.0.1:8000/v1/benchmarks/export
```

The old Firebase Realtime Database JSON endpoint remains supported only when
explicitly configured. Its official `*.firebaseio.com` or
`*.firebasedatabase.app` `.json` URL can be read without an admin token;
self-hosted raw export requires `LOCALFIT_ADMIN_TOKEN`. Exact duplicate events
are ignored.

Automated retraining is fail-closed. Configure
`LOCALFIT_TELEMETRY_EXPORT_URL`; configure `LOCALFIT_ADMIN_TOKEN` as well for a
self-hosted export (it is optional for an official Firebase JSON URL). The
scheduled job otherwise stops without changing the published artifact. It
requires at least 100 distinct valid v6 configurations with explicit runtime and CPU metadata
metadata (legacy rows do not satisfy this minimum), rejects datasets with more
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
  --baseline published/recommend-model.json \
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
