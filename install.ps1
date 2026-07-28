# Installs omm (Open source Model Manager) as an isolated CLI command via pipx.
# Usage: [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1 | iex
# To install a non-default branch: $env:OMM_INSTALL_BRANCH = "beta"; irm ... | iex
# Do not try to fix the first-download TLS problem inside this script: `irm`
# fetches the script before PowerShell can execute any of its contents.
$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/omm-hippo/omm.git"
$SrcDir = Join-Path $env:USERPROFILE ".omm\src"

# Set $env:OMM_INSTALL_BRANCH before piping this script into iex to install
# from a branch other than the repo default (e.g. to try a beta build).
$Branch = $env:OMM_INSTALL_BRANCH

# Trust anchor for the signature check below - must stay identical to
# src/omm/trust/allowed_signers in the repo (that copy is what `omm
# update` verifies future commits against once installed; this one is
# the TOFU root for a brand new machine, since there's no prior install
# to carry a trusted copy yet).
$AllowedSignersContent = "seong381400@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPh12ERbI3Yx6DPiaROPjCyI2GIQXb9Ihbp9J9L4bnpe
ahseongchoi@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO5UPWuM/1GxGo5TQ5nEJm9UvXShygIozjbvxB1VT9u6"

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

Write-Host "Cloning omm source to $SrcDir ..."
if (Test-Path $SrcDir) {
    Remove-Item -Recurse -Force $SrcDir
}
$CloneArgs = @("clone", "--filter=blob:none", "--quiet")
if ($Branch) {
    Write-Host "Using branch: $Branch"
    $CloneArgs += @("-b", $Branch)
}
$CloneArgs += @($RepoUrl, $SrcDir)
git @CloneArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "git clone failed."
    exit 1
}

Write-Host "Verifying commit signature ..."
$headCommit = (git -C $SrcDir rev-parse HEAD).Trim()
# Try the commit itself first - a maintainer can directly SSH-sign a merge
# commit (e.g. syncing one branch into another outside GitHub's PR flow),
# and that signature is trustworthy on its own. Only fall back to
# Resolve-SigningCommit's second-parent heuristic - for GitHub's own
# "create a merge commit" PRs, where GitHub signs the merge with a key
# this script doesn't trust and the real signature is one hop down, on the
# PR branch tip - when the direct check fails.
$verified = Test-CommitSignature -Commit $headCommit -RepoDir $SrcDir -Quiet
if (-not $verified) {
    $signedCommit = Resolve-SigningCommit -Commit $headCommit -RepoDir $SrcDir
    if ($signedCommit -ne $headCommit) {
        $verified = Test-CommitSignature -Commit $signedCommit -RepoDir $SrcDir
    } else {
        $verified = Test-CommitSignature -Commit $headCommit -RepoDir $SrcDir
    }
}
if (-not $verified) {
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
