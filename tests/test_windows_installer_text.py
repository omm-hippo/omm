import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 parser")
def test_powershell_51_parses_installer_and_uninstaller():
    command = r"""
$allErrors = @()
foreach ($name in @('install.ps1', 'uninstall.ps1')) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $PWD $name), [ref]$tokens, [ref]$errors
    ) | Out-Null
    $allErrors += $errors
}
if ($allErrors.Count -gt 0) {
    $allErrors | ForEach-Object { Write-Error $_ }
    exit 1
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_windows_readme_sets_tls_before_downloading_installer():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    command = "[Net.ServicePointManager]::SecurityProtocol"
    assert command in readme
    assert readme.index(command) < readme.index("irm https://omm.run/install.ps1")
    assert "SecurityProtocol -bor" not in readme
    assert "SecurityProtocol = [Net.SecurityProtocolType]::Tls12" in readme


def test_installer_probes_a_runnable_supported_python_not_just_path_presence():
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "function Test-PythonCommand" in script
    assert "sys.version_info >= (3, 10)" in script
    assert 'Executable = "python"' in script
    assert 'Executable = "py"; Arguments = @("-3")' in script
    assert "WindowsApps aliases" in script
    assert "System.Diagnostics.ProcessStartInfo" in script
    assert "WaitForExit(5000)" in script
    assert "$process.Kill()" in script


def test_installer_python_probe_drains_stderr_before_waiting():
    """A child that fills the ~4KB stderr pipe buffer before exiting (a
    couple of broken .pth files, a noisy sitecustomize.py) would otherwise
    block on the write and hit WaitForExit(5000), getting misclassified as
    an unusable Python - see t1-installers-02."""
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")
    probe = script.split("function Test-PythonCommand {", 1)[1].split(
        "function Get-PythonCommand", 1
    )[0]
    assert probe.index("StandardOutput.ReadToEndAsync()") < probe.index("WaitForExit(5000)")
    assert probe.index("StandardError.ReadToEndAsync()") < probe.index("WaitForExit(5000)")
    assert "WaitForExit(5000)" in probe
    assert "$process.Kill()" in probe


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell required",
)
def test_windows_python_probe_survives_a_noisy_child_stderr(tmp_path):
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    start = ps1.index("function Test-PythonCommand {")
    end = ps1.index("function Get-PythonCommand", start)
    probe_fn = ps1[start:end]

    def run(env_extra: dict) -> subprocess.CompletedProcess:
        script_path = tmp_path / "probe.ps1"
        script_path.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"{probe_fn}\n"
            "$r = Test-PythonCommand ([pscustomobject]@{ Executable = $args[0]; Arguments = @() })\n"
            "if ($r) { exit 0 } else { exit 3 }\n",
            encoding="utf-8",
        )
        env = {**os.environ, **env_extra}
        return subprocess.run(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script_path), sys.executable,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    baseline = run({})
    if baseline.returncode == 3:
        pytest.skip("the test interpreter itself is not considered usable by the probe")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr

    noisy_dir = tmp_path / "noisy"
    noisy_dir.mkdir()
    (noisy_dir / "sitecustomize.py").write_text(
        "import sys\nsys.stderr.write('x' * 200000)\nsys.stderr.flush()\n",
        encoding="utf-8",
    )
    noisy = run({"PYTHONPATH": str(noisy_dir)})
    assert noisy.returncode == 0, noisy.stdout + noisy.stderr


def test_install_ps1_removal_and_signers_file_use_literal_path():
    """A -Path argument (as opposed to -LiteralPath) treats `[`/`]` in
    OMM_HOME or %TEMP% as wildcard glob characters. A path containing them
    would silently no-op the cleanup instead of removing the unverified
    staging directory or temp signers file - see t1-installers-03."""
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for line in ps1.splitlines():
        if "Remove-Item" not in line:
            continue
        if re.search(r"\|\s*Remove-Item", line):
            continue
        assert "-LiteralPath" in line, line
    assert "Set-Content -Path" not in ps1


def test_installer_rejects_microsoft_store_python_and_prefers_py_launcher():
    """Live failure 2026-08-23: on a PC whose PATH lacked python.org's dir,
    the bare-name `python` fallback resolved to the Microsoft Store alias.
    Store apps virtualize %LOCALAPPDATA% writes, so pipx wrote its venv to
    ...\\LocalCache\\Local\\ and then read the real path ("failed to locate
    pyvenv.cfg"). The probe must reject Store interpreters by
    sys.executable, and `py -3` must be tried before bare `python`."""
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")
    probe = script.split("function Test-PythonCommand {", 1)[1].split("function Get-PythonCommand", 1)[0]
    assert 'windowsapps' in probe and 'pythonsoftwarefoundation.python' in probe
    assert "and not store" in probe
    fallbacks = script.split("# Probe execution, rather than trusting Get-Command", 1)[1]
    assert fallbacks.index('Executable = "py"; Arguments = @("-3")') < fallbacks.index('Executable = "python"')
    assert "Microsoft Store" in script and "python.org/downloads" in script


def test_installers_repair_a_broken_pipx_shared_venv_before_installing():
    """Seen live on a fresh-user run (Windows 11, old pipx present): the
    shared pip raised `cannot import name 'get_runnable_pip'` and every
    `pipx install` died at "determining package name". Both installers
    must probe that venv's pip and rebuild it rather than fail there."""
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "function Repair-PipxSharedLibs" in ps1
    assert "function Test-SafePipxSharedLibsPath" in ps1
    assert "Test-SafePipxSharedLibsPath $shared" in ps1
    assert "PIPX_SHARED_LIBS" in ps1
    assert ps1.index("Repair-PipxSharedLibs\n$PythonExecutable") > ps1.index("function Repair-PipxSharedLibs")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "pipx_shared_dir_is_safe()" in sh
    assert 'pipx_shared_dir_is_safe "$PIPX_SHARED_LIBS"' in sh
    assert 'PIPX_SHARED_LIBS=$(run_pipx environment --value PIPX_SHARED_LIBS' in sh
    assert 'rm -rf "$PIPX_SHARED_LIBS"' in sh
    assert sh.index("PIPX_SHARED_LIBS") < sh.index("run_pipx install --force")


def test_installers_retry_pipx_install_once_after_rebuilding_shared_venv():
    """The pre-install probe cannot see a shared pip that pipx half-upgrades
    *during* the install (two pipx copies, one shared dir - reproduced on
    Windows 11, 2026-08-23). The first failure must wipe the shared venv and
    retry exactly once instead of giving up."""
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "function Invoke-PipxInstallWithRepair" in ps1
    assert "if (-not (Invoke-PipxInstallWithRepair))" in ps1
    assert ps1.count("Invoke-PipxStatus -Arguments $installArguments") >= 3  # first try + retry + later verify paths
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "pipx_install_with_repair() {" in sh
    assert "if ! pipx_install_with_repair; then" in sh
    assert 'pipx_shared_dir_is_safe "$shared"' in sh
    assert 'rm -rf "$shared"' in sh


def test_installer_trust_anchor_matches_allowed_signers_file():
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")
    expected = (ROOT / "src" / "omm" / "trust" / "allowed_signers").read_text(
        encoding="utf-8"
    ).strip()
    match = re.search(r'\$AllowedSignersContent = "(.*?)"', script, re.DOTALL)

    assert match is not None
    assert match.group(1) == expected

    sh_script = (ROOT / "install.sh").read_text(encoding="utf-8")
    sh_match = re.search(r'ALLOWED_SIGNERS_CONTENT="(.*?)"', sh_script, re.DOTALL)
    assert sh_match is not None
    assert sh_match.group(1) == expected


def test_bootstrap_verifiers_keep_the_same_fail_closed_contract():
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert "2.34" in script
        assert "verify-commit" in script
        assert "gpg.format=ssh" in script
        assert "allowedSignersFile" in script
        assert "Signature verification failed" in script
    assert "$parents.Count -eq 3" in ps1
    assert '[ "$#" -eq 3 ]' in sh


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell required",
)
def test_windows_resolve_signing_commit_fails_closed_when_rev_list_errors(tmp_path):
    """If `git rev-list --parents` itself fails (corrupted object store,
    interrupted clone), Resolve-SigningCommit must fall back to treating the
    input commit as-is instead of crashing on `$null.Trim()` - which would
    skip the caller's cleanup of the unverified staging directory. Mirrors
    trust._signing_commit's behavior - see t1-installers-07."""
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    start = ps1.index("function Resolve-SigningCommit {")
    end = ps1.index("# Verifies $Commit", start)
    fn = ps1[start:end]

    def run(git_stub_body: str) -> str:
        script_path = tmp_path / "resolve.ps1"
        script_path.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"function git {{\n{git_stub_body}\n}}\n"
            f"{fn}\n"
            "Resolve-SigningCommit -Commit abc -RepoDir C:\\nope\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    assert run("$global:LASTEXITCODE = 128") == "abc"
    assert run("'c p1 p2'\n    $global:LASTEXITCODE = 0") == "p2"
    assert run("'c p1'\n    $global:LASTEXITCODE = 0") == "abc"


