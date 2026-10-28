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


def _python_discovery_functions(script: str) -> str:
    return "".join(
        _extract_function(script, f"{name}() {{")
        for name in (
            "find_real_command",
            "list_versioned_pythons",
            "python_candidates",
            "python_is_supported",
            "first_supported_python",
            "find_supported_python",
            "describe_python_candidates",
        )
    )


def _exit_guard(script: str) -> str:
    """The EXIT-trap block at the top of install.sh, as written there."""
    start = script.index('INSTALL_STEP="starting"\n')
    marker = "trap installer_exit_guard EXIT\n"
    return script[start : script.index(marker) + len(marker)]


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

    cases = iter(range(100))

    def run(*, new_present: str, previous_src_dir: str, has_env: bool) -> bool:
        # A plain counter, not the arguments: previous_src_dir is a path, and
        # its slashes would turn the name into subdirectories that do not exist.
        src_dir = tmp_path / f"src-{next(cases)}"
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


def test_installers_final_message_points_to_local_web_manager_and_launchers():
    """#423: after install, the closing guidance names the browser manager
    and the click-launcher command, not only CLI next steps."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")

    sh_tail = sh[sh.rindex('echo "Done.'):]
    assert "omm web --open" in sh_tail
    assert 'omm web --create-launchers \\"$HOME/Desktop\\"' in sh_tail
    assert "Open-OMM.command" in sh_tail

    ps1_tail = ps1[ps1.rindex('Write-Host "Done.'):]
    assert "omm web --open" in ps1_tail
    # Single-quoted so PowerShell prints $HOME literally for the user to paste.
    assert r"""omm web --create-launchers "$HOME\Desktop"   (then""" in ps1_tail
    assert "Write-Host 'Click launchers:" in ps1_tail
    assert "Open-OMM.cmd" in ps1_tail


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_macos_clt_stub_detection(tmp_path):
    """On a Mac without the Command Line Tools, /usr/bin/python3 and
    /usr/bin/git are xcode-select stubs that pop an install dialog when run.
    is_macos_clt_stub must flag them from xcode-select alone, without
    executing them, and must never flag anything off macOS or outside
    /usr/bin."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    script_path = tmp_path / "stub.sh"
    script_path.write_text(_extract_function(script, "is_macos_clt_stub() {") + "\n", encoding="utf-8")
    developer_dir = tmp_path / "CommandLineTools"
    (developer_dir / "usr" / "bin").mkdir(parents=True)
    real_git = developer_dir / "usr" / "bin" / "git"
    real_git.write_text("#!/bin/sh\n", encoding="utf-8")
    real_git.chmod(0o755)

    # POSIX sh rejects a function named xcode-select, so stub it on PATH.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_xcode_select = fake_bin / "xcode-select"

    def is_stub(os_name: str, xcode_select: str, path: str) -> bool:
        fake_xcode_select.write_text(f"#!/bin/sh\n{xcode_select}\n", encoding="utf-8")
        fake_xcode_select.chmod(0o755)
        harness = f"""
PATH={shlex.quote(str(fake_bin))}:$PATH
uname() {{ echo {shlex.quote(os_name)}; }}
. {shlex.quote(str(script_path))}
is_macos_clt_stub {shlex.quote(path)}
"""
        result = subprocess.run(["sh", "-c", harness], capture_output=True, text=True, timeout=10)
        return result.returncode == 0

    no_clt = "exit 2"
    with_clt = f"echo {shlex.quote(str(developer_dir))}"
    assert is_stub("Darwin", no_clt, "/usr/bin/git")
    assert is_stub("Darwin", no_clt, "/usr/bin/python3")
    assert not is_stub("Darwin", with_clt, "/usr/bin/git")
    assert is_stub("Darwin", with_clt, "/usr/bin/python3")
    assert not is_stub("Darwin", no_clt, "/opt/homebrew/bin/git")
    assert not is_stub("Linux", no_clt, "/usr/bin/git")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_find_supported_python_never_runs_a_clt_stub(tmp_path):
    """The Python version probe is what used to pop the developer-tools
    dialog on a fresh Mac. A stubbed python3 must be skipped unexecuted."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    script_path = tmp_path / "find.sh"
    script_path.write_text(_python_discovery_functions(script), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ran"
    for name in ("python3", "python", "python3.12"):
        fake = bin_dir / name
        fake.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)

    def run(stub_rc: int) -> subprocess.CompletedProcess:
        harness = f"""
PATH={shlex.quote(str(bin_dir))}:/usr/bin:/bin
BREW_BIN_DIRS={shlex.quote(str(tmp_path / "no-brew"))}
is_macos_clt_stub() {{ return {stub_rc}; }}
. {shlex.quote(str(script_path))}
find_supported_python
"""
        return subprocess.run(["sh", "-c", harness], capture_output=True, text=True, timeout=10)

    assert run(0).returncode != 0
    assert not marker.exists()
    found = run(1)
    assert found.returncode == 0
    assert found.stdout.strip() == str(bin_dir / "python3")
    assert marker.exists()


def test_unix_installer_is_parsed_in_full_before_it_runs():
    """`curl | sh` feeds the script through stdin, and `brew install python`
    on a fresh Mac reads stdin - swallowing the rest of the script so sh
    exits 0 before omm is installed. One brace group around everything after
    `set -eu` makes sh parse the whole file before running any of it."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    lines = script.splitlines()

    assert lines[lines.index("set -eu") + 1 :].count("{") == 1
    assert script.index("set -eu") < script.index("\n{\n") < script.index("REPO_URL=")
    assert script.endswith("\n}\n")


def _run_piped(script_text: str, shell: str = "sh") -> subprocess.CompletedProcess:
    """Feed a script to sh on stdin, the way `curl ... | sh` does."""
    return subprocess.run([shell], input=script_text, capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh under test")
def test_installer_never_exits_zero_before_omm_is_installed():
    """2026-10-27, a Mac mini over ssh: `curl -fsSL https://omm.run/install.sh
    | sh` printed Homebrew's errors and then nothing, and omm was not
    installed. A command that fails without printing anything of its own
    makes `set -e` stop the script silently, and sh running out of script
    early (a child reading the piped script from stdin) ends it with status
    0. The EXIT trap must turn every early stop into a non-zero exit that
    names the step, and stay silent once omm is installed."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    guard = _exit_guard(script)

    def run(body: str, shell: str = "sh") -> subprocess.CompletedProcess:
        return _run_piped(f"set -eu\n{{\n{guard}INSTALL_STEP=\"installing Python with Homebrew\"\n{body}\n}}\n", shell)

    # A silent set -e abort (brew exiting 1 after printing only its own errors).
    aborted = run("false\necho unreachable")
    assert aborted.returncode == 1
    assert "unreachable" not in aborted.stdout
    assert "omm installer failed while installing Python with Homebrew (exit status 1)" in aborted.stderr

    # An explicit failure keeps its own status.
    explicit = run("echo 'Homebrew could not install python' >&2\nexit 3")
    assert explicit.returncode == 3
    assert "failed while installing Python with Homebrew" in explicit.stderr

    # Running off the end before omm is installed is a failure, not success.
    early = run("echo half-way")
    assert early.returncode == 1
    assert "stopped unexpectedly while installing Python with Homebrew" in early.stderr

    # Once omm is installed the real status passes through untouched.
    finished = run("OMM_INSTALL_FINISHED=1")
    assert (finished.returncode, finished.stderr) == (0, "")
    interrupted = run("OMM_INSTALL_FINISHED=1\nexit 130")
    assert (interrupted.returncode, interrupted.stderr) == (130, "")

    # A failing command substitution handled by its caller must not trip
    # the guard from inside the subshell.
    handled = run('x=$(exit 4) || true\ny=$(false || true)\nOMM_INSTALL_FINISHED=1')
    assert (handled.returncode, handled.stderr) == (0, "")

    # Wiring in the real script: the trap is armed before the first exit,
    # and the only place that marks the install finished is after "Done.".
    assert script.index("trap installer_exit_guard EXIT") < script.index("REPO_URL=")
    assert script.count("OMM_INSTALL_FINISHED=1") == 1
    assert script.index('echo "Done.') < script.index("OMM_INSTALL_FINISHED=1")
    assert script.index("OMM_INSTALL_FINISHED=1") < script.index('if [ "${OMM_INSTALL_OPEN_WEB:-}" = "1" ]')


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh under test")
def test_exit_guard_catches_a_child_that_swallows_the_piped_script(tmp_path):
    """The original exit-0 failure mode: under `curl | sh`, a child that
    reads stdin eats the rest of the script and sh exits 0 at EOF. The brace
    group prevents that for install.sh itself; this proves the exit guard
    alone also turns it into a failure, in any shell, should a future edit
    ever break the brace group."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    guard = _exit_guard(script)
    brew = tmp_path / "brew"
    brew.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    brew.chmod(0o755)
    # Padding pushes the rest of the script past any shell's read buffer.
    padding = "# padding\n" * 8000
    unguarded = (
        f'set -eu\n{shlex.quote(str(brew))} install python\n{padding}echo "Done."\n'
    )
    guarded = (
        f'set -eu\n{guard}INSTALL_STEP="installing Python with Homebrew"\n'
        f'{shlex.quote(str(brew))} install python\n{padding}echo "Done."\nOMM_INSTALL_FINISHED=1\n'
    )
    for shell in ("sh", "bash", "dash", "zsh"):
        if shell != "sh" and not Path(f"/bin/{shell}").exists():
            continue
        before = _run_piped(unguarded, shell)
        assert (before.returncode, before.stdout) == (0, ""), shell  # the bug: silent success
        after = _run_piped(guarded, shell)
        assert after.returncode == 1, shell
        assert "stopped unexpectedly while installing Python with Homebrew" in after.stderr, shell


def _darwin_bootstrap_block(script: str) -> str:
    start = script.index('if [ "$OS_NAME" = "Darwin" ]; then\n    # A clean macOS')
    end = script.index("\nelse\n    PACKAGE_MANAGER=$(detect_package_manager", start)
    return script[start:end] + "\nfi\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh under test")
def test_failed_brew_install_python_explains_itself_and_exits_nonzero(tmp_path):
    """node3's log: Homebrew could not write a prefix another macOS account
    owns, then the script stopped with no word of its own. A failed `brew
    install python` must print why and exit non-zero - unless Python 3.10+
    turned up anyway (brew can fail after finding it already installed). brew
    also gets /dev/null as stdin so it can never read the piped script."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    brew_log = tmp_path / "brew-stdin"
    brew = tmp_path / "brew"
    brew.write_text(
        "#!/bin/sh\n"
        "echo 'Error: /opt/homebrew is not writable.' >&2\n"
        "echo 'Warning: python@3.15 3.15.0 is already installed.' >&2\n"
        f"cat > {shlex.quote(str(brew_log))}\n"
        'exit "$BREW_RC"\n',
        encoding="utf-8",
    )
    brew.chmod(0o755)
    prefix = tmp_path / "foreign-homebrew"
    prefix.mkdir()
    prefix.chmod(0o555)
    python_marker = tmp_path / "python-appeared"

    def run(brew_rc: int, python_appears: bool) -> subprocess.CompletedProcess:
        if python_marker.exists():
            python_marker.unlink()
        if python_appears:
            python_marker.touch()
        harness = (
            "set -eu\n{\n"
            + _exit_guard(script)
            + _extract_function(script, "report_brew_install_failure() {")
            + f"""
export BREW_RC={brew_rc}
OS_NAME=Darwin
PY=""
BREW={shlex.quote(str(brew))}
BREW_PREFIX={shlex.quote(str(prefix))}
prepare_macos_brew() {{ :; }}
find_real_command() {{ echo /usr/bin/git; }}
find_supported_python() {{ [ -e {shlex.quote(str(python_marker))} ] && echo /fake/python3; }}
"""
            + _darwin_bootstrap_block(script)
            + 'echo "continued"\nOMM_INSTALL_FINISHED=1\n}\n# rest of the piped script\n'
        )
        return _run_piped(harness)

    try:
        failed = run(1, python_appears=False)
        assert failed.returncode == 1
        assert "continued" not in failed.stdout
        assert "Homebrew could not install python" in failed.stderr
        if os.geteuid() != 0:
            assert f"{prefix} is not writable by" in failed.stderr
            assert "belongs to another macOS account" in failed.stderr
        assert "python.org/downloads/macos" in failed.stderr
        assert "omm installer failed while installing Python with Homebrew" in failed.stderr
        assert brew_log.read_text(encoding="utf-8") == ""  # stdin was /dev/null

        recovered = run(1, python_appears=True)
        assert recovered.returncode == 0, recovered.stderr
        assert "continued" in recovered.stdout

        ok = run(0, python_appears=True)
        assert ok.returncode == 0, ok.stderr
    finally:
        prefix.chmod(0o755)


def _fake_python(path: Path, version: str) -> None:
    supported = tuple(int(part) for part in version.split(".")[:2]) >= (3, 10)
    path.write_text(
        "#!/bin/sh\n"
        f'case "$2" in *SystemExit*) exit {0 if supported else 1} ;; esac\n'
        f"echo {version}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_find_supported_python_considers_versioned_and_homebrew_interpreters(tmp_path):
    """node3: PATH had /usr/bin (python3 = 3.9) before /opt/homebrew/bin, and
    only python3/python were probed, so the installer missed Homebrew's
    python3.15 and tried to install Python again. Discovery must also try
    python3.N names on PATH (newest first) and Homebrew's bin directories
    even when they are not on PATH."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    script_path = tmp_path / "find.sh"
    script_path.write_text(_python_discovery_functions(script), encoding="utf-8")

    def make_dir(name: str, pythons: dict[str, str]) -> Path:
        directory = tmp_path / name
        directory.mkdir()
        for filename, version in pythons.items():
            _fake_python(directory / filename, version)
        return directory

    # The runner's real /usr/bin may hold a valid python/python3.N (ubuntu has
    # 3.12), which would win over the fakes; expose only the non-Python tools.
    tools = tmp_path / "tools"
    tools.mkdir()
    for system_dir in ("/usr/bin", "/bin"):
        for entry in Path(system_dir).iterdir():
            if not entry.name.startswith("python") and not (tools / entry.name).exists():
                (tools / entry.name).symlink_to(entry)

    def find(path_dirs: list[str], brew_dirs: list[Path], function: str = "find_supported_python"):
        harness = f"""
PATH={shlex.quote(":".join(path_dirs + [str(tools)]))}
BREW_BIN_DIRS={shlex.quote(" ".join(str(d) for d in brew_dirs))}
is_macos_clt_stub() {{ return 1; }}
. {shlex.quote(str(script_path))}
{function}
"""
        return subprocess.run(["sh", "-c", harness], capture_output=True, text=True, timeout=10)

    system = make_dir("usr-bin", {"python3": "3.9.6"})
    no_brew = tmp_path / "no-brew"

    # Homebrew not on PATH at all (non-login ssh shell), only python3.15 there.
    brew_versioned = make_dir("brew-versioned", {"python3.15": "3.15.0", "python3.9": "3.9.1"})
    found = find([str(system)], [brew_versioned])
    assert found.returncode == 0, found.stderr
    assert found.stdout.strip() == str(brew_versioned / "python3.15")

    # Homebrew's unversioned python3 wins over its versioned names.
    brew_full = make_dir("brew-full", {"python3": "3.13.1", "python3.15": "3.15.0"})
    assert find([str(system)], [brew_full]).stdout.strip() == str(brew_full / "python3")

    # Versioned names on PATH, newest first; -config and other suffixes are
    # not interpreters, python3.9 is too old.
    on_path = make_dir(
        "on-path dir",
        {"python3.9": "3.9.2", "python3.11": "3.11.4", "python3.13": "3.13.0", "python3.14-config": "3.14.0"},
    )
    found = find([str(system), "", str(on_path)], [brew_full])
    assert found.stdout.strip() == str(on_path / "python3.13")

    # Equal versions keep PATH order.
    first = make_dir("first", {"python3.12": "3.12.0"})
    second = make_dir("second", {"python3.12": "3.12.0"})
    assert find([str(system), str(second), str(first)], [no_brew]).stdout.strip() == str(second / "python3.12")

    # A supported unversioned python3 on PATH is still the first choice.
    modern = make_dir("modern", {"python3": "3.12.3"})
    assert find([str(modern), str(on_path)], [brew_full]).stdout.strip() == str(modern / "python3")

    # Nothing usable: fail, and say what was looked at.
    missing = find([str(system)], [no_brew])
    assert missing.returncode != 0 and missing.stdout == ""
    described = find([str(system)], [brew_versioned], "describe_python_candidates")
    assert f"{system / 'python3'}: Python 3.9.6" in described.stdout
    assert f"{brew_versioned / 'python3.15'}: Python 3.15.0" in described.stdout
    assert described.stdout.count(str(system / "python3")) == 1

    assert 'BREW_BIN_DIRS="/opt/homebrew/bin /usr/local/bin"' in script


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh function under test")
def test_pipx_install_falls_back_to_no_watch_extra_once(tmp_path):
    """node1: watchdog has no wheel for Python 3.15, building it failed on a
    broken Command Line Tools linker, and the whole install failed after the
    shared-pip repair retry. The [watch] extra only powers auto-import, so the
    same verified checkout is installed once more without it, later repairs
    reuse that spec, and the user is told how to add auto-import later."""
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    script_path = tmp_path / "pipx.sh"
    script_path.write_text(
        _extract_function(script, "pipx_install_with_repair() {")
        + _extract_function(script, "pipx_install_without_watch() {")
        + _extract_function(script, "report_skipped_watch_extra() {"),
        encoding="utf-8",
    )
    calls = tmp_path / "calls"
    shared = tmp_path / "shared"

    def run(watch_rc: int, base_rc: int) -> tuple[subprocess.CompletedProcess, list[str]]:
        shared.mkdir(exist_ok=True)
        calls.write_text("", encoding="utf-8")
        harness = f"""
set -eu
PY=/fake/python3.15
OS_NAME=Darwin
PIPX_ENV=omm-model
INSTALL_SPEC='/src/abc[watch]'
NO_WATCH_INSTALL_SPEC='/src/abc'
WATCH_EXTRA_SKIPPED=0
run_pipx() {{
    if [ "$1" = environment ]; then printf '%s\\n' {shlex.quote(str(shared))}; return 0; fi
    printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
    case "$*" in *"[watch]"*) return {watch_rc} ;; esac
    return {base_rc}
}}
pipx_shared_dir_is_safe() {{ return 0; }}
. {shlex.quote(str(script_path))}
if ! pipx_install_with_repair && ! pipx_install_without_watch; then
    echo INSTALL-FAILED
    exit 1
fi
echo "SPEC=$INSTALL_SPEC SKIPPED=$WATCH_EXTRA_SKIPPED"
report_skipped_watch_extra
"""
        result = subprocess.run(["sh", "-c", harness], capture_output=True, text=True, timeout=10)
        return result, calls.read_text(encoding="utf-8").splitlines()

    install = "install --force --editable --python /fake/python3.15"

    fell_back, made = run(watch_rc=1, base_rc=0)
    assert fell_back.returncode == 0, fell_back.stderr
    assert made == [f"{install} /src/abc[watch]", f"{install} /src/abc[watch]", f"{install} /src/abc"]
    assert "SPEC=/src/abc SKIPPED=1" in fell_back.stdout
    assert "retrying once without it" in fell_back.stderr
    assert "'omm setting auto-import' will not work yet" in fell_back.stderr
    assert "Xcode Command Line Tools" in fell_back.stderr
    assert '"/fake/python3.15" -m pipx inject omm-model watchdog plyer' in fell_back.stderr

    both_failed, made = run(watch_rc=1, base_rc=1)
    assert both_failed.returncode == 1
    assert "INSTALL-FAILED" in both_failed.stdout
    assert len(made) == 3

    with_watch, made = run(watch_rc=0, base_rc=0)
    assert with_watch.returncode == 0
    assert made == [f"{install} /src/abc[watch]"]
    assert "SPEC=/src/abc[watch] SKIPPED=0" in with_watch.stdout
    assert "auto-import" not in with_watch.stderr

    # The real script: no-watch spec keeps [nvidia], every later repair
    # reinstalls "$INSTALL_SPEC" (now the spec that worked), and the warning
    # is printed with the closing message.
    assert 'NO_WATCH_INSTALL_SPEC="${SRC_DIR}[nvidia]"' in script
    assert "if ! pipx_install_with_repair && ! pipx_install_without_watch; then" in script
    tail = script[script.index("if ! pipx_install_with_repair && ! pipx_install_without_watch; then"):]
    assert tail.count('run_pipx install --force --editable --python "$PY" "$INSTALL_SPEC"') == 2
    assert tail.index('echo "Done.') < tail.index("report_skipped_watch_extra\n") < tail.index(
        "OMM_INSTALL_FINISHED=1"
    )
