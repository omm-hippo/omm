from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


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