def test_unix_installer_trust_anchor_matches_allowed_signers_file():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    expected = (ROOT / "src" / "omm" / "trust" / "allowed_signers").read_text(
        encoding="utf-8"
    ).strip()
    match = re.search(r'ALLOWED_SIGNERS_CONTENT="(.*?)"', script, re.DOTALL)

    assert match is not None
    assert match.group(1) == expected


def test_installer_treats_missing_pipx_module_as_unavailable():
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")
    probe = script.split("function Test-PipxAvailable {", 1)[1].split(
        "if (-not (Test-PipxAvailable))", 1
    )[0]

    assert "try {" in probe
    assert "Invoke-Python -m pipx --version *> $null" in probe
    assert "} catch {" in probe
    assert "return $false" in probe


def test_installer_checks_git_signature_exit_code_after_non_terminating_stderr():
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")
    verifier = script.split("function Test-CommitSignature {", 1)[1].split(
        "# --- python", 1
    )[0]

    assert '$previousErrorActionPreference = $ErrorActionPreference' in verifier
    assert '$ErrorActionPreference = "Continue"' in verifier
    assert "try {" in verifier
    assert "-C $RepoDir verify-commit $Commit 2>&1" in verifier
    assert "$ok = $LASTEXITCODE -eq 0" in verifier
    assert "} finally {" in verifier
    assert "$ErrorActionPreference = $previousErrorActionPreference" in verifier


