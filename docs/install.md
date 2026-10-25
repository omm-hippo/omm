# Installing omm

The [README](../README.md#install) has the one-line install command for each
platform. This page has the details: requirements, what each installer does,
where models are stored, shell completion, uninstalling, and troubleshooting.

Pick your OS and follow one path from top to bottom:

- [Windows](#windows) — PowerShell one-liner (verified Git-source installer)
- [macOS](#macos) — Terminal one-liner, or the Homebrew Tap
- [Linux](#linux) — shell one-liner
- [Any OS via PyPI or pipx](#any-os-via-pypi-or-pipx) — `pip` / `pipx`, no signature verification
- [npm](#npm) — standalone native command distributed through npm
- [Troubleshooting](#troubleshooting) — what you see, why, and how to fix it

## Windows

**1. Open this app.** Open **PowerShell** — either Windows PowerShell 5.1 (Start menu → "Windows PowerShell") or PowerShell 7 (`pwsh`). Windows Terminal is fine as long as the active tab is a PowerShell tab. Do **not** use Command Prompt (`cmd.exe`), and do not paste the one-liner into Git Bash or WSL; those are Unix shells and need the [Linux](#linux) path instead.

**2. Requirements.** Python 3.10+. Windows 10 22H2/11 is the supported baseline. The installer bootstraps Python and git via [winget](https://learn.microsoft.com/en-us/windows/package-manager/winget/) if they are missing (winget is built into Windows 10 2004+ and Windows 11 — on older Windows, install [Python 3.10+](https://www.python.org/downloads/) and [git](https://git-scm.com/downloads) manually first), then installs `omm` through that exact validated Python interpreter. The optional NVIDIA detector is installed only when `nvidia-smi` indicates an NVIDIA driver.

**3. Run the installer.** Both lines are one command; paste them together.

```powershell
# This must run before irm: script-internal TLS settings are too late for its first download.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://omm.run/install.ps1 | iex
```

**4. After install.** Open a new PowerShell window so your `PATH` picks up `omm`, then run:

```powershell
omm        # first run: setup wizard (hardware scan + engine checklist)
omm scan   # memory, runner, and model summary
```

**5. Windows notes.** Model exposure tries an unprivileged same-volume hard link first, then a symbolic link (Developer Mode or Administrator), then an owned copy. Before copying, omm checks destination free space and reports that the model now consumes additional bytes. File junctions do not apply because model targets are files, not directories.

Set `OMM_HOME` before installation and on later runs to put the model hub on another volume. Runners omm installs itself (KoboldCpp, text-generation-webui, AnythingLLM, Msty) go to `OMM_HOME\apps` and installer downloads to `OMM_HOME\tmp`, so nothing large lands on the system drive. (AnythingLLM still keeps its bundled Ollama and starter model, ~5 GB, under `%APPDATA%` - its installer offers no way to move that.)

```powershell
[Environment]::SetEnvironmentVariable("OMM_HOME", "D:\omm", "User")
$env:OMM_HOME = "D:\omm"
```

Install native shell completion once, then restart the shell:

```powershell
omm --install-completion powershell
```

Remove a Git-source installation while preserving downloaded models and settings:

```powershell
irm https://omm.run/uninstall.ps1 | iex
```

Download that script and run it with `-Purge` to remove the model hub and settings too.

Runner note: AnythingLLM and Msty currently require manual installation on Windows; the checklist prints guidance instead of guessing an installer or package ID.

Detailed walkthrough: <https://omm.run/install/windows>

## macOS

**1. Open this app.** Open **Terminal** (Applications → Utilities → Terminal), or any terminal emulator you already use. The command runs under `sh`/`zsh`/`bash`.

**2. Requirements.** Python 3.10+ and git. The installer uses Homebrew when either is missing: if Homebrew itself is not installed, it bootstraps Homebrew with Homebrew's official installer first (Homebrew requires a supported macOS and Apple's Xcode Command Line Tools). To require a pre-existing Homebrew installation instead of letting the installer bootstrap it, export `OMM_AUTO_INSTALL_HOMEBREW=0` before running the command. `pipx` is bootstrapped with `pip` if missing (Homebrew and other PEP-668 "externally-managed" Pythons are handled with `--break-system-packages`), and `omm` is then installed as an isolated CLI via `pipx`. The installer also persists pipx's executable directory in `~/.zprofile`, so a newly opened zsh finds `omm` automatically. The optional NVIDIA detector is installed only when `nvidia-smi` indicates an NVIDIA driver.

**3. Run the installer.**

```sh
curl -fsSL https://omm.run/install.sh | sh
```

**4. After install.** Open a new shell afterward so your `PATH` picks up `omm`, then run:

```sh
omm        # first run: setup wizard (hardware scan + engine checklist)
omm scan   # memory, runner, and model summary
```

**5. Homebrew Tap (alternative).**

```sh
brew install omm-hippo/omm/omm
```

Upgrade or remove the formula with Homebrew. Removing the formula preserves
downloaded models and settings under `OMM_HOME`:

```sh
brew upgrade omm-hippo/omm/omm
brew uninstall omm-hippo/omm/omm
```

After a tagged PyPI release passes its public install checks, GitHub notifies
the Homebrew Tap. Homebrew intentionally applies its upstream release cooldown
before opening an automated Formula update PR, so the Tap can temporarily lag
behind PyPI. Use `brew info omm-hippo/omm/omm` to see the version currently
provided by the Tap. `omm update` does not modify a Homebrew installation and
instead prints the matching `brew upgrade` command.

**6. macOS notes.** Set `OMM_HOME` before installation and on later runs to put the model hub elsewhere:

```sh
export OMM_HOME=/mnt/models/omm
```

Install native shell completion once, then restart the shell:

```sh
omm --install-completion bash  # or zsh/fish
```

Remove a Git-source installation while preserving downloaded models and settings:

```sh
curl -fsSL https://omm.run/uninstall.sh | sh
```

Download that script and run it with `--purge` to remove the model hub and settings too.

Detailed walkthrough: <https://omm.run/install/macos>

## Linux

**1. Open this app.** Any terminal emulator. The command runs under `sh`/`bash`.

**2. Requirements.** Python 3.10+. The installer bootstraps `python3`, `python3-venv`/equivalent, and `git` via whichever supported package manager is present — `apt-get`, `dnf`, `yum`, `pacman`, or `apk` — when the current user can install system packages; on an unsupported distribution, install Python 3.10+ and git yourself first or the installer exits with `Python 3.10+ not found` or `git not found. Install git first`. `pipx` is bootstrapped with `pip` if missing (PEP-668 "externally-managed" Pythons are handled with `--break-system-packages`), and `omm` is then installed as an isolated CLI via `pipx`. The optional NVIDIA detector is installed only when `nvidia-smi` indicates an NVIDIA driver.

**3. Run the installer.**

```sh
curl -fsSL https://omm.run/install.sh | sh
```

**4. After install.** Open a new shell afterward so your `PATH` picks up `omm`, then run:

```sh
omm        # first run: setup wizard (hardware scan + engine checklist)
omm scan   # memory, runner, and model summary
```

**5. Linux notes.** Set `OMM_HOME` before installation and on later runs to put the model hub on another volume:

```sh
export OMM_HOME=/mnt/models/omm
```

Install native shell completion once, then restart the shell:

```sh
omm --install-completion bash  # or zsh/fish
```

Remove a Git-source installation while preserving downloaded models and settings:

```sh
curl -fsSL https://omm.run/uninstall.sh | sh
```

Download that script and run it with `--purge` to remove the model hub and settings too.

Runner note: omm installs Jan on Linux through Flatpak, so `flatpak` must be present for that entry in the runner checklist.

Detailed walkthrough: <https://omm.run/install/linux>

## Any OS via PyPI or pipx

Works on macOS, Linux, and Windows:

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

Both commands preserve downloaded models and settings under `OMM_HOME`.

## npm

The public npm launcher installs a platform-specific standalone `omm` binary,
so Python is not required for this path. It requires Node.js 22.14 or newer:

```sh
npm install --global @omm-hippo/omm
omm --version
```

Published npm binaries cover macOS on Apple Silicon and Intel, glibc-based
Linux on ARM64 and x86_64, and Windows x64. Other operating-system,
architecture, and libc combinations should use the Python-based installer or
PyPI path instead.

Upgrade or remove the npm installation with:

```sh
npm update --global @omm-hippo/omm
npm uninstall --global @omm-hippo/omm
```

The npm launcher verifies the selected native package name, version, platform
metadata, and binary containment before executing it. Release CI separately
checks package integrity and registry signatures. Removal preserves models and
settings under `OMM_HOME`.

## Troubleshooting

Match the message you see, not the step you think you are on.

| What you see | Why | Fix |
|---|---|---|
| PowerShell says `sh` is not recognized, or `curl` rejects `-fsSL` | The macOS/Linux `curl … \| sh` line was pasted into PowerShell, where `curl` is an alias for `Invoke-WebRequest` and there is no `sh` | Use the [Windows](#windows) command instead |
| `irm` or `iex` is not recognized | You are in Command Prompt (`cmd.exe`), not PowerShell | Open PowerShell (or switch the Windows Terminal tab to PowerShell) and rerun |
| `Windows detected. Run the native PowerShell installer instead:` | The `install.sh` one-liner was run under Git Bash/MSYS/Cygwin, which the script refuses | Run the [Windows](#windows) PowerShell command |
| The download fails, times out, or reports a TLS/SSL error on Windows | The default security protocol negotiated an older TLS version | Run the `[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;` pre-line in the same command, before `irm`. Behind a corporate proxy, configure the proxy for PowerShell first |
| `Python not found. Install Python 3.10+ first` (Windows) or `Python 3.10+ not found` (macOS/Linux) | No runnable Python 3.10+ was found, and the winget/Homebrew/package-manager bootstrap could not supply one | Install [Python 3.10+](https://www.python.org/downloads/), open a new shell, and rerun |
| `git not found. Install git first (needed to fetch omm from GitHub)` | git is missing and could not be bootstrapped | Install [git](https://git-scm.com/downloads), open a new shell, and rerun |
| `git 2.34+ is required to verify SSH commit signatures` | Older git cannot check SSH commit signatures, and the installer fails closed rather than trusting an unverified checkout | Upgrade git to 2.34 or newer and rerun |
| `merge commit ... can only be verified with git 2.38+` | `omm update` needs `git merge-tree --write-tree` (git 2.38+) to authenticate an unsigned merge commit's result tree, and fails closed instead of trusting it unverified | Upgrade git to 2.38+ and rerun `omm update` |
| `Signature verification failed - refusing to install untrusted code.` | The fetched commit is not signed by the expected trust anchor | Do not bypass it. Stop and report the failure at <https://github.com/omm-hippo/omm/issues> |
| `Refusing to replace unrelated pipx environment 'omm'.` | A different tool already owns a pipx environment named `omm` | Remove or rename that environment manually, then rerun |
| `Refusing to replace an unverified omm-model pipx environment.` | An existing `omm-model` pipx environment could not be confirmed as OMM's | Inspect it with `pipx list`, remove it if it is safe to remove, then rerun |
| `Could not inspect existing pipx environments; refusing an unsafe migration.` | pipx metadata could not be read | Repair the pipx installation (`python -m pip install --user --upgrade pipx`) and rerun |
| `Refusing to remove unsafe pipx shared directory` | pipx reported a shared-environment path outside its own `PIPX_HOME/shared` directory, or that path is a symlink/reparse point | Do not delete the reported path. Repair or reinstall pipx, confirm `pipx environment --value PIPX_HOME` and `PIPX_SHARED_LIBS`, then rerun the installer |
| `git clone failed.` | The staging clone could not be fetched | Check network/proxy access to `github.com` and rerun |
| `omm` is not found after a successful install | The new `PATH` entry is not in the shell that ran the installer | Open a new terminal window and try again |
| `Could not remove <model>; the registry entry was kept` | Another program, antivirus scanner, or indexing service still has the model file open | Close local AI runners that use the model and retry `omm uninstall <model>`; OMM keeps the registry entry so the retry remains safe |
| winget is unavailable (older Windows) | winget ships with Windows 10 2004+ and Windows 11 only | Install [Python 3.10+](https://www.python.org/downloads/) and [git](https://git-scm.com/downloads) manually first, then rerun the installer |
| Homebrew bootstrap fails or is refused on macOS | `curl`/`/bin/bash` are unavailable, or `OMM_AUTO_INSTALL_HOMEBREW=0` was set without Homebrew already installed | Install [Homebrew](https://brew.sh) yourself, or unset `OMM_AUTO_INSTALL_HOMEBREW`, then rerun |
| pipx fails with `ensurepip is not available`, or `python3`/`git` are missing on an unsupported Linux distribution | The Linux bootstrap only automates `apt-get`, `dnf`, `yum`, `pacman`, and `apk` | Install `python3` (3.10+), the venv package for it, and `git` with your distribution's package manager, then rerun |
| Jan cannot be installed from the runner checklist on Linux | omm installs Jan on Linux via Flatpak | Install `flatpak` (and the Flathub remote), then rerun `omm setup` |
| `Refusing unsafe OMM_HOME`, `Refusing non-absolute OMM_HOME`, or `Refusing OMM_HOME that contains the current directory` | `OMM_HOME` points at `/`, your home directory, a relative path, or the directory you are running from | Set `OMM_HOME` to a dedicated absolute path and rerun from outside it |
| `Refusing unrecognized custom OMM_HOME (missing .omm-managed)` during uninstall | The uninstaller only removes homes that an omm installer marked as its own | Remove the directory yourself if it really is your model hub |

## Windows portable and Winget status

The Windows x64 portable ZIP and checksum are published as CI-verified GitHub
Release assets. The community Winget submission is not yet merged, so there is
no supported `winget install` command to document at this time.

## Supported platforms

`omm` is tested in CI on Windows, macOS, and Linux. Python-based installation
paths require Python 3.10 or newer; the npm path requires Node.js 22.14 or
newer and one of the published native targets listed above. Windows 10
22H2/11 is the supported Windows baseline because that matches Ollama's native
Windows requirements. Hardware scan, install, linking, benchmark, update, and
contribution flows are cross-platform. Benchmarking selects an available
local Ollama or LM Studio runtime; runner- and model-specific capabilities can
still differ.

## Updates and verification

Both installers clone to a versioned staging directory, verify the signed commit against a bootstrap trust anchor, and only then switch pipx to it. Do not replace this with an unverified `git clone` plus `pipx install` if commit authenticity matters.

`omm update` updates only a canonical OMM Git-source installation. For a
package-managed installation, it leaves files unchanged and prints the
matching package-manager upgrade command. The Git-only beta channel is
likewise unavailable to package-managed installations. Git-source updates
parse the new release's declared dependencies, honor `python_version` markers,
and refresh the pipx environment when an applicable dependency is missing or
older than the declared minimum.

## Package-channel verification

| Installation path | Highest verified level | Remaining limitation |
|---|---|---|
| PyPI / pipx | Simulator-verified on GitHub-hosted Windows, macOS, and Ubuntu runners using the public package | **Physical-device-verified: Not verified**; a real upgrade from the first public release is also unverified |
| npm | Simulator-verified by building, installing, running, checking update guidance, and uninstalling all five published native targets | **Physical-device-verified: Not verified** |
| Homebrew Tap | Physical-device-verified on an Apple Silicon Mac for public Tap install, `omm --version`, `brew test`, upgrade guidance, and uninstall | Intel Mac installation is not yet physical-device-verified |

Additional package-manager commands are added here only after their public
registry path has been installed and verified.

## Local AI runners

The first bare `omm` run on a fresh install (or `omm setup` any time after) shows a hardware summary and a checklist of local AI runners. Checking one that omm knows how to install runs its official installer with live progress in the terminal; checking one it doesn't yet automate on your platform prints a link instead. Automation coverage today:

| Runner | Automated on | Manual elsewhere |
|---|---|---|
| Ollama | macOS (Homebrew), Windows (WinGet) | Linux |
| LM Studio | macOS (Homebrew), Windows (WinGet) | Linux |
| Jan | macOS (Homebrew), Windows (winget), Linux (Flatpak) | wherever that package manager isn't installed |
| AnythingLLM | macOS (Homebrew) | Linux, Windows |
| Msty | macOS (Homebrew) | Linux, Windows |
| KoboldCpp | — | All platforms; automatic artifacts lack a pinned checksum |
| text-generation-webui | — | All platforms; install manually from the official releases |

Every currently-installed runner is also listed (marked as already installed, not selectable) rather than hidden, so the checklist always reflects what omm actually detects on the machine.

See [engine management](engine-management.md) for operation previews,
package ownership checks, and verification limits.

## Storage location

The model hub and omm state default to `~/.omm`. Set `OMM_HOME` before installation and on later runs to put them on another volume (see the snippet in your OS section above). The setup table shows the path separately from decimal GB that is immediately writable on the volume containing it; this is not the size of the `.omm` folder, and an OS storage screen may additionally count purgeable space.

Ollama's own model location follows `OLLAMA_MODELS`. LM Studio follows its home pointer; set `OMM_LMSTUDIO_MODELS_DIR` when LM Studio uses a custom directory that omm cannot discover automatically.

## Completion and uninstall

Each OS section above carries the completion and uninstall commands for that shell. For a PyPI installation, use `python -m pip uninstall omm-model`; for pipx,
use `pipx uninstall omm-model`. Both commands preserve downloaded models and
settings under `OMM_HOME`.

Purge (`-Purge` on PowerShell, `--purge` on sh) removes only known omm-owned paths and leaves unrelated files in a custom `OMM_HOME` untouched. Installers mark custom homes so uninstallers can refuse ambiguous or unsafe locations; shell profiles are never rewritten during uninstall.
