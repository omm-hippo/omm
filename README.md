# omm — Open source Model Manager

[![Cross-platform validation](https://github.com/omm-hippo/omm/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/omm-hippo/omm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/omm-model)](https://pypi.org/project/omm-model/)
[![npm](https://img.shields.io/npm/v/@omm-hippo/omm)](https://www.npmjs.com/package/@omm-hippo/omm)
[![Python](https://img.shields.io/pypi/pyversions/omm-model)](https://pypi.org/project/omm-model/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Contributor Covenant](https://img.shields.io/badge/Contributor%20Covenant-3.0-4baaaa.svg)](CODE_OF_CONDUCT.md)

`omm` is an apt/brew-style package manager for local GGUF models. It keeps one
model hub, exposes models to supported local AI runners, checks whether a model
fits the current hardware, and can verify real local generation before you rely
on it.

> [!NOTE]
> The `main` branch documents the current development version. For the latest
> published build, use the [GitHub Releases](https://github.com/omm-hippo/omm/releases)
> page or the version shown by your package manager.

**Project guides:** [Contributing](CONTRIBUTING.md) ·
[Code of Conduct](CODE_OF_CONDUCT.md) · [Security](SECURITY.md) ·
[Privacy](PRIVACY.md) · [MIT License](LICENSE)

## What omm does

- Installs GGUF models into a central, configurable model hub.
- Links or imports models across Ollama, LM Studio, Jan, AnythingLLM, Msty,
  text-generation-webui, and KoboldCpp without silently duplicating large files.
- Ranks models against live RAM, VRAM, operating-system, and runner state.
- Verifies load and generation through local Ollama or LM Studio APIs.
- Keeps benchmark uploads, anonymous usage stats, crash reports, and arena
  battle votes opt-in, with a separate setting for each. Searches, downloads,
  and update checks also use the network; see [PRIVACY.md](PRIVACY.md).

## Quick start

After [installing omm](#install), run these commands in order:

```sh
omm setup       # Scan hardware and choose a local AI runner
omm recommend   # Review compatible models and choose one to install
omm list        # Find the installed model's name
```

Use that name with `omm verify <name>` to check local load and generation,
then `omm run <name>` to use it. If setup or a runner is not working, start
with `omm doctor` and `omm log --lines 5`. A recommendation estimates fit;
verification checks the selected model on your machine.

## Table of contents

- [What omm does](#what-omm-does)
- [Quick start](#quick-start)
- [Install](#install)
- [Usage](#usage)
  - [Setup & discovery](#setup--discovery)
  - [Install & manage models](#install--manage-models)
  - [Verify & benchmark](#verify--benchmark)
  - [Update & configuration](#update--configuration)
  - [Documents for coding agents](#documents-for-coding-agents)
  - [Shared company documents (cluster head)](#shared-company-documents-cluster-head)
  - [Local logs and automatic import](#local-logs-and-automatic-import)
  - [Local document search (RAG)](#local-document-search-rag)
  - [Scripting](#scripting)
- [Self-hosted benchmark data](#self-hosted-benchmark-data)
- [Signed recommendation data](#signed-recommendation-data)
- [Development](#development)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Install

**macOS / Linux** — in Terminal:

```sh
curl -fsSL https://omm.run/install.sh | sh
```

**Windows** — in PowerShell (not Command Prompt, Git Bash, or WSL). Both lines
are one command; paste them together:

```powershell
# This must run before irm: script-internal TLS settings are too late for its first download.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://omm.run/install.ps1 | iex
```

**Other ways:**

```sh
brew install omm-hippo/omm/omm       # Homebrew Tap (macOS)
pipx install omm-model               # PyPI, any OS with Python 3.10+
npm install --global @omm-hippo/omm  # Standalone binary, Node.js 22.14+
```

The two installer scripts set up Python and git when they are missing and
verify the signed source commit before installing. The Homebrew, PyPI, and npm
paths rely on that registry's own security instead. Open a new terminal
afterward and run `omm` to start the setup wizard. Set `OMM_HOME` before
installing to keep models somewhere other than `~/.omm`.

Requirements, uninstalling, shell completion, which runners omm can install,
and a troubleshooting table are in the [install guide](docs/install.md).
Step-by-step walkthroughs: <https://omm.run/install/windows>,
<https://omm.run/install/macos>, <https://omm.run/install/linux>.

## Usage

### Setup & discovery

```sh
omm setup  # First-run setup wizard: hardware scan + engine checklist (re-runnable any time)
omm engine install [ENGINE]  # Install one supported local runner, or choose interactively
omm engine status [ENGINE] [--json]  # Separate application, package version, and local API state
omm engine doctor [ENGINE]  # Read-only diagnostics and next steps
omm engine security [ENGINE] [--fix-local-only] [--json] [--yes]  # Show whether Ollama/LM Studio (and this Mac's omm cluster) accept connections from other devices
omm engine update ENGINE [--dry-run] [--yes]  # Use the identified package manager
omm engine uninstall ENGINE [--dry-run] [--yes]  # Remove the engine package, keep OMM models
omm cluster init  # Make this Mac the head of a Thunderbolt-linked exo cluster (macOS 26.2+)
omm cluster join <namespace>  # Add this Mac to the cluster the head Mac created
omm cluster status [--json]  # Nodes, memory, RDMA, firewall, gateway, document search (head), and any problems (employee PCs: Macs connected, memory, loaded models)
omm cluster restart  # Restart exo on this Mac when status lists Macs that are on but missing
omm cluster recommend [--json]  # Show the biggest exo models that fit the cluster's free memory
omm cluster search TEXT [--json]  # Search Hugging Face for MLX models exo can run
omm cluster install MODEL  # Download a model to every Mac without loading it (head Mac)
omm cluster uninstall MODEL [--yes]  # Delete a model from every Mac, unloading it first (head Mac)
omm cluster list [--json]  # Models on the cluster and whether each is loaded (employee PCs: loaded ones)
omm cluster info MODEL [--json]  # A model's details, which Macs have it, and every way it can be placed now (also from employee PCs)
omm cluster load MODEL [--sharding tensor|pipeline] [--macs N | --on MAC]  # Load a model into memory for employees, optionally placed your way (--on: one named Mac); obeys omm setting memory-guard (head Mac)
omm cluster unload MODEL  # Free a loaded model's memory; its files stay (head Mac)
omm cluster run [MODEL]  # Chat in the terminal, loading an installed model first (any PC with omm rag remote set)
omm cluster benchmark MODEL [--json]  # First-token time and tok/s of one answer on the cluster (employee PCs: loaded models; never uploaded)
omm cluster token add <name>  # Create an employee access token for the cluster gateway (head Mac; shown once)
omm cluster token list  # List employee tokens by name (head Mac)
omm cluster token revoke <name>  # Revoke an employee token (head Mac)
omm cluster handover MAC [--yes]  # Move the cluster head to another Mac in the cluster (head Mac; employees re-run omm rag remote set)
omm cluster leave [--yes]  # Remove this Mac's cluster firewall, services, and settings
omm scan [--json]  # Memory, storage, installed runners, and models
omm doctor [--json]  # Read-only diagnostics plus safe next steps for installation and Ollama findings
omm bug-report [--include os|policies|checks|stages] [--save PATH]  # Preview and save local diagnostics and optional stage counts
omm recommend [--json]  # Rank compatible models, mark installed ones, and offer a new one to install
omm web [--port PORT] [--open] [--create-launchers DIRECTORY]  # Start the local interface or create personal click launchers
omm setting web-autostart enable  # Optionally start the local manager (no browser) at sign-in; off by default
omm setting web-autostart disable  # Remove optional sign-in startup
omm setting web-autostart status  # Inspect optional sign-in startup
omm tune <name> [--json]  # Recommend context, GPU offload, threads, and batch size
omm tune <name> --apply --save --engine ollama --yes  # Verify proposed settings locally, then save
omm search <query> [--json] [--skip-unfit] [--skip-ms] [--limit N] [--provider curated|huggingface|modelscope]  # Search curated, Hugging Face, and ModelScope sources
omm help [command]  # Show help, same as --help
```

`omm web` starts a loopback-only local manager for searching, installing,
chatting with, and comparing models. See [command notes](docs/command-notes.md#omm-recommend)
and the [local manager guide](docs/local-web.md).

The **공유 AI** screen connects to an administrator-provided private gateway:
enter its address and employee token, check the connection, select the model
confirmed by the administrator, and send a first question. The token stays in
the local OMM settings; no SSH login is needed for normal use.

### Install & manage models

```sh
omm install <name> [--quant NAME] [--skip-unfit] [--upload/--no-upload] [--force] [--load-check|--no-load-check]  # Download, link, and optionally verify a model (--quant Q4_K_M skips the quant picker; --force re-checks the source and only re-downloads when it actually changed)
omm fit <name>  # Memory card: does this model (installed or not) fit next to what is running right now?
omm run [name] [--engine NAME]  # Chat with an installed model: Ollama in the terminal, KoboldCpp/text-generation-webui with the model loaded, GUI apps opened
omm import [directory] [--yes]  # Adopt GGUF files found across supported runners (and an optional directory) into the hub
omm uninstall <name> [--dry-run]  # Uninstall a model and clean up its symlinks/manifests (alias: rm)
omm uninstall all [--yes] [--dry-run]  # Uninstall every model installed via omm
omm list [--json] [--engine NAME]  # Show models installed via omm and their linked status (alias: ls)
omm info <name> [<name>...] [--json]  # Inspect one installed or remote model, or compare several side by side
omm upgrade <name> [--dry-run]  # Look for a better model than this one - a curated successor, or a higher quantization from the same repo that still fits (alias: up)
omm upgrade [--yes] [--dry-run]  # Scan every installed model for a better alternative
omm link [models] [--engine NAME]  # Re-verify and repair installed-model links across supported runners; `models` is a comma-separated name/index list (omit for every model)
omm link [models] --to <directory>  # Reuse central GGUF files; Windows warns if a real copy is required
omm unlink <models> [--engine NAME]  # Remove models' links from one runner (--engine all for every runner), keeping the hub file
omm export <name> <destination> [--force]  # Copy a hub model out for deployment or backup (hard link when possible)
omm pin <name>  # Archive this model's current version before its next forced reinstall
omm unpin <name>  # Stop archiving this model and delete any version already archived for it
omm rollback <name>  # Restore a pinned model's archived version in place of the installed one
omm cleanup  # Remove orphaned partial downloads and broken runner symlinks
```

`install`, `uninstall`, `info`, and `upgrade` accept a model name or the number
shown by the last `omm search` or `omm list` in that terminal. Re-run an
interrupted install to resume it. See [command notes](docs/command-notes.md#omm-install-uninstall-and-upgrade).

### Verify & benchmark

```sh
omm verify <name> [--engine ollama|lmstudio] [--yes] [--keep-loaded]  # Prove local load + generation works
omm benchmark <name>... [--output PATH]  # Local quality + speed evidence for selected installed models
omm benchmark all [--output PATH]  # Benchmark every installed model in the selected runtime
omm evaluate <ollama-tag> [--pack PATH] [--output PATH]  # Run local Python coding tasks in Docker/Podman
omm arena [MODEL1 MODEL2] [--keep]  # Compare two installed models blind on your own prompt and vote; votes stay in ~/.omm/arena/votes.jsonl
omm arena --leaderboard  # Show how people with machines like yours rated models, numbered for omm install / omm arena
omm contribute [--yes]  # Repeatedly install/benchmark/upload hardware-fit models to grow the dataset
omm contribute --max-minutes 30 --max-download-gb 10 --max-models 3
```

`omm verify` sends one short prompt and stops or unloads only what it started.
`omm evaluate` runs model-generated code only inside Docker or Podman with no
network. See [command notes](docs/command-notes.md#omm-verify) for what each check stores.

### Update & configuration

```sh
omm update  # Update a canonical OMM Git-source install; package installs print their manager command
omm setting  # Interactive menu for outbound data, version, theme, calibration, and catalog trust
omm setting agent  # Pick connect/restore/status, the harness and the model interactively
omm setting agent local [claude|codex|pi] [--model <tag>] [--base-url <origin>]  # Back up harness settings and route to local Ollama (asks for what is missing)
omm setting agent cluster [claude|codex|pi] [--model <exo-model-id>]  # Back up harness settings and route to the company cluster gateway (asks for what is missing)
omm setting agent cloud [claude|codex|pi]  # Restore original routing and preserve unrelated settings edits
omm setting agent status [claude|codex|pi]  # Inspect saved routing state without loading a model
omm setting version [--stable|--beta]  # Show or switch the update channel `omm update` pulls from
omm setting telemetry --endpoint <url>  # Configure where benchmark telemetry is sent
omm setting upload  # Show all four outbound-data policies (benchmark / usage / crash / votes); see PRIVACY.md
omm setting upload benchmark --enable|--disable|--ask  # Benchmark-result upload policy
omm setting upload usage --enable|--disable  # Anonymous daily usage stats (off by default)
omm setting upload crash --enable|--disable|--ask  # Opt-in crash-report policy
omm setting upload votes --enable|--disable|--ask  # Arena battle vote uploads (asked after each session; the prompt you typed is never sent)
omm setting memory-guard --policy ask|block|observe  # Protect local runtime loads from live memory pressure
omm setting theme [--set NAME]  # Show or change omm's output color theme
omm setting runtime-profile <name> [--engine ollama|lmstudio] [--restore] [--json]  # Inspect or undo saved settings
omm setting catalog-trust --manifest-url <url> --public-key <key>  # Require signed recommendation downloads
omm setting catalog-status [--json]  # Show trust, rollback snapshots, and per-check evaluation evidence
omm setting catalog-rollback  # Restore the most recent different recommendation snapshot
```

See [local agent connections](docs/agent-connections.md) for backup, restoration,
configuration precedence and verification limits. For reviewed Python runtime
extensions, see the [runtime plugin contract](docs/runtime-plugins.md).

### Documents for coding agents

```sh
omm rag mcp  # Run the local document search MCP server on stdin/stdout (started by a coding agent)
omm rag connect <claude-code|codex|pi|cursor>  # Register OMM document search as an MCP server in a coding agent
omm rag disconnect <claude-code|codex|pi|cursor>  # Remove only the OMM MCP entry and keep other settings
```

Coding agents search documents registered in OMM through a local stdio MCP
server; documents and questions never leave this computer. See
[document search for coding agents](docs/rag-mcp.md).

### Shared company documents (cluster head)

```sh
omm rag remote set <head address> [--token TOKEN] [--yes]  # On an employee PC: check the head's certificate fingerprint, then use its shared collections (token typed hidden when omitted)
omm rag remote  # Show which head `omm rag` uses (the token is never printed)
omm rag remote clear  # Forget the head and its token; `omm rag` uses this PC again
omm rag share <collection> <person> | --team <team>  # Let an employee (token name) or a team search a collection (owner or head administrator)
omm rag unshare <collection> <person> | --team <team>  # Withdraw that share
omm rag team create <team>  # On the head: create a team for sharing
omm rag team add <team> <person>  # On the head: add an employee (token name) to a team
omm rag team remove <team> <person>  # On the head: remove an employee from a team
omm rag team list  # On the head: list teams and members
omm rag team delete <team>  # On the head: delete a team and every share made to it
omm rag connect <harness> --remote  # Register the head's MCP endpoint (HTTPS, employee token in the harness config)
```

With a head set, `omm rag collection create|list|delete`, `add`, `remove`, `search`,
`share` and `unshare` work on the head through the token-checked cluster gateway
(HTTPS, port 52440). The first `omm rag remote set` shows the head certificate's
fingerprint; accept it only if it matches `omm cluster status` on the head. A collection is searchable only by its creator and the head administrator
until it is shared. See [document search for coding agents](docs/rag-mcp.md#사내-공유).

### Local logs and automatic import

```sh
omm log --lines 5                 # Read the last five command summaries
omm log --grep install            # Filter the local history by text
omm setting auto-import status   # Inspect the setting and OS service registration
omm setting auto-import enable   # Register background import of models from local runners
omm setting auto-import disable  # Stop and unregister background import
```

Logs live under `OMM_HOME/logs/` and are not uploaded by the data-sharing
channels. Review and redact a log before attaching it to an issue. Automatic
import is off by default and needs the optional `watch` dependencies; see
[command notes](docs/command-notes.md#automatic-import).

### Local document search (RAG)

```sh
omm rag status [--json]  # Collections, embedding model, whether semantic search is ready (with the fix), and documents that need reindexing
omm rag collection create <name>  # Create a named collection, e.g. 인사규정
omm rag collection list [--json]  # List collections with document counts, owner and shares
omm rag collection delete <name> [--yes]  # Delete a collection's stored text and index (original files stay)
omm rag add <collection> <paths...> [--replace] [--json]  # Add files or folders (recursive) to a collection, showing extraction and embedding progress
omm rag remove <collection> <name> [--yes]  # Remove one document's stored text and index
omm rag search <question> [--collection NAME] [--limit N] [--json]  # Hybrid (meaning + words) search with source passages
omm rag reindex [--collection NAME] [--all] [--json]  # Rebuild semantic vectors after an embedding-model change
omm rag embedding-model  # Show the embedding model and whether local Ollama has it
omm rag embedding-model set <model> [--base-url URL] [--keep-alive 5m]  # Change the local Ollama embedding model (default bge-m3) and how long it stays in memory (0 unloads at once)
```

Semantic search uses a local Ollama embedding model (default `bge-m3`, MIT
license; get it with `ollama pull bge-m3`). Results say which method answered:
`hybrid` (meaning + words, fused by reciprocal rank) or `lexical` (words only,
with the reason, for example Ollama is off or documents need `omm rag reindex`).
Only loopback Ollama is contacted; see [PRIVACY.md](PRIVACY.md).

### Scripting

All errors, warnings, and confirmation prompts print to stderr. For `search`,
`list`, `info`, `tune`, `scan`, `doctor`, `recommend`, `evaluate`, and
`bug-report`, `--json` makes
stdout a single structured document that is safe to pipe (for example,
`omm list --json | jq .`). `benchmark --json` also writes a single JSON report to stdout; `--output` saves
the same evidence as a file. Supported commands emit a structured error document
when their command body fails before producing a result, and use exit status 130
with `status: "cancelled"` when interrupted. Argument-parser errors use a JSON
error document when `--json` was requested, and stderr otherwise, with exit
status 2. Successful data shapes remain unchanged.

For commands that document `--yes`/`-y`, pass it to skip their confirmation
prompts, or use the command-specific flag (`install --skip-unfit`, `install
--upload`/`--no-upload`). Benchmark upload consent remains governed by
`omm setting upload`; for non-interactive benchmarks, start the selected local
runtime before invoking the command.

These common flags are available on most top-level commands. Where supported,
they may be accepted before or after the subcommand name (`omm --json search
foo` and `omm search foo --json` are equivalent). Use `omm COMMAND --help` for
the exact flags and placement of a specific command:

- `--json` — structured output, where supported (see above)
- `--yes` / `-y` — skip confirmation prompts
- `--quiet` / `-q` — suppress progress bars and background status/hint lines (e.g. download progress, "Verifying checksum...", scan's "Run: omm link" nudge); errors, warnings, and the result of what you asked for still print
- `--no-color` — disable ANSI colors on omm's own console output and its download progress bar; the `NO_COLOR` environment variable does the same

Unsupported `--json` combinations return a single `unsupported_json` error
document and exit 2 before command actions or startup prompts run. JSON mode
never opens the first-run setup/import dialogs, including on a terminal.
`omm --json --version` returns a version document; explicit `--help` still
shows normal help. The shared flag wrapper warns when `--yes` has no effect.
Exit codes are consistent across commands: `0` success, `1` failure, and `2`
usage error (bad flag or argument).

`rm`, `ls`, and `up` are short aliases for `uninstall`, `list`, and `upgrade`.

Set `OMM_HOME` to store everything (models, config, catalog history) under a different directory instead of `~/.omm` — useful when `$HOME`'s filesystem doesn't have room for GGUF models, e.g. `OMM_HOME=/mnt/data/omm omm contribute --yes`.

Disk-space limits for `omm contribute`, what `omm benchmark` stores, and how
the memory budget is computed are in [command notes](docs/command-notes.md#omm-benchmark).

## Self-hosted benchmark data

Benchmark results are never uploaded without explicit per-run consent or an
`always` policy. To send them to your own FastAPI + SQLite collector instead of
the hosted gateway, and for how the nightly retraining gate uses that data, see
[self-hosted benchmark data](docs/self-hosted-telemetry.md).

## Signed recommendation data

`omm setting catalog-trust --manifest-url <https-url> --public-key <base64-key>`
enables Ed25519 verification for future recommendation downloads. Existing
artifacts are snapshotted before replacement and `omm setting catalog-rollback`
restores the most recent different snapshot.

## Development

```sh
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]" -r requirements-train.txt
```

Run tests with the disposable home and model-hub setup in
[CONTRIBUTING.md](CONTRIBUTING.md#development-setup). That guide also covers
platform-specific setup, scoped checks, signed-head requirements, and
pull-request conventions.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
testing, and PR conventions, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for
community expectations. Report security issues per [SECURITY.md](SECURITY.md)
rather than as a public issue.

## Security

OMM-managed runner install/update uses [mandatory source, integrity and native
security checks](docs/mandatory-install-safety.md). Failed or unavailable checks
do not count as successful installation. These named checks are not a guarantee
that software has no vulnerabilities.

Do not open a public issue for a suspected vulnerability. Follow the private
reporting instructions and supported-version policy in
[SECURITY.md](SECURITY.md).

## License

The omm source code is available under the [MIT License](LICENSE). Third-party
dependency notices are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Models and local runner
applications installed or linked by omm keep their own upstream licenses and
terms; the project MIT license does not replace them.