def test_installers_pin_pipx_to_validated_python_and_use_versioned_staging():
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'Invoke-Python -m pipx @args' in ps1
    assert '$installArguments = @("install", "--force", "--editable", "--python", $PythonExecutable, $InstallSpec)' in ps1
    assert '--source winget' in ps1
    assert 'checkout-' in ps1 and '$SourcesDir' in ps1

    assert '"$PY" -m pipx "$@"' in sh
    assert '--python "$PY" "$INSTALL_SPEC"' in sh
    assert 'checkout.$$' in sh and '$SOURCES_DIR' in sh
    assert '.bashrc' in sh and '.zshrc' in sh


def test_installers_abort_a_stalled_clone_instead_of_hanging_forever():
    """No `timeout` binary on stock macOS, so the client-side deadline has to
    come from git's own low-speed knobs (and curl's --max-time for the
    Homebrew bootstrap) - see t1-installers-08."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert sh.index("http.lowSpeedTime=60") < sh.index("clone --filter=blob:none")
    clone_block = sh.split("clone --filter=blob:none", 1)[1].split("fi\n", 1)[0]
    assert 'rm -rf "$STAGING_DIR"' in clone_block
    assert "--max-time 300" in sh
    assert re.search(r"^\s*timeout\s", sh, re.M) is None

    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    clone_args_line = next(line for line in ps1.splitlines() if line.strip().startswith("$CloneArgs = @("))
    assert '"http.lowSpeedTime=60"' in clone_args_line


def test_installers_never_reuse_an_existing_commit_directory_without_refreshing_it():
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'Move-Item -LiteralPath $SrcDir -Destination $PreviousSrcDir' in ps1
    assert 'Move-Item -LiteralPath $StagingDir -Destination $SrcDir' in ps1
    assert 'Remove-Item -Recurse -Force $StagingDir\n} else {' not in ps1

    assert 'mv "$SRC_DIR" "$PREVIOUS_SRC_DIR"' in sh
    assert 'mv "$STAGING_DIR" "$SRC_DIR"' in sh
    assert 'if [ -d "$SRC_DIR" ]; then\n    rm -rf "$STAGING_DIR"' not in sh


def test_uninstallers_exist_and_preserve_models_without_purge():
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert "omm-model" in script
        assert "omm" in script
        assert "list --json" in script
        assert "PIPX_LOCAL_VENVS" in script
    assert "if ($Purge)" in ps1
    assert 'if [ "$PURGE" = "1" ]' in sh


def test_installers_verify_new_environment_before_removing_verified_legacy():
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert '$PipxEnvironment = "omm-model"' in ps1
    assert '$LegacyPipxEnvironment = "omm"' in ps1
    assert 'Removing verified legacy pipx environment: $LegacyPipxEnvironment' in ps1
    assert ps1.index("if (-not (Test-InstalledOmmModel))") < ps1.index(
        'Removing verified legacy pipx environment: $LegacyPipxEnvironment'
    )
    assert 'Invoke-PipxStatus -Arguments @("uninstall", $LegacyPipxEnvironment)' in ps1

    assert 'PIPX_ENV="omm-model"' in sh
    assert 'LEGACY_PIPX_ENV="omm"' in sh
    assert "run_pipx install --force --editable" in sh
    assert 'Removing verified legacy pipx environment: $LEGACY_PIPX_ENV' in sh
    assert 'run_pipx uninstall "$LEGACY_PIPX_ENV"' in sh
    assert sh.index("if ! verify_installed_omm_model; then") < sh.index(
        'Removing verified legacy pipx environment: $LEGACY_PIPX_ENV'
    )


def test_installers_distinguish_reinstalled_rollback_from_verified_rollback():
    """Rolling back a legacy pipx environment re-fetches it over the network
    with `pipx reinstall`, which does not go through omm's signature
    verification for a Git-URL install. The recovery message must not claim
    that state is "verified" the same way an in-place `pipx reinstall` of the
    still-present new environment is - see t1-installers-13."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    rollback_fn = sh.split("rollback_failed_new_install() {", 1)[1].split(
        "report_failed_install() {", 1
    )[0]
    legacy_branch = rollback_fn.split('run_pipx reinstall "$LEGACY_PIPX_ENV"', 1)[1]
    first_assignment = legacy_branch.split("ROLLBACK_STATE=", 2)[1].splitlines()[0]
    assert first_assignment.strip() == "reinstalled"
    report_fn = sh.split("report_failed_install() {", 1)[1].split("\n}\n", 1)[0]
    assert "without omm's signature verification" in report_fn

    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    restore_fn = ps1.split("function Restore-PipxAfterFailedInstall {", 1)[1].split(
        "function Write-FailedInstallRecovery {", 1
    )[0]
    legacy_branch_ps1 = restore_fn.split('@("reinstall", $LegacyPipxEnvironment)', 1)[1]
    first_assignment_ps1 = legacy_branch_ps1.split("$rollbackState = ", 2)[1].splitlines()[0]
    assert first_assignment_ps1.strip().strip('"') == "reinstalled"
    recovery_fn = ps1.split("function Write-FailedInstallRecovery {", 1)[1].split(
        "function Remove-UnreferencedNewSource {", 1
    )[0]
    assert "without omm's signature verification" in recovery_fn


