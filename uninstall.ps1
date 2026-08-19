# Remove the omm CLI and installer-managed source checkouts.
# Models and settings are preserved unless -Purge is passed.
param([switch]$Purge)
$ErrorActionPreference = "Stop"

$OmmHome = if ($env:OMM_HOME) { $env:OMM_HOME } else { Join-Path $env:USERPROFILE ".omm" }
$resolvedHome = [IO.Path]::GetFullPath($OmmHome).TrimEnd('\')
$profileHome = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
$defaultHome = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".omm")).TrimEnd('\')
$currentDirectory = [IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\')
$root = [IO.Path]::GetPathRoot($resolvedHome).TrimEnd('\')
if (-not $resolvedHome -or $resolvedHome -eq $root -or $resolvedHome -eq $profileHome) {
    throw "Refusing unsafe OMM_HOME: $resolvedHome"
}
$homePrefix = $resolvedHome + [IO.Path]::DirectorySeparatorChar
if ($currentDirectory -eq $resolvedHome -or $currentDirectory.StartsWith($homePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to uninstall while the current directory is inside OMM_HOME: $resolvedHome"
}
$marker = Join-Path $resolvedHome ".omm-managed"
if ((Test-Path -LiteralPath $resolvedHome -PathType Container) -and $resolvedHome -ne $defaultHome -and -not (Test-Path -LiteralPath $marker -PathType Leaf)) {
    throw "Refusing unrecognized custom OMM_HOME (missing .omm-managed): $resolvedHome"
}

$PipxCommand = $null
$pipxCandidates = @()
$python = Get-Command python -ErrorAction SilentlyContinue
$py = Get-Command py -ErrorAction SilentlyContinue
$pipx = Get-Command pipx -ErrorAction SilentlyContinue
if ($python) { $pipxCandidates += [pscustomobject]@{ Executable = $python.Source; Arguments = @("-m", "pipx") } }
if ($py) { $pipxCandidates += [pscustomobject]@{ Executable = $py.Source; Arguments = @("-3", "-m", "pipx") } }
if ($pipx) { $pipxCandidates += [pscustomobject]@{ Executable = $pipx.Source; Arguments = @() } }

foreach ($candidate in $pipxCandidates) {
    try {
        & $candidate.Executable @($candidate.Arguments) --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $PipxCommand = $candidate
            break
        }
    } catch {
        # Try the next candidate. A Python executable may exist without the
        # pipx module while a standalone pipx command is still available.
    }
}

function Invoke-Pipx {
    & $PipxCommand.Executable @($PipxCommand.Arguments) @args
}

function Stop-UninstallPreservingSources {
    param([string]$Reason, [ValidateSet("unchanged", "verified", "uncertain")][string]$CommandState = "unchanged")
    [Console]::Error.WriteLine($Reason)
    [Console]::Error.WriteLine("OMM was not fully uninstalled. Source checkouts and user data were preserved.")
    if ($CommandState -eq "verified") {
        [Console]::Error.WriteLine("The omm command was repaired and verified after the pipx failure.")
    } elseif ($CommandState -eq "uncertain") {
        [Console]::Error.WriteLine("pipx may have removed the omm command link before failing; command repair could not be verified.")
    } else {
        [Console]::Error.WriteLine("No pipx uninstall mutation was attempted, so the existing command was left unchanged.")
    }
    [Console]::Error.WriteLine("Recovery: repair pipx, run 'pipx uninstall omm-model' (and 'pipx uninstall omm' only if it is OMM), then rerun this script.")
    exit 1
}

function Remove-OmmOwnedData {
    # Delete only paths the application owns. A custom OMM_HOME may contain
    # unrelated files, so never recursively delete the container itself.
    foreach ($name in @("models", "evaluations", "catalog-history", "session")) {
        $target = Join-Path $resolvedHome $name
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
    $ownedFiles = @(
        "config.json", "models.json", "link-ownership.json", "rules.json",
        "recommend-model.json", "calibration.json", "benchmark_history.json",
        "contribute_state.json", "telemetry.log", "telemetry_pending.json",
        "update_check.json", ".omm-managed"
    )
    foreach ($name in $ownedFiles) {
        foreach ($candidate in @((Join-Path $resolvedHome $name), (Join-Path $resolvedHome ($name + ".lock")))) {
            if (Test-Path -LiteralPath $candidate) {
                Remove-Item -LiteralPath $candidate -Force
            }
        }
    }
    $ownedJson = $ownedFiles | Where-Object { $_ -like "*.json" }
    foreach ($name in $ownedJson) {
        Get-ChildItem -LiteralPath $resolvedHome -File -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -like ($name + ".corrupt-*") -or $_.Name -like ("." + $name + ".*.tmp")
        } | Remove-Item -Force
    }
    if (Test-Path -LiteralPath $resolvedHome) {
        $remaining = Get-ChildItem -LiteralPath $resolvedHome -Force -ErrorAction Stop | Select-Object -First 1
        if ($null -eq $remaining) {
            Remove-Item -LiteralPath $resolvedHome -Force
        }
    }
    Write-Host "Removed omm models, settings, and cached data. Unrelated files in $resolvedHome were preserved."
}

if ($null -eq $PipxCommand) {
    # A data-only managed home has no installer source or pipx environment to
    # identify. In explicit purge mode, remove only allowlisted OMM data and
    # leave every unrelated entry untouched.
    $hasSourceCheckout = (Test-Path -LiteralPath (Join-Path $resolvedHome "src")) -or
        (Test-Path -LiteralPath (Join-Path $resolvedHome "sources"))
    if ($Purge -and -not $hasSourceCheckout) {
        Remove-OmmOwnedData
        $global:LASTEXITCODE = 0
        return
    }
    Stop-UninstallPreservingSources "pipx was not found; refusing to remove source checkouts."
}

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
    if (-not $ok) { throw "pipx environments could not be listed." }
    try {
        return (($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) | ConvertFrom-Json
    } catch {
        throw "pipx environment metadata was not valid JSON."
    }
}

function Test-PipxSnapshotEnvironment {
    param($Snapshot, [string]$Name)
    if ($null -eq $Snapshot -or $null -eq $Snapshot.venvs) { return $false }
    return (@($Snapshot.venvs.PSObject.Properties | Where-Object { $_.Name -ceq $Name }).Count -eq 1)
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

$OmmEnvironmentVerifier = @'
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

distribution, omm_home, require_source = sys.argv[1:]
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

function Get-PipxEnvironmentPython {
    param([string]$Name)
    $candidate = Join-Path (Join-Path $PipxLocalVenvs $Name) "Scripts\python.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $null
}

function Test-OmmPipxEnvironment {
    param([string]$Name, [string]$Distribution, [switch]$RequireLegacySource)
    $environmentPython = Get-PipxEnvironmentPython $Name
    if (-not $environmentPython) { return $false }
    $requireSource = if ($RequireLegacySource) { "1" } else { "0" }
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativePref = $PSNativeCommandUseErrorActionPreference
    $ok = $false
    try {
        $ErrorActionPreference = "Continue"
        $PSNativeCommandUseErrorActionPreference = $false
        & $environmentPython -c $OmmEnvironmentVerifier $Distribution $resolvedHome $requireSource *> $null
        $ok = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $previousNativePref
    }
    return $ok
}

function Invoke-PipxStatus {
    param([string[]]$Arguments)
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
    foreach ($line in $output) { Write-Host ([string]$line) }
    return $ok
}

function Test-ExposedOmmEnvironment {
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

function Repair-AfterFailedUninstall {
    param([string]$Name, [string]$Distribution, [switch]$RequireLegacySource)
    if (-not (Invoke-PipxStatus -Arguments @("reinstall", $Name))) { return $false }
    return (Test-ExposedOmmEnvironment $Name $Distribution -RequireLegacySource:$RequireLegacySource)
}

try {
    $PipxLocalVenvs = ([string](Invoke-Pipx environment --value PIPX_LOCAL_VENVS)).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $PipxLocalVenvs) { throw "pipx venv location could not be determined." }
    $PipxBinDir = ([string](Invoke-Pipx environment --value PIPX_BIN_DIR)).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $PipxBinDir) { throw "pipx app location could not be determined." }
    $PipxSnapshot = Get-PipxSnapshot
} catch {
    Stop-UninstallPreservingSources ([string]$_)
}

$hasNew = Test-PipxSnapshotEnvironment $PipxSnapshot "omm-model"
$hasLegacy = Test-PipxSnapshotEnvironment $PipxSnapshot "omm"
$newIsOmm = $hasNew -and (Test-PipxSnapshotIdentity $PipxSnapshot "omm-model" "omm-model") -and (Test-OmmPipxEnvironment "omm-model" "omm-model")
$legacyIsOmm = $hasLegacy -and (Test-PipxSnapshotIdentity $PipxSnapshot "omm" "omm") -and (Test-OmmPipxEnvironment "omm" "omm" -RequireLegacySource)
if ($hasNew -and -not $newIsOmm) {
    Stop-UninstallPreservingSources "The omm-model environment could not be verified as OMM; it was preserved."
}
if ($hasLegacy -and -not $legacyIsOmm) {
    Write-Warning "Preserving unrelated pipx environment 'omm'."
    Stop-UninstallPreservingSources "Resolve the pipx environment-name conflict manually before uninstalling OMM."
}

$removed = @()
foreach ($pipxEnvironment in @("omm", "omm-model")) {
    if ($pipxEnvironment -eq "omm" -and -not $legacyIsOmm) { continue }
    if ($pipxEnvironment -eq "omm-model" -and -not $newIsOmm) { continue }
    if (-not (Invoke-PipxStatus -Arguments @("uninstall", $pipxEnvironment))) {
        $requireLegacySource = $pipxEnvironment -eq "omm"
        $repaired = Repair-AfterFailedUninstall $pipxEnvironment $pipxEnvironment -RequireLegacySource:$requireLegacySource
        $commandState = if ($repaired) { "verified" } else { "uncertain" }
        Stop-UninstallPreservingSources "pipx uninstall $pipxEnvironment failed." $commandState
    }
    $removed += $pipxEnvironment
}

if ($removed.Count -gt 0) {
    try { $PipxSnapshot = Get-PipxSnapshot } catch { Stop-UninstallPreservingSources ([string]$_) }
    foreach ($pipxEnvironment in $removed) {
        if (Test-PipxSnapshotEnvironment $PipxSnapshot $pipxEnvironment) {
            Stop-UninstallPreservingSources "pipx still reports $pipxEnvironment after uninstall."
        }
    }
} elseif ((Test-Path -LiteralPath (Join-Path $resolvedHome "src")) -or (Test-Path -LiteralPath (Join-Path $resolvedHome "sources"))) {
    Stop-UninstallPreservingSources "No verified OMM pipx environment was removed; refusing to remove source checkouts."
}
# A failed best-effort pipx probe must not leak its native exit code into a
# caller such as a GitHub Actions pwsh step after cleanup itself succeeds.
$global:LASTEXITCODE = 0

foreach ($name in @("src", "sources")) {
    $target = Join-Path $resolvedHome $name
    if (Test-Path -LiteralPath $target) {
        try { Remove-Item -LiteralPath $target -Recurse -Force }
        catch { Write-Warning "Could not remove $target (an omm process may still be running): $_" }
    }
}

if ($Purge) {
    Remove-OmmOwnedData
} else {
    Write-Host "Removed omm. Models and settings remain in $resolvedHome (use -Purge to remove them)."
}
