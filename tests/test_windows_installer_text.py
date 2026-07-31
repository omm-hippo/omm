from pathlib import Path
import re
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
    assert readme.index(command) < readme.index("irm https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1")
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
    assert '--python $PythonExecutable $InstallSpec' in ps1
    assert '--source winget' in ps1
    assert 'checkout-' in ps1 and '$SourcesDir' in ps1

    assert '"$PY" -m pipx "$@"' in sh
    assert '--python "$PY" "$INSTALL_SPEC"' in sh
    assert 'checkout.$$' in sh and '$SOURCES_DIR' in sh
    assert '.bashrc' not in sh and '.zshrc' not in sh


def test_uninstallers_exist_and_preserve_models_without_purge():
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    assert "pipx uninstall omm" in ps1
    assert "pipx uninstall omm" in sh
    assert "if ($Purge)" in ps1
    assert 'if [ "$PURGE" = "1" ]' in sh


def test_uninstallers_require_managed_custom_home_and_never_delete_the_container_recursively():
    ps1 = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert ".omm-managed" in script
        assert "current directory" in script
    assert 'rm -rf "$OMM_HOME"' not in sh
    assert 'rm -rf "$RESOLVED_HOME"' not in sh
    assert "Remove-Item -LiteralPath $resolvedHome -Recurse" not in ps1


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
