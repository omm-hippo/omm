import importlib.metadata
import subprocess
from pathlib import Path

from rich.console import Console
from rich.progress import Progress
from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_install_spec_points_at_src_dir_on_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    assert cli._install_spec() == str(cli.SRC_DIR)


def test_install_spec_adds_nvidia_extra_on_non_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")

    assert cli._install_spec() == f"{cli.SRC_DIR}[nvidia]"


def test_update_migrates_when_not_yet_migrated_even_if_commit_matches(monkeypatch):
    """Migration must run purely because SRC_DIR isn't set up yet - even
    when the old-style installed commit already equals latest, since the
    point of migrating is switching update *mechanism*, not code."""
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: None)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: same_commit)
    migrate_calls = []
    monkeypatch.setattr(
        cli,
        "_migrate_to_editable_install",
        lambda: migrate_calls.append(1) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert migrate_calls == [1]
    assert refresh_calls == [1]
    assert "reinstalled" in result.stdout.lower()


def test_update_fast_path_skips_pipx_when_deps_unaffected(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    git_calls = []
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: git_calls.append(1) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    pipx_calls = []
    monkeypatch.setattr(cli, "_run_pipx_install_with_progress", lambda args: pipx_calls.append(args))
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert git_calls == [1]
    assert pipx_calls == []
    assert refresh_calls == [1]


def test_update_fast_path_falls_back_to_pipx_when_deps_changed(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)
    pipx_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: pipx_calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert pipx_calls == [["pipx", "install", "--force", "--editable", cli._install_spec()]]
    assert refresh_calls == [1]


def test_update_reports_error_when_git_update_fails(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 1, stdout="", stderr="fetch failed"),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "fetch failed" in result.stderr
    assert refresh_calls == []


def test_update_reports_error_when_pipx_missing(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)

    def _raise(*args, **kwargs):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(cli.subprocess, "Popen", _raise)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert refresh_calls == []


def test_update_reports_error_and_skips_data_refresh_on_pipx_failure(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: _FakeProc(["boom\n"], returncode=1),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "boom" in result.stderr
    assert refresh_calls == []


def test_update_skips_reinstall_when_already_up_to_date(monkeypatch):
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: same_commit)
    popen_calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: popen_calls.append(a) or _FakeProc([]))
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert "up to date" in result.stdout.lower()
    assert popen_calls == []
    assert refresh_calls == [1]


def test_update_refreshes_stale_cache_with_live_remote_head(monkeypatch):
    """A background check that ran before this `update` populated
    update_check.json with a now-outdated remote head. update() fetches
    the remote head live - it must write that fresh value back into the
    cache, or the next command's background check keeps serving the
    stale pre-update reading (false "Update available") until the TTL
    expires."""
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    recorded = []
    monkeypatch.setattr(cli.version_check, "record", lambda head: recorded.append(head))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert recorded == [same_commit]


def test_installed_commit_reads_vcs_info_from_direct_url_json(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: None)

    class _FakeDist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return '{"url": "https://x", "vcs_info": {"commit_id": "deadbeef", "vcs": "git"}}'

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _FakeDist())

    assert cli._installed_commit() == "deadbeef"


def test_installed_commit_returns_none_for_editable_dev_install(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: None)

    class _FakeDist:
        def read_text(self, name):
            return '{"dir_info": {"editable": true}, "url": "file:///repo"}'

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _FakeDist())

    assert cli._installed_commit() is None


def test_installed_commit_returns_none_when_package_not_found(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: None)

    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", _raise)

    assert cli._installed_commit() is None


def test_installed_commit_prefers_src_head_over_direct_url_json(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "from-src-clone")

    class _FakeDist:
        def read_text(self, name):
            return '{"url": "https://x", "vcs_info": {"commit_id": "from-direct-url"}}'

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _FakeDist())

    assert cli._installed_commit() == "from-src-clone"


def test_remote_head_commit_parses_git_ls_remote_output(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="abcdef1234567890\trefs/heads/main\n", stderr=""
        ),
    )

    assert cli._remote_head_commit() == "abcdef1234567890"


def test_deps_satisfied_true_when_all_declared_deps_importable(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = [\n    "click>=8.1",\n    "rich>=13",\n]\n'
    )
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.0")

    assert cli._deps_satisfied() is True


def test_deps_satisfied_false_when_a_declared_dep_is_missing(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = [\n    "click>=8.1",\n    "rich>=13",\n]\n'
    )
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path)

    def _version(name):
        if name == "click":
            raise importlib.metadata.PackageNotFoundError(name)
        return "1.0"

    monkeypatch.setattr(importlib.metadata, "version", _version)

    assert cli._deps_satisfied() is False


def test_deps_satisfied_false_when_pyproject_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path)

    assert cli._deps_satisfied() is False


def test_remote_head_commit_returns_none_when_git_missing(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cli.subprocess, "run", _raise)

    assert cli._remote_head_commit() is None


def test_src_head_commit_returns_none_when_git_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path / "src")

    assert cli._src_head_commit() is None


def test_src_head_commit_returns_head_when_git_dir_present(monkeypatch, tmp_path):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="deadbeef123\n", stderr=""),
    )

    assert cli._src_head_commit() == "deadbeef123"


def test_src_head_commit_returns_none_when_rev_parse_fails(monkeypatch, tmp_path):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository"),
    )

    assert cli._src_head_commit() is None


