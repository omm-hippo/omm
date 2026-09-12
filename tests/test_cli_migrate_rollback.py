"""Regression coverage for audit #4: `_migrate_to_editable_install`'s
pipx-failure rollback used to assume `_remove_update_path(SRC_DIR)` (an
`ignore_errors=True` rmtree) always fully succeeds, then rename the backup
straight over it in a bare `finally` block. On Windows a freshly-cloned
`.git/objects` pack file is read-only, so the rmtree routinely leaves a
partial tree behind; the unconditional `backup_dir.rename(SRC_DIR)` then
raises `FileExistsError` *inside the finally block*, which silently
replaces the real pipx failure in `result` with that rename error and
leaves `SRC_DIR` partially deleted while the backup is orphaned.

These tests exercise the real `_remove_update_path` and the real
`_migrate_to_editable_install` rollback logic (only `subprocess.run` and
the trust check are stubbed, same as the rest of the update test suite) -
no stub stands in for the buggy function itself.
"""

import subprocess
from pathlib import Path

from omm import cli


def _stub_successful_clone_then_failing_pipx(monkeypatch, tmp_clone: Path):
    def fake_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            tmp_clone.mkdir(parents=True)
            (tmp_clone / "marker").write_text("new source")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="newcommit\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.trust, "verify_commit", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: subprocess.CompletedProcess(args, 1, stdout="", stderr="pipx failed: disk full"),
    )


def test_rmtree_retry_readonly_gives_up_quietly_when_chmod_cannot_help(monkeypatch):
    """The onerror hook must never raise back into rmtree (that would
    defeat `ignore_errors`-like tolerance) when even chmod can't unblock a
    file - e.g. another process (or Windows itself) still has it open."""
    monkeypatch.setattr(
        cli.os, "chmod", lambda *a, **k: (_ for _ in ()).throw(PermissionError("still locked"))
    )
    calls = []

    def failing_unlink(path):
        calls.append(path)
        raise PermissionError("still locked")

    # Must not raise - chmod itself fails, so the retry is never attempted,
    # and rmtree's own tolerance for this one file is preserved either way.
    cli._rmtree_retry_readonly(failing_unlink, "some/locked/file", (PermissionError, PermissionError("x"), None))

    assert calls == []


def test_rmtree_retry_readonly_retries_the_failing_call_after_chmod_succeeds(monkeypatch):
    """When chmod does succeed, the hook must retry the original failing
    call (this is the actual Windows .git/objects-pack fix path)."""
    monkeypatch.setattr(cli.os, "chmod", lambda *a, **k: None)
    calls = []

    def flaky_unlink(path):
        calls.append(path)

    cli._rmtree_retry_readonly(flaky_unlink, "some/locked/file", (OSError, OSError("x"), None))

    assert calls == ["some/locked/file"]


def test_remove_update_path_reports_failure_instead_of_silently_ignoring_it(
    monkeypatch, tmp_path
):
    """`_remove_update_path` must tell its caller whether the path is
    actually gone - the old version returned None unconditionally even
    when the rmtree it wraps could not finish (e.g. a Windows file handle
    another process, or the .git/objects pack's own read-only bit, keeps
    the chmod-and-retry hook from clearing it). Stubbing `shutil.rmtree`
    itself (rather than relying on OS-specific unlink/permission behavior)
    keeps this reproduction of "rmtree made no progress" portable."""
    stubborn = tmp_path / "stubborn"
    stubborn.mkdir()
    (stubborn / "locked.txt").write_text("held open")
    monkeypatch.setattr(cli.shutil, "rmtree", lambda path, **kwargs: None)

    removed = cli._remove_update_path(stubborn)

    assert removed is False
    assert stubborn.exists()
    assert (stubborn / "locked.txt").exists()


def test_migrate_rollback_preserves_backup_and_original_pipx_error_when_removal_fails(
    monkeypatch, tmp_path
):
    """CRITICAL-adjacent regression: when the post-failure rmtree can't
    fully clear SRC_DIR, the rollback must not rename the backup on top of
    it (which used to raise inside `finally` and destroy `result`) and must
    not lose the backup - the previous install stays recoverable."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "marker").write_text("old source")
    tmp_clone = tmp_path / "src.new"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    _stub_successful_clone_then_failing_pipx(monkeypatch, tmp_clone)

    # `_remove_update_path(SRC_DIR)` (called only after SRC_DIR now holds
    # the freshly-cloned content) reports it could not finish - the
    # real-world Windows case this stands in for.
    monkeypatch.setattr(cli, "_remove_update_path", lambda path: False)

    result = cli._migrate_to_editable_install()

    # The original pipx failure must survive - not get clobbered by a
    # rename exception raised inside the `finally` block.
    assert result.returncode == 1
    assert "pipx failed: disk full" in result.stderr

    backups = list(tmp_path.glob("src.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "marker").read_text() == "old source"


def test_migrate_rollback_restores_backup_when_removal_succeeds(monkeypatch, tmp_path):
    """Sanity check for the ordinary (non-Windows-locked) rollback path:
    once SRC_DIR is actually gone, the backup is renamed back so the
    previous install keeps working, exactly as before this fix."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "marker").write_text("old source")
    tmp_clone = tmp_path / "src.new"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    _stub_successful_clone_then_failing_pipx(monkeypatch, tmp_clone)

    result = cli._migrate_to_editable_install()

    assert result.returncode == 1
    assert (src / "marker").read_text() == "old source"
    assert list(tmp_path.glob("src.previous-*")) == []
