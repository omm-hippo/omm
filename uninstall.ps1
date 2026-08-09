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

$python = Get-Command python -ErrorAction SilentlyContinue
$py = Get-Command py -ErrorAction SilentlyContinue
$pipxExitCode = $null
try {
    if ($python) {
        & $python.Source -m pipx uninstall omm
        $pipxExitCode = $LASTEXITCODE
    } elseif ($py) {
        & $py.Source -3 -m pipx uninstall omm
        $pipxExitCode = $LASTEXITCODE
    } elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
        & pipx uninstall omm
        $pipxExitCode = $LASTEXITCODE
    } else {
        Write-Warning "pipx was not found; removing installer-managed files only."
    }
} catch {
    Write-Warning "pipx uninstall failed: $_"
}
if ($null -ne $pipxExitCode -and $pipxExitCode -ne 0) {
    Write-Warning "pipx uninstall exited with code $pipxExitCode; removing installer-managed files only."
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
    # Remove only application-owned paths. A custom OMM_HOME may contain
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
} else {
    Write-Host "Removed omm. Models and settings remain in $resolvedHome (use -Purge to remove them)."
}
