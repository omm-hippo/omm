import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _managed_home(tmp_path: Path) -> tuple[Path, Path]:
    managed = tmp_path / "custom-omm-home"
    source = managed / "sources" / ("a" * 40)
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", "https://github.com/omm-hippo/omm.git"],
        check=True,
    )
    (managed / "models").mkdir()
    (managed / ".omm-managed").write_text("omm installer managed home v1\n")
    (managed / "config.json").write_text("{}\n")
    sentinel = managed / "keep-me.txt"
    sentinel.write_text("user-owned\n")
    return managed, sentinel


def _environment_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _create_pipx_environment(
    local_venvs: Path,
    name: str,
    managed: Path,
    *,
    entry_point: str = "omm.cli:main",
    legacy_source: Path | None = None,
    metadata_version: str = "0.12",
) -> dict:
    environment = local_venvs / name
    venv.EnvBuilder(with_pip=False).create(environment)
    python = _environment_python(environment)
    purelib = Path(
        subprocess.run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    distribution = name
    dist_info = purelib / f"{distribution.replace('-', '_')}-0.2.119.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 0.2.119\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        f"[console_scripts]\nomm = {entry_point}\n", encoding="utf-8"
    )
    if legacy_source is not None:
        (dist_info / "direct_url.json").write_text(
            json.dumps({"url": legacy_source.resolve().as_uri(), "dir_info": {"editable": True}}),
            encoding="utf-8",
        )

    app_dir = environment / ("Scripts" if sys.platform == "win32" else "bin")
    app = app_dir / ("omm.exe" if sys.platform == "win32" else "omm")
    if sys.platform == "win32":
        app.write_bytes(b"fake internal omm app\n")
    else:
        app.write_text("#!/bin/sh\necho 'omm 0.2.119'\n", encoding="utf-8")
        app.chmod(0o755)
    metadata = {
        "pipx_metadata_version": metadata_version,
        "main_package": {
                "package": distribution,
                "suffix": "",
                "apps": ["omm"],
                "app_paths": [{"__Path__": str(app), "__type__": "Path"}],
        }
    }
    if metadata_version != "0.5":
        metadata["environment"] = name
    return {"metadata": metadata}


def _setup_fake_pipx(
    tmp_path: Path,
    managed: Path,
    installed: list[str],
    *,
    legacy_source: Path | None = None,
    legacy_entry_point: str = "omm.cli:main",
    metadata_version: str = "0.12",
) -> tuple[dict[str, str], Path, Path]:
    local_venvs = tmp_path / "pipx-venvs"
    local_venvs.mkdir()
    canonical_source = next((managed / "sources").iterdir())
    snapshot = {"pipx_spec_version": "0.1", "venvs": {}}
    for name in installed:
        snapshot["venvs"][name] = _create_pipx_environment(
            local_venvs,
            name,
            managed,
            entry_point=legacy_entry_point if name == "omm" else "omm.cli:main",
            legacy_source=(legacy_source or canonical_source) if name == "omm" else None,
            metadata_version=metadata_version,
        )
    state = tmp_path / "pipx-state.json"
    state.write_text(json.dumps(snapshot), encoding="utf-8")
    log = tmp_path / "pipx-uninstalls.log"
    driver = tmp_path / "pipx_driver.py"
    driver.write_text(
        """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] != ["-m", "pipx"]:
    raise SystemExit(2)
args = args[2:]
if args == ["--version"]:
    print("1.16.7")
    raise SystemExit(0)
if args[:2] == ["environment", "--value"]:
    if args[2] == "PIPX_LOCAL_VENVS":
        print(os.environ["PIPX_TEST_LOCAL_VENVS"])
        raise SystemExit(0)
    if args[2] == "PIPX_BIN_DIR":
        print(os.environ["PIPX_TEST_BIN_DIR"])
        raise SystemExit(0)
    raise SystemExit(2)
if args[:2] == ["list", "--json"]:
    if os.environ.get("PIPX_FAIL_LIST") == "1":
        raise SystemExit(23)
    print(Path(os.environ["PIPX_TEST_STATE"]).read_text(encoding="utf-8"))
    raise SystemExit(0)
if args and args[0] == "uninstall":
    name = args[1]
    if os.environ.get("PIPX_FAIL_UNINSTALL") == name:
        for variable in ("PIPX_TEST_EXPOSED", "PIPX_TEST_EXPOSED_CMD"):
            exposed = Path(os.environ[variable]) if os.environ.get(variable) else None
            if exposed and (exposed.exists() or exposed.is_symlink()):
                exposed.unlink()
        raise SystemExit(24)
    log = Path(os.environ["PIPX_TEST_LOG"])
    with log.open("a", encoding="utf-8") as handle:
        handle.write(name + "\\n")
    state_path = Path(os.environ["PIPX_TEST_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.get("venvs", {}).pop(name, None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    raise SystemExit(0)
if args and args[0] == "reinstall":
    name = args[1]
    local = Path(os.environ["PIPX_TEST_LOCAL_VENVS"])
    if os.name == "nt":
        import shutil
        shutil.copyfile(local / name / "Scripts" / "omm.exe", Path(os.environ["PIPX_TEST_EXPOSED"]))
        Path(os.environ["PIPX_TEST_EXPOSED_CMD"]).write_text("@echo omm 0.2.119\\r\\n", encoding="utf-8")
    else:
        exposed = Path(os.environ["PIPX_TEST_EXPOSED"])
        if exposed.exists() or exposed.is_symlink():
            exposed.unlink()
        exposed.symlink_to(local / name / "bin" / "omm")
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )

    stub = tmp_path / "bin"
    stub.mkdir()
    if sys.platform == "win32":
        (stub / "python.cmd").write_text(
            '@"%REAL_PYTHON%" "%PIPX_TEST_DRIVER%" %*\r\n', encoding="utf-8"
        )
        exposed = stub / "omm.exe"
        exposed_cmd = stub / "omm.cmd"
        preferred = "omm-model" if "omm-model" in installed else "omm"
        exposed.write_bytes((local_venvs / preferred / "Scripts" / "omm.exe").read_bytes())
        exposed_cmd.write_text("@echo omm 0.2.119\r\n", encoding="utf-8")
    else:
        wrapper = stub / "python3"
        wrapper.write_text(
            """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "pipx" ]; then
    exec "$REAL_PYTHON" "$PIPX_TEST_DRIVER" "$@"