def test_uninstallers_check_actual_pipx_environments_before_removal():
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    assert 'Invoke-Pipx list --json' in ps1
    assert 'Test-PipxSnapshotIdentity' in ps1
    assert '$null -ne $metadata.environment' in ps1
    assert 'main.package -cne $Distribution' in ps1
    assert 'main.suffix -cne ""' in ps1
    assert '"omm.cli:main"' in ps1

    assert 'run_pipx list --json' in sh
    assert 'pipx_snapshot_environment_is' in sh
    assert 'metadata.get("environment") in (None, name)' in sh
    assert 'main.get("package") == distribution' in sh
    assert 'main.get("suffix") == ""' in sh
    assert 'entry_points[0].value != "omm.cli:main"' in sh


def test_powershell_pipx_identity_uses_file_identity_for_virtualized_paths():
    for name in ("install.ps1", "uninstall.ps1"):
        script = (ROOT / name).read_text(encoding="utf-8")
        helper = script.split("function Test-SameDirectoryIdentity {", 1)[1].split(
            "function Test-PipxSnapshotIdentity", 1
        )[0]
        identity = script.split("function Test-PipxSnapshotIdentity {", 1)[1].split(
            "$OmmEnvironmentVerifier", 1
        )[0]

        assert "os.path.samefile" in helper
        assert "Test-Path -LiteralPath $leftFull -PathType Container" in helper
        assert "Test-Path -LiteralPath $rightFull -PathType Container" in helper
        assert 'Join-Path $rightFull "python.exe"' in helper
        assert "& $identityPython -c" in helper
        assert "Test-SameDirectoryIdentity $actualDir $expectedDir" in identity
        assert "return $actualDir.Equals($expectedDir" not in identity


