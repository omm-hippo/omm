# Installs omm (Open source Model Manager) as an isolated CLI command via pipx.
# Usage: irm https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1's default SecurityProtocol can exclude TLS 1.2 on
# older Windows builds / .NET Framework versions - GitHub requires TLS 1.2+,
# so without this, Invoke-RestMethod against raw.githubusercontent.com can
# fail (sometimes silently, leaving $s as $null instead of throwing).
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$RepoUrl = "https://github.com/omm-hippo/omm.git"
$SrcDir = Join-Path $env:USERPROFILE ".omm\src"

# Trust anchor for the signature check below - must stay identical to
# src/omm/trust/allowed_signers in the repo (that copy is what `omm
# update` verifies future commits against once installed; this one is
# the TOFU root for a brand new machine, since there's no prior install
# to carry a trusted copy yet).
$AllowedSignersContent = "seong381400@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPh12ERbI3Yx6DPiaROPjCyI2GIQXb9Ihbp9J9L4bnpe"

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

# Verifies $Commit (a commit-ish, usually HEAD) in the git repo at $RepoDir
# is SSH-signed by a key from $AllowedSignersContent. Fails closed: git too
# old to check SSH signatures, or verification itself erroring out, is
# treated the same as an actual bad signature - "can't verify" must never
# silently mean "trust it anyway".
function Test-CommitSignature {
    param([string]$Commit, [string]$RepoDir)

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

    $verifyOutput = git -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$($signersFile.FullName)" `
        -C $RepoDir verify-commit $Commit 2>&1
    $ok = $LASTEXITCODE -eq 0
    Remove-Item $signersFile.FullName -Force -ErrorAction SilentlyContinue
    if (-not $ok) {
        Write-Warning ($verifyOutput | Out-String)
    }
    return $ok
}

# --- python --------------------------------------------------------------
#
# Prefer the `python` command (what python.org's installer and winget's
# Python package both put on PATH); fall back to the `py` launcher, which
# some existing installs expose without a bare `python` on PATH.

function Get-PythonCommand {
    if (Test-CommandExists "python") { return @("python") }
    if (Test-CommandExists "py") { return @("py", "-3") }
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
    & $PythonCmd[0] @($PythonCmd | Select-Object -Skip 1) @args
}

$PyOk = (Invoke-Python -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)")
if ($PyOk -ne "1") {
    $pyVersion = (Invoke-Python --version)
    Write-Error "omm requires Python 3.10+, found: $pyVersion"
    exit 1
}

# --- git -------------------------------------------------------------------

if (-not (Test-CommandExists "git")) {
    if (-not (Install-ViaWinget "Git.Git" "git")) {
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
    if (Test-CommandExists "pipx") {
        & pipx @args
    } else {
        Invoke-Python -m pipx @args
    }
}

function Test-PipxAvailable {
    if (Test-CommandExists "pipx") { return $true }
    Invoke-Python -m pipx --version *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-PipxAvailable)) {
    Write-Host "pipx not found, installing it..."
    Invoke-Python -m pip install --user --quiet pipx
    Invoke-Pipx ensurepath
    Update-SessionPath
}

Write-Host "Cloning omm source to $SrcDir ..."
if (Test-Path $SrcDir) {
    Remove-Item -Recurse -Force $SrcDir
}
git clone --filter=blob:none --quiet $RepoUrl $SrcDir
if ($LASTEXITCODE -ne 0) {
    Write-Error "git clone failed."
    exit 1
}

Write-Host "Verifying commit signature ..."
$headCommit = (git -C $SrcDir rev-parse HEAD).Trim()
if (-not (Test-CommitSignature -Commit $headCommit -RepoDir $SrcDir)) {
    Remove-Item -Recurse -Force $SrcDir
    Write-Error "Signature verification failed - refusing to install untrusted code."
    exit 1
}

# NVIDIA GPUs are common on Windows machines (unlike Mac, which hasn't
# shipped one since 2016), so always pull in the VRAM-detection extra here.
$InstallSpec = "$SrcDir[nvidia]"

Write-Host "Installing omm (editable) from $SrcDir ..."
Invoke-Pipx install --force --editable $InstallSpec
if ($LASTEXITCODE -ne 0) {
    Write-Error "pipx install failed."
    exit 1
}

Write-Host ""
Write-Host "Done. If 'omm' isn't found, open a new PowerShell window (pipx just updated your PATH)."
Write-Host "Try:  omm scan"