fi
exec "$REAL_PYTHON" "$@"
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        exposed = stub / "omm"
        exposed_cmd = None
        preferred = "omm-model" if "omm-model" in installed else "omm"
        exposed.symlink_to(local_venvs / preferred / "bin" / "omm")

    env = os.environ.copy()
    env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    env["OMM_HOME"] = str(managed)
    env["REAL_PYTHON"] = sys.executable
    env["PIPX_TEST_DRIVER"] = str(driver)
    env["PIPX_TEST_LOCAL_VENVS"] = str(local_venvs)
    env["PIPX_TEST_STATE"] = str(state)
    env["PIPX_TEST_LOG"] = str(log)
    env["PIPX_TEST_BIN_DIR"] = str(stub)
    env["PIPX_TEST_EXPOSED"] = str(exposed)
    if exposed_cmd is not None:
        env["PIPX_TEST_EXPOSED_CMD"] = str(exposed_cmd)
    return env, log, stub


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell smoke test")
def test_powershell_purge_preserves_unknown_files_and_refuses_cwd(tmp_path):
    managed, sentinel = _managed_home(tmp_path)
    env, _, _ = _setup_fake_pipx(tmp_path, managed, ["omm-model"])

    command = f"& '{ROOT / 'uninstall.ps1'}' -Purge; exit $LASTEXITCODE"
    result = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", command,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr or result.stdout

    assert sentinel.read_text() == "user-owned\n"
    assert not (managed / "models").exists()
    assert not (managed / "sources").exists()
    assert not (managed / "config.json").exists()
    assert not (managed / ".omm-managed").exists()

    unsafe_env = {**env, "OMM_HOME": str(ROOT)}
    refused = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "uninstall.ps1"),
        ],
        cwd=ROOT,
        env=unsafe_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert refused.returncode != 0


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell smoke test")
def test_powershell_uninstaller_removes_only_installed_pipx_environments(tmp_path):
    managed, _ = _managed_home(tmp_path)
    env, log, _ = _setup_fake_pipx(tmp_path, managed, ["omm-model"])

    result = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "uninstall.ps1"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert log.read_text().splitlines() == ["omm-model"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell smoke test")
def test_powershell_uninstall_failure_preserves_sources_and_command(tmp_path):
    managed, _ = _managed_home(tmp_path)
    env, _, stub = _setup_fake_pipx(tmp_path, managed, ["omm-model"])
    env["PIPX_FAIL_UNINSTALL"] = "omm-model"

    result = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "uninstall.ps1"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert (managed / "sources").is_dir()
    command = subprocess.run([str(stub / "omm.cmd"), "--version"], env=env, capture_output=True, text=True)
    assert command.returncode == 0
    assert command.stdout.strip() == "omm 0.2.119"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell smoke test")
def test_posix_purge_preserves_unknown_files_and_shell_profiles(tmp_path):
    managed, sentinel = _managed_home(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bashrc = home / ".bashrc"
    bashrc.write_text('export PATH="$HOME/.local/bin:$PATH"\n')
    env, _, _ = _setup_fake_pipx(tmp_path, managed, ["omm-model"])
    env["HOME"] = str(home)

    subprocess.run(
        ["sh", str(ROOT / "uninstall.sh"), "--purge"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert sentinel.read_text() == "user-owned\n"
    assert bashrc.read_text() == 'export PATH="$HOME/.local/bin:$PATH"\n'
    assert not (managed / "models").exists()
    assert not (managed / "sources").exists()
    assert not (managed / "config.json").exists()
    assert not (managed / ".omm-managed").exists()

    unsafe_env = {**env, "OMM_HOME": str(ROOT)}
    refused = subprocess.run(
        ["sh", str(ROOT / "uninstall.sh")],
        cwd=ROOT,
        env=unsafe_env,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell smoke test")
def test_posix_data_only_purge_succeeds_without_pipx(tmp_path):
    managed = tmp_path / "custom-omm-home"
    (managed / "models").mkdir(parents=True)
    (managed / ".omm-managed").write_text("omm installer managed home v1\n")
    (managed / "config.json").write_text("{}\n")
    sentinel = managed / "keep-me.txt"
    sentinel.write_text("user-owned\n")
    stub = tmp_path / "bin"
    stub.mkdir()
    for name in ("python3", "python", "pipx"):
        executable = stub / name
        executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(ROOT / "uninstall.sh"), "--purge"],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}", "OMM_HOME": str(managed)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert not (managed / "models").exists()
    assert not (managed / "config.json").exists()
    assert not (managed / ".omm-managed").exists()
    assert sentinel.read_text() == "user-owned\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell smoke test")
@pytest.mark.parametrize(
    ("installed", "expected_uninstalls", "metadata_version"),
    [
        (["omm"], ["omm"], "0.12"),
        (["omm-model"], ["omm-model"], "0.12"),
        (["omm-model"], ["omm-model"], "0.5"),
        (["omm", "omm-model"], ["omm", "omm-model"], "0.12"),
    ],
)
def test_posix_uninstaller_removes_only_installed_pipx_environments(
    tmp_path, installed, expected_uninstalls, metadata_version
):
    managed, _ = _managed_home(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env, log, _ = _setup_fake_pipx(
        tmp_path, managed, installed, metadata_version=metadata_version
    )
    env["HOME"] = str(home)

    result = subprocess.run(
        ["sh", str(ROOT / "uninstall.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    actual = log.read_text().splitlines() if log.exists() else []
    assert actual == expected_uninstalls
    assert not (managed / "sources").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell smoke test")
@pytest.mark.parametrize(
    ("failure", "installed", "failed_environment"),
    [
        ("missing", ["omm-model"], None),
        ("list", ["omm-model"], None),
        ("uninstall", ["omm-model"], "omm-model"),
        ("uninstall", ["omm", "omm-model"], "omm"),
        ("uninstall", ["omm", "omm-model"], "omm-model"),
    ],
)
def test_posix_uninstall_failure_preserves_sources_and_runnable_command(
    tmp_path, failure, installed, failed_environment
):
    managed, _ = _managed_home(tmp_path)
    env, _, stub = _setup_fake_pipx(tmp_path, managed, installed)
    if failure == "missing":
        for name in ("python3", "python", "pipx"):
            executable = stub / name
            executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            executable.chmod(0o755)
    elif failure == "list":
        env["PIPX_FAIL_LIST"] = "1"
    else:
        env["PIPX_FAIL_UNINSTALL"] = failed_environment

    result = subprocess.run(
        ["sh", str(ROOT / "uninstall.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Source checkouts and user data were preserved" in result.stderr
    assert "pipx uninstall omm-model" in result.stderr
    assert (managed / "sources").is_dir()
    command = subprocess.run(["omm", "--version"], env=env, capture_output=True, text=True)
    assert command.returncode == 0
    assert command.stdout.strip() == "omm 0.2.119"
    if failure == "uninstall":
        assert "command was repaired and verified" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell smoke test")
@pytest.mark.parametrize("installed", [["omm"], ["omm", "omm-model"]])
def test_posix_uninstaller_preserves_unrelated_omm_environment(tmp_path, installed):
    managed, _ = _managed_home(tmp_path)
    env, log, _ = _setup_fake_pipx(
        tmp_path, managed, installed, legacy_entry_point="other.cli:main"
    )

    result = subprocess.run(
        ["sh", str(ROOT / "uninstall.sh")], cwd=ROOT, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert not log.exists()
    assert "Preserving unrelated pipx environment 'omm'" in result.stderr
    assert "environment-name conflict" in result.stderr
    assert (managed / "sources").is_dir()
    command = subprocess.run(["omm", "--version"], env=env, capture_output=True, text=True)
    assert command.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell smoke test")
@pytest.mark.parametrize("bad_source_kind", ["evil-origin", "nested-source"])
def test_posix_uninstaller_rejects_ambiguous_legacy_source(tmp_path, bad_source_kind):
    managed, _ = _managed_home(tmp_path)
    if bad_source_kind == "nested-source":
        bad_source = managed / "sources" / ("b" * 40) / "nested"
        origin = "https://github.com/omm-hippo/omm.git"
    else:
        bad_source = managed / "sources" / ("b" * 40)
        origin = "https://github.com/evil/omm-hippo/omm.git"
    bad_source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(bad_source)], check=True)
    subprocess.run(["git", "-C", str(bad_source), "remote", "add", "origin", origin], check=True)
    env, log, _ = _setup_fake_pipx(tmp_path, managed, ["omm"], legacy_source=bad_source)

    result = subprocess.run(
        ["sh", str(ROOT / "uninstall.sh")], cwd=ROOT, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert not log.exists()
    assert (managed / "sources").is_dir()