def test_installers_reject_ambiguous_legacy_sources_and_verify_exact_app():
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert 'host == "github.com"' in script
        assert 'normalized_path in {' in script
        assert 're.fullmatch(r"[0-9a-fA-F]{40}"' in script
        assert '"remote", "get-url", "origin"' in script
        assert "is_relative_to" not in script
    assert '[ "$PIPX_BIN_DIR/omm" -ef "$PIPX_LOCAL_VENVS/$PIPX_ENV/bin/omm" ]' in sh
    assert '[ "$version_output" = "omm $EXPECTED_VERSION" ]' in sh
    assert 'Get-FileHash -LiteralPath $ommApp -Algorithm SHA256' in ps1
    assert '-ceq "omm $ExpectedVersion"' in ps1


def test_uninstallers_require_managed_custom_home_and_never_delete_the_container_recursively():
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert ".omm-managed" in script
        assert "current directory" in script
    assert 'rm -rf "$OMM_HOME"' not in sh
    assert 'rm -rf "$RESOLVED_HOME"' not in sh
    assert "Remove-Item -LiteralPath $resolvedHome -Recurse" not in ps1


def test_powershell_scripts_reject_a_non_absolute_omm_home_before_resolving_it():
    """[IO.Path]::GetFullPath resolves a relative, "~", drive-relative
    ("C:omm") or root-relative ("\\omm") OMM_HOME against the process's own
    .NET current directory, not the real hub - install.sh/uninstall.sh
    already reject these with a `case "$OMM_HOME" in /*)` guard; install.ps1
    and uninstall.ps1 must reject them before ever calling GetFullPath -
    see t1-installers-15."""
    for name in ("install.ps1", "uninstall.ps1"):
        script = (ROOT / name).read_text(encoding="utf-8")
        assert "Refusing non-absolute OMM_HOME" in script
        assert script.index("Refusing non-absolute OMM_HOME") < script.index(
            "[IO.Path]::GetFullPath($OmmHome)"
        )


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell required",
)
@pytest.mark.parametrize("script_name,end_marker", [
    ("uninstall.ps1", "$linkOwnershipFile = Join-Path"),
    ("install.ps1", "function Test-CommandExists"),
])
def test_windows_scripts_refuse_non_absolute_omm_home(tmp_path, script_name, end_marker):
    script = (ROOT / script_name).read_text(encoding="utf-8")
    end = script.index(end_marker)
    guard_text = script[:end]

    def run(omm_home: str) -> subprocess.CompletedProcess:
        script_path = tmp_path / f"guard-{script_name}"
        script_path.write_text(guard_text, encoding="utf-8")
        profile_dir = tmp_path / "profile"
        profile_dir.mkdir(exist_ok=True)
        env = {**os.environ, "USERPROFILE": str(profile_dir), "OMM_HOME": omm_home}
        return subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
            env=env,
        )

    for bad in ("omm-rel", "~/omm", "C:omm", "\\omm"):
        result = run(bad)
        assert result.returncode != 0, (bad, result.stdout, result.stderr)
        assert "non-absolute" in result.stderr, (bad, result.stderr)

    good = run(str(tmp_path / "hub"))
    assert good.returncode == 0, good.stdout + good.stderr


