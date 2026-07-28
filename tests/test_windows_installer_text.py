from pathlib import Path


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
