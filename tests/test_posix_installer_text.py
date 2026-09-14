import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_unix_installer_persists_pipx_path_for_macos_login_zsh():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "ensure_pipx_bin_path()" in script
    assert "Darwin)" in script
    assert 'zprofile="$HOME/.zprofile"' in script
    assert 'export PATH="%s:$PATH"' in script
    assert 'run_pipx ensurepath >/dev/null 2>&1 || true' in script


def test_unix_installer_updates_current_process_path():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'PATH="$PIPX_BIN_DIR:$PATH"; export PATH' in script
    assert "ensure_pipx_bin_path" in script


def test_unix_installer_bootstraps_macos_and_common_linux_package_managers():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "OMM_AUTO_INSTALL_HOMEBREW" in script
    assert "raw.githubusercontent.com/Homebrew/install/HEAD/install.sh" in script
    assert "NONINTERACTIVE=1 /bin/bash -c" in script
    assert '"$BREW" install python' in script
    assert '"$BREW" install git' in script
    for manager in ("apt-get", "dnf", "yum", "pacman", "apk"):
        assert manager in script
    assert "python3-venv" in script
    assert "python3-pip" in script


def test_unix_installer_bootstraps_ssh_keygen_for_signature_verification():
    """Debian's git only Recommends ssh-client, and apt-get above runs with
    --no-install-recommends; apk/pacman git do not depend on openssh at all.
    Without ssh-keygen, signature verification fails with a message that
    never mentions the real cause - the installer must bootstrap it and
    fail closed with a clear message if it still can't find it."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "command -v ssh-keygen" in script
    assert "openssh-client ;;" in script
    assert "openssh-clients ;;" in script
    assert "openssh ;;" in script
    assert "openssh-keygen ;;" in script
    assert script.index("ssh-keygen not found after dependency bootstrap") < script.index(
        "git clone --filter=blob:none"
    )


def _extract_function(script: str, marker: str) -> str:
    start = script.index(marker)
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_unix_installer_resolves_omm_home_prefix_before_using_it(tmp_path):
    """OMM_HOME=$HOME/. or $HOME// or a symlink to $HOME must not slip past
    the unsafe-OMM_HOME guard just because the raw string comparison never
    saw the normalized form. resolve_existing_prefix collapses '.', '//',
    and symlinks in the existing leading portion of the path, even when the
    path itself (or a tail of it) does not exist yet."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    function_text = _extract_function(script, "resolve_existing_prefix() {")

    script_path = tmp_path / "f.sh"
    script_path.write_text(function_text + "\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    resolved_home = str(home.resolve())
    link = tmp_path / "link"
    link.symlink_to(home, target_is_directory=True)

    def run(path_arg):
        result = subprocess.run(
            ["sh", "-c", '. "$1"; resolve_existing_prefix "$2"', "_", str(script_path), path_arg],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(home)},
            timeout=10,
        )
        return result

    assert run(f"{home}/.").stdout.strip() == resolved_home
    assert run(f"{home}//").stdout.strip() == resolved_home
    assert run(str(link)).stdout.strip() == resolved_home
    assert run(f"{home}/new/omm").stdout.strip() == f"{resolved_home}/new/omm"
    assert run("/nonexist/../x").returncode != 0

    unsafe_case_index = script.index('"$RESOLVED_HOME_DIR") echo "Refusing unsafe OMM_HOME')
    assert unsafe_case_index < script.index('mkdir -p "$SOURCES_DIR"')


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_unix_installer_accepts_pipx_list_exit_1_only_with_a_valid_snapshot(tmp_path):
    """pipx's own `list --json` exits 1 (EXIT_CODE_LIST_PROBLEM) whenever any
    venv on the machine is unhealthy - it still prints the full snapshot for
    every other venv first, simply omitting the broken one. refresh_pipx_snapshot
    must accept that exit code only when stdout actually parsed as a valid
    snapshot; any other nonzero exit, or unparsable/empty stdout, must still
    fail closed."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    function_text = _extract_function(script, "refresh_pipx_snapshot() {")
    script_path = tmp_path / "refresh.sh"
    script_path.write_text(function_text + "\n", encoding="utf-8")

    def run(stub_out: str, stub_rc: int) -> int:
        harness = f"""
PY={shlex.quote(sys.executable)}
STUB_OUT={shlex.quote(stub_out)}
STUB_RC={stub_rc}
run_pipx() {{ printf '%s' "$STUB_OUT"; return "$STUB_RC"; }}
. {shlex.quote(str(script_path))}
refresh_pipx_snapshot
"""
        result = subprocess.run(["sh", "-c", harness], capture_output=True, text=True, timeout=10)
        return result.returncode

    valid_json = '{"pipx_spec_version": "1.0", "venvs": {}}'
    assert run(valid_json, 1) == 0
    assert run("", 1) != 0
    assert run(valid_json, 2) != 0
    assert run("{}", 1) != 0