def test_migrate_to_editable_install_clones_then_pipx_installs(monkeypatch, tmp_path):
    src = tmp_path / "src"
    tmp_clone = tmp_path / "src.new"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True)
            (Path(args[-1]) / "marker").write_text("cloned")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="newcommit\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    run_calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: run_calls.append(args) or fake_run(args, **kwargs),
    )
    verify_calls = []
    monkeypatch.setattr(
        cli.trust,
        "verify_commit",
        lambda repo_dir, commit, anchor: verify_calls.append((repo_dir, commit, anchor)) or (True, "ok"),
    )
    progress_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: progress_calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    result = cli._migrate_to_editable_install()

    assert result.returncode == 0
    assert run_calls == [
        ["git", "clone", "--filter=blob:none", "--quiet", cli._BARE_REPO_URL, str(tmp_clone)],
        ["git", "-C", str(tmp_clone), "rev-parse", "HEAD"],
    ]
    assert verify_calls == [(tmp_clone, "newcommit", cli.trust.current_trust_anchor())]
    assert progress_calls == [["pipx", "install", "--force", "--editable", str(src)]]
    assert (src / "marker").read_text() == "cloned"
    assert not tmp_clone.exists()


def test_migrate_to_editable_install_skips_pipx_when_signature_verification_fails(monkeypatch, tmp_path):
    src = tmp_path / "src"
    tmp_clone = tmp_path / "src.new"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="badcommit\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli.trust, "verify_commit", lambda *a, **k: (False, "commit badcomm failed signature verification")
    )
    progress_calls = []
    monkeypatch.setattr(cli, "_run_pipx_install_with_progress", lambda args: progress_calls.append(args))

    result = cli._migrate_to_editable_install()

    assert result.returncode == 1
    assert "failed signature verification" in result.stderr
    assert progress_calls == []
    assert not tmp_clone.exists()
    assert not src.exists()


def test_migrate_to_editable_install_skips_pipx_when_clone_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path / "src")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="clone failed"),
    )
    progress_calls = []
    monkeypatch.setattr(cli, "_run_pipx_install_with_progress", lambda args: progress_calls.append(args))

    result = cli._migrate_to_editable_install()

    assert result.returncode == 1
    assert progress_calls == []


def test_migrate_to_editable_install_preserves_existing_src_on_clone_failure(monkeypatch, tmp_path):
    """A working editable install must survive a failed re-migration
    attempt (network blip, timeout, interrupted clone) - the old rmtree-
    before-clone order left `omm` permanently broken with
    ModuleNotFoundError until the user reinstalled from scratch."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "marker").write_text("still here")
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="clone failed"),
    )
    monkeypatch.setattr(cli, "_run_pipx_install_with_progress", lambda args: (_ for _ in ()).throw(AssertionError))

    result = cli._migrate_to_editable_install()

    assert result.returncode == 1
    assert (src / "marker").read_text() == "still here"


def test_git_update_src_fetches_then_resets(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if args[-2:] == ["rev-parse", "origin/main"]:
            stdout = "newcommit\n"
        elif args[-2:] == ["get-url", "origin"]:
            stdout = cli._BARE_REPO_URL + "\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    verify_calls = []
    monkeypatch.setattr(
        cli.trust,
        "verify_commit",
        lambda repo_dir, commit, anchor: verify_calls.append((repo_dir, commit, anchor)) or (True, "ok"),
    )

    result = cli._git_update_src()

    assert result.returncode == 0
    assert run_calls == [
        ["git", "-C", str(src), "remote", "get-url", "origin"],
        ["git", "-C", str(src), "fetch", "--quiet", "origin", "main"],
        ["git", "-C", str(src), "rev-parse", "origin/main"],
        ["git", "-C", str(src), "reset", "--hard", "--quiet", "origin/main"],
    ]
    assert verify_calls == [(src, "newcommit", cli.trust.current_trust_anchor())]


def test_git_update_src_skips_reset_when_signature_verification_fails(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        stdout = "newcommit\n" if args[-2:] == ["rev-parse", "origin/main"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.trust, "verify_commit", lambda *a, **k: (False, "commit newcomm failed signature verification"))

    result = cli._git_update_src()

    assert result.returncode == 1
    assert "failed signature verification" in result.stderr
    assert ["git", "-C", str(src), "reset", "--hard", "--quiet", "origin/main"] not in run_calls


def test_git_update_src_stops_after_fetch_failure(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if args[-2:] == ["get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout=cli._BARE_REPO_URL + "\n", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="fetch failed")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._git_update_src()

    assert result.returncode == 1
    assert run_calls == [
        ["git", "-C", str(src), "remote", "get-url", "origin"],
        ["git", "-C", str(src), "fetch", "--quiet", "origin", "main"],
    ]


def test_run_pipx_install_advances_progress_on_known_stage_lines(monkeypatch):
    lines = [
        "creating virtual environment...\n",
        "determining package name from 'x'...\n",
        "some unrelated pip chatter\n",
        "installing omm from spec 'x'...\n",
        "done! ✨\n",
        "installed package omm 0.1.0\n",
    ]
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: _FakeProc(lines))

    with Progress(console=Console(quiet=True)) as progress:
        task_id = progress.add_task("upgrade", total=len(cli._PIPX_INSTALL_STAGES))
        result = cli._run_pipx_install(["pipx", "install"], progress, task_id)
        completed = progress.tasks[0].completed

    assert result.returncode == 0
    assert completed == len(cli._PIPX_INSTALL_STAGES)


def test_run_pipx_install_stalls_at_last_reached_stage_when_lines_missing(monkeypatch):
    lines = ["creating virtual environment...\n", "done! ✨\n"]
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: _FakeProc(lines))

    with Progress(console=Console(quiet=True)) as progress:
        task_id = progress.add_task("upgrade", total=len(cli._PIPX_INSTALL_STAGES))
        cli._run_pipx_install(["pipx", "install"], progress, task_id)
        completed = progress.tasks[0].completed

    # "creating virtual environment" (stage 1) then "done!" (stage 4) -
    # stages 2/3 never printed, so we jump straight to 4, not fabricate 2/3.
    assert completed == 4
