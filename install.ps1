# Installs omm (Open source Model Manager) as an isolated CLI command via pipx.
# Usage: [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1 | iex
# To install a non-default branch: $env:OMM_INSTALL_BRANCH = "beta"; irm ... | iex
# Do not try to fix the first-download TLS problem inside this script: `irm`
# fetches the script before PowerShell can execute any of its contents.
$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/omm-hippo/omm.git"
$OmmHome = if ($env:OMM_HOME) { $env:OMM_HOME } else { Join-Path $env:USERPROFILE ".omm" }
$OmmHome = [IO.Path]::GetFullPath($OmmHome).TrimEnd('\')
$profileHome = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
if (-not $OmmHome -or $OmmHome -eq [IO.Path]::GetPathRoot($OmmHome).TrimEnd('\') -or $OmmHome -eq $profileHome) {
    throw "Refusing unsafe OMM_HOME: $OmmHome"
}
$currentDirectory = [IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\')
$homePrefix = $OmmHome + [IO.Path]::DirectorySeparatorChar
if ($currentDirectory -eq $OmmHome -or $currentDirectory.StartsWith($homePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing OMM_HOME that contains the current directory: $OmmHome"
}
$SourcesDir = Join-Path $OmmHome "sources"

# Set $env:OMM_INSTALL_BRANCH before piping this script into iex to install
# from a branch other than the repo default (e.g. to try a beta build).
$Branch = $env:OMM_INSTALL_BRANCH

# Trust anchor for the signature check below - must stay identical to
# src/omm/trust/allowed_signers in the repo (that copy is what `omm
# update` verifies future commits against once installed; this one is
# the TOFU root for a brand new machine, since there's no prior install
# to carry a trusted copy yet).
$AllowedSignersContent = "seong381400@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPh12ERbI3Yx6DPiaROPjCyI2GIQXb9Ihbp9J9L4bnpe
ahseongchoi@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO5UPWuM/1GxGo5TQ5nEJm9UvXShygIozjbvxB1VT9u6
fakeminjun7321@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL4gaNZPEizBHr81LObieqSxd6HExCPK7UKupsTniJ8s
github-actions[bot]@users.noreply.github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICn3omW2ymuC5oHshx3WC7AcPP/wP0sLn2E/x4njWMP+"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# winget-installed tools update the registry's PATH value, but this
# already-running process doesn't pick that up on its own - reload from
# both scopes so the rest of this script can find what it just installed
# without needing a new shell (the equivalent of install.sh's ~/.bashrc
# patch-up, for the same "current shell can't see the new PATH yet" reason).
function Update-SessionPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

# Tries to install a missing dependency via winget (built into Windows 10
# 2004+ / Windows 11). Returns $false without throwing if winget itself
# isn't available, so the caller can fall back to a manual-install message.
function Install-ViaWinget {
    param([string]$Id, [string]$FriendlyName)
    if (-not (Test-CommandExists "winget")) {
        return $false
    }
    Write-Host "$FriendlyName not found, installing via winget..."
    winget install --id $Id -e --source winget --accept-package-agreements --accept-source-agreements --silent
    $ok = $LASTEXITCODE -eq 0
    Update-SessionPath
    return $ok
}

# Resolves $Commit in the git repo at $RepoDir to the commit whose signature
# actually matters. The repo only accepts changes to main via a GitHub-merged
# PR ("create a merge commit" strategy) - GitHub builds that merge commit
# itself and signs it with GitHub's own key, while the contributor's
# signature lives on the merge commit's second parent (the PR branch tip).
# For a normal two-parent merge commit, resolve to that second parent;
# anything else (a direct single-parent commit, or an octopus merge) is
# returned as-is.
function Resolve-SigningCommit {
    param([string]$Commit, [string]$RepoDir)

    $parents = (git -C $RepoDir rev-list --parents -n 1 $Commit).Trim() -split '\s+'
    if ($parents.Count -eq 3) {
        return $parents[2]
    }
    return $Commit
}

# Verifies $Commit (a commit-ish, usually HEAD) in the git repo at $RepoDir
# is SSH-signed by a key from $AllowedSignersContent. Fails closed: git too
# old to check SSH signatures, or verification itself erroring out, is
# treated the same as an actual bad signature - "can't verify" must never
# silently mean "trust it anyway".
function Test-CommitSignature {
    param([string]$Commit, [string]$RepoDir, [switch]$Quiet)

    # Write-Warning, not Write-Error: with $ErrorActionPreference = "Stop"
    # a Write-Error here would terminate the whole script immediately,
    # skipping the caller's cleanup (removing the unverified clone) that's
    # supposed to run before exiting.
    $gitVersionOutput = (git --version)
    if ($gitVersionOutput -notmatch '(\d+)\.(\d+)\.(\d+)') {
        Write-Warning "Could not parse git version from: $gitVersionOutput"
        return $false
    }
    $gitMajor = [int]$Matches[1]
    $gitMinor = [int]$Matches[2]
    if ($gitMajor -lt 2 -or ($gitMajor -eq 2 -and $gitMinor -lt 34)) {
        Write-Warning "git 2.34+ is required to verify SSH commit signatures (found $gitVersionOutput)."
        return $false
    }

    $signersFile = New-TemporaryFile
    Set-Content -Path $signersFile.FullName -Value $AllowedSignersContent -NoNewline

    # git/ssh-keygen write their "Good signature" status line to stderr even
    # on success. PowerShell 7.3+ defaults $PSNativeCommandUseErrorActionPreference
    # to $true, which - combined with the script-wide $ErrorActionPreference =
    # "Stop" - promotes that single stderr line into a terminating
    # NativeCommandError before $LASTEXITCODE is ever checked. Suppress both
    # preferences for just this native call so a successful verification
    # isn't mistaken for a crash.
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativePref = $PSNativeCommandUseErrorActionPreference
    $ok = $false
    try {
        $ErrorActionPreference = "Continue"
        $PSNativeCommandUseErrorActionPreference = $false
        $verifyOutput = git -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$($signersFile.FullName)" `
            -C $RepoDir verify-commit $Commit 2>&1
        $ok = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $previousNativePref
        Remove-Item $signersFile.FullName -Force -ErrorAction SilentlyContinue
    }
    if (-not $ok -and -not $Quiet) {
        Write-Warning ($verifyOutput | Out-String)
    }
    return $ok
}

# --- python --------------------------------------------------------------
#
# Prefer the `python` command (what python.org's installer and winget's
# Python package both put on PATH); fall back to the `py` launcher, which
# some existing installs expose without a bare `python` on PATH.

function Test-PythonCommand {
    param($Python)
    $process = $null
    try {
        # Do not invoke an app-execution alias in this PowerShell process:
        # WindowsApps can display a Store prompt and never return. A direct
        # child process plus a short timeout keeps the installer responsive.
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $Python.Executable
        $startInfo.Arguments = (@($Python.Arguments) + @(
            "-c",
            '"import sys; print(1 if sys.version_info >= (3, 10) else 0)"'
        )) -join " "
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.CreateNoWindow = $true
        $process = [System.Diagnostics.Process]::Start($startInfo)
        if ($null -eq $process) { return $false }
        if (-not $process.WaitForExit(5000)) {
            try { $process.Kill() } catch {}
            return $false
        }
        $result = $process.StandardOutput.ReadToEnd()
        return ($process.ExitCode -eq 0 -and $result.Trim() -eq "1")
    } catch {
        # WindowsApps aliases can exist on PATH but open a Store prompt (or
        # otherwise fail) instead of starting Python. Treat them as absent.
        return $false
    } finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}

function Get-PythonCommand {
    # Resolve concrete python/python3 executables ahead of the bare-name
    # probe below. A WindowsApps python.exe/python3.exe alias stub can sit
    # earlier on PATH than a just-installed real Python (winget's install
    # appends to PATH, it doesn't reorder it), so a bare-name probe run
    # right after a successful winget install can still resolve to the
    # Store alias and report "not found" - even though Python is now
    # actually there. Get-Command -All sees every match on PATH; filtering
    # out WindowsApps and probing each remaining Source directly sidesteps
    # the PATH-order problem instead of just detecting the stub (which
    # Test-PythonCommand's timeout/kill already does for the bare-name path).
    $resolved = @()
    foreach ($name in @("python", "python3")) {
        $resolved += (Get-Command $name -All -ErrorAction SilentlyContinue) |
            Where-Object { $_.Source -and ($_.Source -notmatch '\\WindowsApps\\') } |
            ForEach-Object { [pscustomobject]@{ Executable = $_.Source; Arguments = @() } }
    }
    foreach ($candidate in $resolved) {
        if (Test-PythonCommand $candidate) { return $candidate }
    }
    # Probe execution, rather than trusting Get-Command. Prefer python.org /
    # winget's `python`, then the py launcher, before asking winget to install.
    foreach ($candidate in @(
        [pscustomobject]@{ Executable = "python"; Arguments = @() },
        [pscustomobject]@{ Executable = "py"; Arguments = @("-3") }
    )) {
        if (Test-PythonCommand $candidate) { return $candidate }
    }
    return $null
}

$PythonCmd = Get-PythonCommand
if (-not $PythonCmd) {
    if (Install-ViaWinget "Python.Python.3.12" "Python") {
        $PythonCmd = Get-PythonCommand
    }
}
if (-not $PythonCmd) {
    Write-Error "Python not found. Install Python 3.10+ first: https://www.python.org/downloads/"
    exit 1
}

function Invoke-Python {
    & $PythonCmd.Executable @($PythonCmd.Arguments) @args
}

# --- git -------------------------------------------------------------------

if (-not (Test-CommandExists "git")) {
    if (-not (Install-ViaWinget "Git.MinGit" "git")) {
        Write-Error "git not found. Install git first (needed to fetch omm from GitHub): https://git-scm.com/downloads"
        exit 1
    }
}
if (-not (Test-CommandExists "git")) {
    Write-Error "git not found. Install git first (needed to fetch omm from GitHub): https://git-scm.com/downloads"
    exit 1
}

# --- pipx --------------------------------------------------------------

function Invoke-Pipx {
    Invoke-Python -m pipx @args
}

$PipxEnvironment = "omm-model"
$LegacyPipxEnvironment = "omm"

function Get-PipxSnapshot {
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativePref = $PSNativeCommandUseErrorActionPreference
    $ok = $false
    $output = @()
    try {
        $ErrorActionPreference = "Continue"
        $PSNativeCommandUseErrorActionPreference = $false
        $output = @(Invoke-Pipx list --json 2>$null)
        $ok = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $previousNativePref
    }
    if (-not $ok) {
        throw "Could not inspect existing pipx environments; refusing an unsafe migration."
    }
    try {
        return (($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) | ConvertFrom-Json
    } catch {
        throw "Could not parse pipx environment metadata; refusing an unsafe migration."
    }
}

function Test-PipxSnapshotEnvironment {
    param($Snapshot, [string]$Name)
    if ($null -eq $Snapshot -or $null -eq $Snapshot.venvs) { return $false }
    $matches = @($Snapshot.venvs.PSObject.Properties | Where-Object { $_.Name -ceq $Name })
    return $matches.Count -eq 1
}

function Test-PipxSnapshotIdentity {
    param($Snapshot, [string]$Name, [string]$Distribution)
    if (-not (Test-PipxSnapshotEnvironment $Snapshot $Name)) { return $false }
    $property = @($Snapshot.venvs.PSObject.Properties | Where-Object { $_.Name -ceq $Name })[0]
    $metadata = $property.Value.metadata
    $main = $metadata.main_package
    if (($null -ne $metadata.environment -and $metadata.environment -cne $Name) -or $main.package -cne $Distribution -or $main.suffix -cne "") {
        return $false
    }
    if (@($main.apps | Where-Object { $_ -ceq "omm" }).Count -ne 1) { return $false }
    $expectedDir = Join-Path (Join-Path $PipxLocalVenvs $Name) "Scripts"
    $ommPaths = @($main.app_paths | ForEach-Object {
        $path = [string]$_.'__Path__'
        if ([IO.Path]::GetFileName($path) -in @("omm", "omm.exe")) { $path }
    })
    if ($ommPaths.Count -ne 1) { return $false }
    $actualDir = [IO.Path]::GetFullPath((Split-Path -Parent $ommPaths[0])).TrimEnd('\')
    $expectedDir = [IO.Path]::GetFullPath($expectedDir).TrimEnd('\')
    return $actualDir.Equals($expectedDir, [StringComparison]::OrdinalIgnoreCase)
}

function Get-PipxEnvironmentPython {
    param([string]$Name)
    $candidate = Join-Path (Join-Path $PipxLocalVenvs $Name) "Scripts\python.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $null
}

$OmmEnvironmentVerifier = @'
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

distribution, omm_home, require_source, expected_version = sys.argv[1:]
try:
    dist = importlib.metadata.distribution(distribution)
except importlib.metadata.PackageNotFoundError:
    raise SystemExit(1)

entry_points = [
    ep for ep in dist.entry_points
    if ep.group == "console_scripts" and ep.name == "omm"
]
if len(entry_points) != 1 or entry_points[0].value != "omm.cli:main":
    raise SystemExit(1)
if expected_version and dist.version != expected_version:
    raise SystemExit(1)
if require_source != "1":
    raise SystemExit(0)

raw_direct_url = dist.read_text("direct_url.json")
if not raw_direct_url:
    raise SystemExit(1)
try:
    direct_url = json.loads(raw_direct_url)
except json.JSONDecodeError:
    raise SystemExit(1)

def known_repo(value: str) -> bool:
    value = value.strip()
    if value.startswith("git@github.com:"):
        host, path = "github.com", "/" + value.split(":", 1)[1]
    else:
        parsed_repo = urlparse(value)
        host, path = (parsed_repo.hostname or "").lower(), parsed_repo.path
    normalized_path = "/" + path.strip("/").removesuffix(".git").lower()
    return host == "github.com" and normalized_path in {
        "/omm-hippo/omm",
        "/minigu5/omm",
        "/minigu5/localfit",
    }

url = str(direct_url.get("url", ""))
if direct_url.get("vcs_info") and known_repo(url):
    raise SystemExit(0)

parsed = urlparse(url)
if parsed.scheme != "file":
    raise SystemExit(1)
source = Path(url2pathname(unquote(parsed.path))).resolve()
home = Path(omm_home).resolve()
legacy_source = (home / "src").resolve()
versioned_root = (home / "sources").resolve()
is_versioned_source = (
    source.parent == versioned_root
    and re.fullmatch(r"[0-9a-fA-F]{40}", source.name) is not None
)
if source != legacy_source and not is_versioned_source:
    raise SystemExit(1)
try:
    origin = subprocess.run(
        ["git", "-C", str(source), "remote", "get-url", "origin"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    raise SystemExit(1)
raise SystemExit(0 if known_repo(origin) else 1)
'@

function Test-OmmPipxEnvironment {
    param([string]$Name, [string]$Distribution, [switch]$RequireLegacySource, [string]$ExpectedVersion = "")
    $environmentPython = Get-PipxEnvironmentPython $Name
    if (-not $environmentPython) { return $false }
    $requireSource = if ($RequireLegacySource) { "1" } else { "0" }
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativePref = $PSNativeCommandUseErrorActionPreference
    $ok = $false
    try {
        $ErrorActionPreference = "Continue"
        $PSNativeCommandUseErrorActionPreference = $false
        & $environmentPython -c $OmmEnvironmentVerifier $Distribution $OmmHome $requireSource $ExpectedVersion *> $null
        $ok = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $previousNativePref
    }
    return $ok
}

function Invoke-PipxStatus {
    param([string[]]$Arguments, [switch]$Quiet)
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativePref = $PSNativeCommandUseErrorActionPreference
    $ok = $false
    $output = @()
    try {
        $ErrorActionPreference = "Continue"
        $PSNativeCommandUseErrorActionPreference = $false
        $output = @(Invoke-Pipx @Arguments 2>&1)
        $ok = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $previousNativePref
    }
    if (-not $Quiet) {
        foreach ($line in $output) { Write-Host ([string]$line) }
    }
    return $ok
}

function Test-InstalledOmmModel {
    try { $snapshot = Get-PipxSnapshot } catch { return $false }
    if (-not (Test-PipxSnapshotIdentity $snapshot $PipxEnvironment $PipxEnvironment)) { return $false }
    if (-not (Test-OmmPipxEnvironment $PipxEnvironment $PipxEnvironment -ExpectedVersion $ExpectedVersion)) { return $false }
    $ommApp = Join-Path $PipxBinDir "omm.exe"
    $internalApp = Join-Path (Join-Path (Join-Path $PipxLocalVenvs $PipxEnvironment) "Scripts") "omm.exe"
    if (-not (Test-Path -LiteralPath $ommApp -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath $internalApp -PathType Leaf)) { return $false }
    try {
        if ((Get-FileHash -LiteralPath $ommApp -Algorithm SHA256).Hash -cne (Get-FileHash -LiteralPath $internalApp -Algorithm SHA256).Hash) {
            return $false
        }
    } catch { return $false }
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativePref = $PSNativeCommandUseErrorActionPreference
    $ok = $false
    $versionOutput = @()
    try {
        $ErrorActionPreference = "Continue"
        $PSNativeCommandUseErrorActionPreference = $false
        $versionOutput = @(& $ommApp --version 2>&1)
        $ok = $LASTEXITCODE -eq 0 -and (($versionOutput -join "`n").Trim() -ceq "omm $ExpectedVersion")
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $previousNativePref
    }
    return $ok
}

function Test-ExposedExistingEnvironment {
    param([string]$Name, [string]$Distribution, [switch]$RequireLegacySource)
    try {
        $snapshot = Get-PipxSnapshot
        if (-not (Test-PipxSnapshotIdentity $snapshot $Name $Distribution)) { return $false }
        if (-not (Test-OmmPipxEnvironment $Name $Distribution -RequireLegacySource:$RequireLegacySource)) { return $false }
        $environmentPython = Get-PipxEnvironmentPython $Name
        $internalApp = Join-Path (Join-Path (Join-Path $PipxLocalVenvs $Name) "Scripts") "omm.exe"
        $exposedApp = Join-Path $PipxBinDir "omm.exe"
        if (-not (Test-Path -LiteralPath $internalApp -PathType Leaf) -or -not (Test-Path -LiteralPath $exposedApp -PathType Leaf)) {
            return $false
        }
        if ((Get-FileHash -LiteralPath $internalApp -Algorithm SHA256).Hash -cne (Get-FileHash -LiteralPath $exposedApp -Algorithm SHA256).Hash) {
            return $false
        }
        $expectedVersion = (& $environmentPython -c "import importlib.metadata, sys; print(importlib.metadata.version(sys.argv[1]))" $Distribution).Trim()
        if ($LASTEXITCODE -ne 0) { return $false }
        $versionOutput = (& $exposedApp --version).Trim()
        return ($LASTEXITCODE -eq 0 -and $versionOutput -ceq "omm $expectedVersion")
    } catch {
        return $false
    }
}

function Test-PipxAvailable {
    try {
        Invoke-Python -m pipx --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        # A missing pipx module writes an error under $ErrorActionPreference = "Stop".
        # That is the expected signal for the bootstrap below, not an installer failure.
        return $false
    }
}

if (-not (Test-PipxAvailable)) {
    Write-Host "pipx not found, installing it..."
    Invoke-Python -m pip install --user --quiet pipx
    Invoke-Pipx ensurepath
    Update-SessionPath
}
Invoke-Pipx ensurepath
$PythonExecutable = (Invoke-Python -c "import sys; print(sys.executable)").Trim()
$PipxLocalVenvs = ([string](Invoke-Pipx environment --value PIPX_LOCAL_VENVS)).Trim()
$PipxBinDir = ([string](Invoke-Pipx environment --value PIPX_BIN_DIR)).Trim()
$PipxSnapshot = Get-PipxSnapshot
$LegacyPipxPresent = Test-PipxSnapshotEnvironment $PipxSnapshot $LegacyPipxEnvironment
if ($LegacyPipxPresent -and (
    -not (Test-PipxSnapshotIdentity $PipxSnapshot $LegacyPipxEnvironment $LegacyPipxEnvironment) -or
    -not (Test-OmmPipxEnvironment $LegacyPipxEnvironment $LegacyPipxEnvironment -RequireLegacySource)
)) {
    Write-Error "Refusing to replace unrelated pipx environment 'omm'. Remove or rename that environment manually first."
    exit 1
}
$NewPipxPresent = Test-PipxSnapshotEnvironment $PipxSnapshot $PipxEnvironment
if ($NewPipxPresent -and (
    -not (Test-PipxSnapshotIdentity $PipxSnapshot $PipxEnvironment $PipxEnvironment) -or
    -not (Test-OmmPipxEnvironment $PipxEnvironment $PipxEnvironment)
)) {
    Write-Error "Refusing to replace an unverified $PipxEnvironment pipx environment."
    exit 1
}

New-Item -ItemType Directory -Force -Path $SourcesDir | Out-Null
$StagingDir = Join-Path $SourcesDir ("checkout-" + $PID + "-" + [guid]::NewGuid().ToString("N"))
Write-Host "Cloning omm source to a versioned staging directory ..."
$CloneArgs = @("clone", "--filter=blob:none", "--quiet")
if ($Branch) {
    Write-Host "Using branch: $Branch"
    $CloneArgs += @("-b", $Branch)
}
$CloneArgs += @($RepoUrl, $StagingDir)
git @CloneArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "git clone failed."
    exit 1
}

Write-Host "Verifying commit signature ..."
$headCommit = (git -C $StagingDir rev-parse HEAD).Trim()
# Try the commit itself first - a maintainer can directly SSH-sign a merge
# commit (e.g. syncing one branch into another outside GitHub's PR flow),
# and that signature is trustworthy on its own. Only fall back to
# Resolve-SigningCommit's second-parent heuristic - for GitHub's own
# "create a merge commit" PRs, where GitHub signs the merge with a key
# this script doesn't trust and the real signature is one hop down, on the
# PR branch tip - when the direct check fails.
$verified = Test-CommitSignature -Commit $headCommit -RepoDir $StagingDir -Quiet
if (-not $verified) {
    $signedCommit = Resolve-SigningCommit -Commit $headCommit -RepoDir $StagingDir
    if ($signedCommit -ne $headCommit) {
        $verified = Test-CommitSignature -Commit $signedCommit -RepoDir $StagingDir
    } else {
        $verified = Test-CommitSignature -Commit $headCommit -RepoDir $StagingDir
    }
}
if (-not $verified) {
    Remove-Item -Recurse -Force $StagingDir
    Write-Error "Signature verification failed - refusing to install untrusted code."
    exit 1
}
$SrcDir = Join-Path $SourcesDir $headCommit
if (Test-Path $SrcDir) {
    Remove-Item -Recurse -Force $StagingDir
} else {
    Move-Item -LiteralPath $StagingDir -Destination $SrcDir
}
$versionMatch = Select-String -LiteralPath (Join-Path $SrcDir "pyproject.toml") -Pattern '^version = "([^"]+)"\s*$' | Select-Object -First 1
if ($null -eq $versionMatch) {
    Write-Error "Could not determine the project version from the verified checkout."
    exit 1
}
$ExpectedVersion = $versionMatch.Matches[0].Groups[1].Value

# Install NVML only when the machine actually exposes an NVIDIA driver.
$InstallSpec = if (Test-CommandExists "nvidia-smi") { "$SrcDir[nvidia]" } else { $SrcDir }

Write-Host "Installing omm (editable) from $SrcDir ..."
# pipx names the new environment after the distribution (`omm-model`)
# while old Git installs used `omm`. Install and verify the new environment
# first so an install failure leaves the working legacy CLI untouched. pipx
# --force moves the shared `omm` app link to the new environment; current pipx
# preserves that foreign-owned link when the legacy environment is removed.
$installArguments = @("install", "--force", "--editable", "--python", $PythonExecutable, $InstallSpec)
function Restore-PipxAfterFailedInstall {
    $rollbackState = "not-needed"
    try { $currentSnapshot = Get-PipxSnapshot } catch { $currentSnapshot = $null }
    if ($null -ne $currentSnapshot -and (Test-PipxSnapshotEnvironment $currentSnapshot $PipxEnvironment)) {
        if ($NewPipxPresent) {
            [void](Invoke-PipxStatus -Arguments @("reinstall", $PipxEnvironment) -Quiet)
        } else {
            [void](Invoke-PipxStatus -Arguments @("uninstall", $PipxEnvironment) -Quiet)
        }
    }
    if ($LegacyPipxPresent) {
        if ((Invoke-PipxStatus -Arguments @("reinstall", $LegacyPipxEnvironment) -Quiet) -and
            (Test-ExposedExistingEnvironment $LegacyPipxEnvironment $LegacyPipxEnvironment -RequireLegacySource)) {
            $rollbackState = "verified"
        } else {
            $rollbackState = "uncertain"
        }
    } elseif ($NewPipxPresent) {
        if (Test-ExposedExistingEnvironment $PipxEnvironment $PipxEnvironment) {
            $rollbackState = "verified"
        } else {
            $rollbackState = "uncertain"
        }
    } else {
        try { $currentSnapshot = Get-PipxSnapshot } catch { $currentSnapshot = $null }
        if ($null -eq $currentSnapshot -or (Test-PipxSnapshotEnvironment $currentSnapshot $PipxEnvironment)) {
            $rollbackState = "uncertain"
        }
    }
    return $rollbackState
}
function Write-FailedInstallRecovery {
    param([string]$Reason, [string]$RollbackState)
    Write-Warning $Reason
    if ($RollbackState -eq "verified") {
        Write-Warning "The pre-existing omm command was restored and verified."
    } elseif ($RollbackState -eq "uncertain") {
        Write-Warning "The previous environment was not removed, but its omm command could not be verified after rollback; run 'pipx reinstall omm' or 'pipx reinstall omm-model'."
    }
}
if (-not (Invoke-PipxStatus -Arguments $installArguments)) {
    $rollbackState = Restore-PipxAfterFailedInstall
    Write-FailedInstallRecovery "pipx install failed; the legacy environment was not removed." $rollbackState
    exit 1
}
if (-not (Test-InstalledOmmModel)) {
    $rollbackState = Restore-PipxAfterFailedInstall
    Write-FailedInstallRecovery "The new $PipxEnvironment environment or its omm command failed verification; the legacy environment was not removed." $rollbackState
    exit 1
}
if ($LegacyPipxPresent) {
    Write-Host "Removing verified legacy pipx environment: $LegacyPipxEnvironment"
    if (-not (Invoke-PipxStatus -Arguments @("uninstall", $LegacyPipxEnvironment))) {
        Write-Warning "Could not remove the verified legacy pipx environment."
        if (Test-InstalledOmmModel) {
            Write-Warning "The new omm command remains installed and was verified."
        } elseif ((Invoke-PipxStatus -Arguments $installArguments) -and (Test-InstalledOmmModel)) {
            Write-Warning "The new omm command was repaired and verified after the pipx failure."
        } else {
            Write-Warning "pipx may have removed the omm command link before failing; repair could not be verified. Run 'pipx reinstall omm-model'."
        }
        exit 1
    }
    if (-not (Test-InstalledOmmModel)) {
        Write-Warning "Repairing the new omm command after legacy cleanup ..."
        if (-not (Invoke-PipxStatus -Arguments $installArguments) -or -not (Test-InstalledOmmModel)) {
            Write-Error "The new omm command could not be verified after legacy cleanup."
            exit 1
        }
    }
}

# Marks custom OMM_HOME directories as installer-managed. The uninstaller
# requires this marker before removing anything from a non-default home.
Set-Content -LiteralPath (Join-Path $OmmHome ".omm-managed") -Value "omm installer managed home v1" -Encoding Ascii

# pipx now points at the verified checkout above. Best-effort cleanup of old
# versioned checkouts is safe; a live old omm process may keep one locked on
# Windows, in which case it remains available and a future reinstall retries.
Get-ChildItem -LiteralPath $SourcesDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    if ([IO.Path]::GetFullPath($_.FullName) -ne [IO.Path]::GetFullPath($SrcDir)) {
        try { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
        catch { Write-Warning "Could not remove old source checkout $($_.FullName): $_" }
    }
}
$LegacySrcDir = Join-Path $OmmHome "src"
if (Test-Path -LiteralPath $LegacySrcDir) {
    try { Remove-Item -LiteralPath $LegacySrcDir -Recurse -Force }
    catch { Write-Warning "Could not remove legacy source checkout ${LegacySrcDir}: $_" }
}

Write-Host ""
Write-Host "Done. If 'omm' isn't found, open a new PowerShell window (pipx just updated your PATH)."
Write-Host "Try:  omm scan"
