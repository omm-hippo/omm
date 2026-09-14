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
        "clone --filter=blob:none"
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_signing_commit_falls_through_when_rev_list_itself_fails(tmp_path):
    """Mirrors trust._signing_commit's fail-closed behavior: if `git rev-list
    --parents` itself errors (e.g. a corrupted object store right after
    clone), signing_commit must not let `set -e` kill the whole script via
    the `parents=$(...)` assignment - it must fall through to returning the
    commit as-is, so the caller's own verify_commit_signature + cleanup
    still runs."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    function_text = _extract_function(script, "signing_commit() {")
    script_path = tmp_path / "signing_commit.sh"
    script_path.write_text(function_text + "\n", encoding="utf-8")

    def run(git_stub: str) -> str:
        harness = f"""
set -eu
git() {{ {git_stub}; }}
. {shlex.quote(str(script_path))}
r=$(signing_commit abc /nonexistent)
echo "R=$r"
"""
        result = subprocess.run(["sh", "-c", harness], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    assert run("return 128") == "R=abc"
    assert run("echo 'c p1 p2'") == "R=p2"
    assert run("echo 'c p1'") == "R=abc"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_discard_unreferenced_new_source_only_when_nothing_points_at_it(tmp_path):
    """A fresh checkout nothing points at (new pipx install failed before it
    was ever exposed, and no legacy checkout was displaced) would otherwise
    survive a failed install and block the uninstaller's "No verified OMM
    pipx environment was removed" guard on a later, unrelated run."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    function_text = _extract_function(script, "discard_unreferenced_new_source() {")
    script_path = tmp_path / "discard.sh"
    script_path.write_text(function_text + "\n", encoding="utf-8")

    def run(*, new_present: str, previous_src_dir: str, has_env: bool) -> bool:
        src_dir = tmp_path / f"src-{new_present}-{previous_src_dir or 'none'}-{has_env}"
        src_dir.mkdir()
        harness = f"""
set -eu
NEW_PIPX_PRESENT={shlex.quote(new_present)}
PREVIOUS_SRC_DIR={shlex.quote(previous_src_dir)}
PIPX_ENV=omm-model
SRC_DIR={shlex.quote(str(src_dir))}
refresh_pipx_snapshot() {{ return 0; }}
pipx_snapshot_has_environment() {{ [ {"1" if has_env else "0"} = 1 ]; }}
. {shlex.quote(str(script_path))}
discard_unreferenced_new_source
"""
        result = subprocess.run(["sh", "-c", harness], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        return not src_dir.exists()

    assert run(new_present="0", previous_src_dir="", has_env=False) is True
    assert run(new_present="1", previous_src_dir="", has_env=False) is False
    assert run(new_present="0", previous_src_dir="/some/prior", has_env=False) is False
    assert run(new_present="0", previous_src_dir="", has_env=True) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_ensure_pipx_bin_path_escapes_shell_metacharacters_and_stays_idempotent(tmp_path):
    """A PIPX_BIN_DIR containing `$` or a backtick must not be interpolated
    when the profile is later sourced, and the idempotency check (grep) must
    look for the same escaped text that was actually written - otherwise a
    path with those characters gets a duplicate PATH line appended on every
    rerun."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    function_text = _extract_function(script, "ensure_pipx_bin_path() {")
    script_path = tmp_path / "ensure_pipx_bin_path.sh"
    script_path.write_text(function_text + "\n", encoding="utf-8")

    def run(home: Path, pipx_bin_dir: Path, uname_output: str, rcfile_name: str, *, runs: int = 2):
        harness = f"""
set -eu
uname() {{ echo {shlex.quote(uname_output)}; }}
SHELL=/bin/bash
HOME={shlex.quote(str(home))}
PIPX_BIN_DIR={shlex.quote(str(pipx_bin_dir))}
. {shlex.quote(str(script_path))}
for i in $(seq 1 {runs}); do ensure_pipx_bin_path; done
"""
        result = subprocess.run(
            ["sh", "-c", harness],
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": os.environ["PATH"]},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        rcfile = home / rcfile_name
        content = rcfile.read_text(encoding="utf-8")
        assert content.count("# Added by omm installer") == 1
        sourced = subprocess.run(
            ["sh", "-c", f'. {shlex.quote(str(rcfile))}; printf %s "$PATH"'],
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert sourced.returncode == 0, sourced.stderr
        assert sourced.stdout.startswith(str(pipx_bin_dir) + ":")
        assert list(home.iterdir()) == [rcfile]

    home1 = tmp_path / "home-linux"
    home1.mkdir()
    run(home1, home1 / 'a$b`c"d\\e' / "bin", "Linux", ".bashrc")

    home2 = tmp_path / "home-darwin"
    home2.mkdir()
    run(home2, home2 / 'a$b`c"d\\e' / "bin", "Darwin", ".zprofile")

    home3 = tmp_path / "home-backslash"
    home3.mkdir()
    run(home3, home3 / "x\\y" / "bin", "Linux", ".bashrc")