def test_uninstallers_do_not_rewrite_user_shell_profiles():
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    assert ".bashrc" not in sh
    assert ".zshrc" not in sh
    assert "$PROFILE" not in ps1


def test_installers_create_custom_home_ownership_marker():
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'Join-Path $OmmHome ".omm-managed"' in ps1
    assert '"$OMM_HOME/.omm-managed"' in sh

    # The marker must be written right after the home directory is created,
    # before any rejection guard further down could leave a half-marked
    # home behind on failure - see t1-installers-05.
    assert sh.count('"$OMM_HOME/.omm-managed"') == 1
    assert sh.index('"$OMM_HOME/.omm-managed"') > sh.index('mkdir -p "$SOURCES_DIR"')
    assert sh.index('"$OMM_HOME/.omm-managed"') < sh.index('clone --filter=blob:none')

    assert ps1.count('Join-Path $OmmHome ".omm-managed"') == 1
    assert ps1.index('Join-Path $OmmHome ".omm-managed"') > ps1.index(
        'New-Item -ItemType Directory -Force -Path $SourcesDir'
    )
    assert ps1.index('Join-Path $OmmHome ".omm-managed"') < ps1.index('$CloneArgs = @(')


def test_installers_discard_unreferenced_new_source_before_legacy_removal():
    """A fresh checkout nothing points at (new pipx install failed before it
    was ever exposed) must not be left behind for the uninstaller's "No
    verified OMM pipx environment was removed" guard to trip over."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "discard_unreferenced_new_source() {" in sh
    first_failure = sh.index("if ! pipx_install_with_repair; then")
    second_failure = sh.index("if ! verify_installed_omm_model; then")
    legacy_removal = sh.index("Removing verified legacy pipx environment")
    for start in (first_failure, second_failure):
        block = sh[start:legacy_removal]
        assert block.index("rollback_failed_new_install") < block.index("discard_unreferenced_new_source")
    assert "discard_unreferenced_new_source" not in sh[legacy_removal:]

    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "function Remove-UnreferencedNewSource {" in ps1
    first_failure_ps1 = ps1.index("if (-not (Invoke-PipxInstallWithRepair)) {")
    second_failure_ps1 = ps1.index("if (-not (Test-InstalledOmmModel)) {")
    legacy_removal_ps1 = ps1.index("Removing verified legacy pipx environment")
    for start in (first_failure_ps1, second_failure_ps1):
        block = ps1[start:legacy_removal_ps1]
        assert block.index("Restore-PipxAfterFailedInstall") < block.index("Remove-UnreferencedNewSource")
    assert "Remove-UnreferencedNewSource" not in ps1[legacy_removal_ps1:]


def test_powershell_verifiers_use_stdin_instead_of_multiline_dash_c():
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    uninstaller = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")

    for script in (installer, uninstaller):
        assert "$OmmEnvironmentVerifier | & $environmentPython -" in script
        assert "-c $OmmEnvironmentVerifier" not in script


def test_installer_environment_verifier_tolerates_dropped_empty_trailing_arg():
    """Windows PowerShell 5.1 drops an empty trailing argument when invoking
    a native command (`& python - a b c ""` arrives as argv [a, b, c]), so a
    caller that omits -ExpectedVersion always failed unpacking exactly 4
    names from `sys.argv[1:]`. The verifier must accept either 3 or 4 args."""
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert 'expected_version = args[3] if len(args) == 4 else ""' in script
    assert "distribution, omm_home, require_source, expected_version = sys.argv[1:]" not in script


def test_installers_tolerate_pipx_list_reporting_an_unrelated_broken_venv():
    """pipx's own `list --json` exits 1 (EXIT_CODE_LIST_PROBLEM) whenever any
    venv on the machine is unhealthy - it still prints the full snapshot for
    every other venv first, simply omitting the broken one. Treating that
    exit code as a hard failure refused to install because of a venv omm has
    never heard of; it must instead check the omm/omm-model venvs it cares
    about are not the ones missing from an otherwise-valid snapshot."""
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    body = ps1.split("function Get-PipxSnapshot {", 1)[1].split(
        "function Test-PipxSnapshotEnvironment", 1
    )[0]
    assert "$exitCode -ne 1" in body
    assert "pipx_spec_version" in body

    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "pipx reports the" in sh
    assert '"$pipx_status" -eq 1' in sh


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell required",
)
def test_windows_get_pipx_snapshot_tolerates_exit_1_with_valid_json(tmp_path):
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    start = ps1.index("function Get-PipxSnapshot {")
    end = ps1.index("function Test-PipxSnapshotEnvironment", start)
    get_pipx_snapshot_fn = ps1[start:end]

    def run(stub_body: str, tail: str) -> subprocess.CompletedProcess:
        script_path = tmp_path / "snapshot.ps1"
        script_path.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"function Invoke-Pipx {{\n{stub_body}\n}}\n"
            f"{get_pipx_snapshot_fn}\n"
            f"{tail}\n",
            encoding="utf-8",
        )
        return subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    ok_result = run(
        "    '{\"pipx_spec_version\":\"0.1\",\"venvs\":{}}'\n    cmd /c exit 1",
        "$result = Get-PipxSnapshot\nif ($null -eq $result) { exit 2 }\nexit 0",
    )
    assert ok_result.returncode == 0, ok_result.stdout + ok_result.stderr

    empty_result = run(
        "    ''\n    cmd /c exit 1",
        "Get-PipxSnapshot | Out-Null\nexit 0",
    )
    assert empty_result.returncode != 0


def test_uninstallers_purge_every_owned_omm_home_path():
    """Every literal `OMM_HOME / "name"` path referenced anywhere in
    src/omm must be covered by the purge allowlist in both uninstallers -
    otherwise `--purge`/`-Purge` leaves it behind and the trailing `rmdir`
    (which only succeeds on an empty directory) never fires. `src` is
    handled directly by the uninstaller's own source-checkout removal;
    `apps` (engines omm installed, which the user may still be using) is a
    deliberate exclusion pending a separate decision."""
    excluded = {"src", "apps"}
    names: set[str] = set()
    for path in (ROOT / "src" / "omm").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r'OMM_HOME\s*/\s*"([^"/]+)"', text))
    names -= excluded
    assert names  # sanity: the scan actually found real names to check

    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    sh_body = sh.split("purge_owned_data() {", 1)[1].split("\n}\n", 1)[0]
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    # PowerShell closes every block (foreach/if, not just the function) with
    # a lone "}", so split up to the next top-level statement instead of the
    # first "\n}\n" - matching the "split up to the next known marker"
    # convention the rest of this file's PS1 extraction tests already use.
    ps1_body = ps1.split("function Remove-OmmOwnedData {", 1)[1].split(
        "if ($null -eq $PipxCommand)", 1
    )[0]

    missing_sh = sorted(name for name in names if name not in sh_body)
    missing_ps1 = sorted(name for name in names if name not in ps1_body)
    assert missing_sh == []
    assert missing_ps1 == []


def test_uninstallers_check_link_ownership_before_any_purge_mutation():
    """--purge/-Purge deletes models/ directly without running linker.py's
    unlink logic. A non-empty link-ownership.json means omm still has
    models linked into local engines, and the hub must not be deleted out
    from under those links - so the check must run before any pipx
    mutation or file deletion, not merely before purge_owned_data itself."""
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "link-ownership.json" in sh
    assert "omm uninstall all" in sh
    link_check_index = sh.index('[ "$PURGE" = "1" ] && [ -f "$RESOLVED_HOME/link-ownership.json" ]')
    assert link_check_index < sh.index("PIPX_AVAILABLE=0")
    assert link_check_index < sh.index("run_pipx(")
    assert link_check_index < sh.index("rm -rf")

    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    assert "link-ownership.json" in ps1
    assert "omm uninstall all" in ps1
    link_check_index_ps1 = ps1.index('$linkOwnershipFile = Join-Path $resolvedHome "link-ownership.json"')
    assert link_check_index_ps1 < ps1.index("$PipxCommand = $null")
    assert link_check_index_ps1 < ps1.index("Remove-Item")


def test_uninstaller_accepts_pipx_exe_app_name_like_installer():
    """pipx on Windows records apps as launcher filenames ("omm.exe").
    install.ps1 accepts both spellings; uninstall.ps1 must too, or a real
    Windows uninstall fails its identity verification every time."""
    install = (ROOT / "install.ps1").read_text(encoding="utf-8")
    uninstall = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")

    accepted = '$_ -in @("omm", "omm.exe")'
    assert accepted in install
    assert accepted in uninstall
    assert '$_ -ceq "omm" }' not in uninstall


def test_uninstall_sh_redirects_to_powershell_uninstaller_on_windows():
    """uninstall.sh's pipx-identity verifier hardcodes a POSIX bin/ layout
    and a bare "python" parser lookup; run under Git Bash/MSYS on Windows it
    can only ever fail closed, masking the real problem (wrong uninstaller).
    install.sh already has this guard; uninstall.sh needs the same one,
    checked before any pipx probing or deletion - see t1-installers-19."""
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "MINGW*|MSYS*|CYGWIN*)" in sh
    assert "uninstall.ps1 | iex" in sh
    guard_index = sh.index("MINGW*|MSYS*|CYGWIN*)")
    for marker in ("PIPX_AVAILABLE=0", "run_pipx", "rm -rf"):
        assert guard_index < sh.index(marker)


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh required")
def test_uninstall_sh_is_syntactically_valid():
    result = subprocess.run(["sh", "-n", str(ROOT / "uninstall.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh required")
def test_install_sh_is_syntactically_valid():
    result = subprocess.run(["sh", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_uninstallers_explain_that_omm_could_be_an_unrelated_or_stale_install():
    """The old wording ("Refusing to replace unrelated pipx environment
    'omm'" / "environment-name conflict") implied the 'omm' pipx environment
    must belong to someone else's unrelated package - but the verifier also
    fails closed for a *stale OMM install* whose source checkout was deleted
    or whose OMM_HOME moved, which is common and not actually a conflict
    with another package. See t1-installers-20."""
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    for script in (sh, ps1):
        assert "environment-name conflict" not in script
        assert "source checkout was deleted" in script

    install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for script in (install_sh, install_ps1):
        assert "If it is your old OMM install" in script


def test_install_ps1_no_longer_supports_an_install_time_branch_override():
    """beta installs are now a post-install step (`omm setting version
    --beta`) instead of an install-time env var that only install.ps1
    understood (install.sh never had a matching -b/branch code path) -
    t1-installers-01/11, user decision A."""
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "OMM_INSTALL_BRANCH" not in ps1
    assert "OMM_INSTALL_BRANCH" not in sh
    assert "omm setting version --beta" in ps1


def test_install_and_uninstall_ps1_do_not_close_the_iex_session_on_failure():
    """`irm ... | iex` runs the script's text inside the caller's own
    PowerShell session; a bare `exit` in that mode closes the whole session
    window along with any recovery message just printed. Every `exit 1` must
    route through a helper that only hard-exits when the script is actually
    running as a file ($PSCommandPath set) - t1-installers-04/16, decision A."""
    for name, expected_message in (
        ("install.ps1", "omm installer failed"),
        ("uninstall.ps1", "omm uninstaller failed"),
    ):
        script = (ROOT / name).read_text(encoding="utf-8")
        standalone_exits = re.findall(r"^\s*exit 1\s*$", script, re.M)
        assert standalone_exits == []
        assert script.count("{ exit 1 }") == 1
        assert "if ($OmmRunAsFile) { exit 1 } else { throw" in script
        assert expected_message in script
        assert script.index("$OmmRunAsFile = [bool]$PSCommandPath") < script.index(
            "function Exit-OmmFailure"
        )
        # every remaining failure exit routes through the helper
        assert script.count("Exit-OmmFailure") >= 2
