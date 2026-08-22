import importlib.metadata
import json
import subprocess
from pathlib import Path

import pytest
from rich.console import Console
from rich.progress import Progress
from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _canonical_git_install(monkeypatch):
    """Keep Git-update tests independent of whether .git is in the test image."""

    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )


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


def test_omm_version_ignores_newer_src_when_install_is_not_editable(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.148"\n')
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path)
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: False)
    monkeypatch.setattr(cli.package_metadata, "version", lambda: "0.2.129")

    assert cli._omm_version() == "0.2.129"


def test_omm_version_reads_src_for_verified_editable_install(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.148"\n')
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path)
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: True)
    monkeypatch.setattr(cli.package_metadata, "version", lambda: "0.2.129")

    assert cli._omm_version() == "0.2.148"


def test_update_migrates_when_not_yet_migrated_even_if_commit_matches(monkeypatch):
    """Migration must run purely because SRC_DIR isn't set up yet - even
    when the old-style installed commit already equals latest, since the
    point of migrating is switching update *mechanism*, not code."""
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: None)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: same_commit)
    migrate_calls = []
    monkeypatch.setattr(
        cli,
        "_migrate_to_editable_install",
        lambda *a, **k: migrate_calls.append(1) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert migrate_calls == [1]
    assert refresh_calls == [1]
    assert "updated" in result.stdout.lower()


@pytest.mark.parametrize(
    ("source", "command"),
    [
        (cli.package_metadata.InstallSource.PIPX, "pipx upgrade omm-model"),
        (
            cli.package_metadata.InstallSource.PYPI,
            "python -m pip install --upgrade omm-model",
        ),
        (
            cli.package_metadata.InstallSource.HOMEBREW,
            "brew upgrade omm-hippo/omm/omm",
        ),
        (
            cli.package_metadata.InstallSource.WINGET,
            "winget upgrade --id OmmHippo.OMM -e",
        ),
        (
            cli.package_metadata.InstallSource.NPM,
            "npm update --global @omm-hippo/omm",
        ),
    ],
)
def test_package_managed_update_refuses_git_migration_and_shows_manager_command(
    monkeypatch, source, command
):
    monkeypatch.setattr(cli.package_metadata, "install_source", lambda: source)
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(
        cli,
        "_migrate_to_editable_install",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not migrate")),
    )
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert command in result.stderr
    assert "left the installation unchanged" in result.stderr


def test_src_head_ignores_a_stale_clone_for_a_package_managed_install(monkeypatch, tmp_path):
    stale_src = tmp_path / "src"
    (stale_src / ".git").mkdir(parents=True)
    monkeypatch.setattr(cli, "SRC_DIR", stale_src)
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.PIPX,
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not inspect stale Git")),
    )

    assert cli._src_head_commit() is None


def test_update_fast_path_skips_pipx_when_deps_unaffected(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: True)
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new" * 13 + "new")
    git_calls = []
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: git_calls.append(1) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    monkeypatch.setattr(
        cli.package_metadata,
        "find_distribution",
        lambda: (cli.package_metadata.DISTRIBUTION_NAME, object()),
    )
    pipx_calls = []
    monkeypatch.setattr(cli, "_run_pipx_install_with_progress", lambda args: pipx_calls.append(args))
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert git_calls == [1]
    assert pipx_calls == []
    assert refresh_calls == [1]


def test_partial_migration_forces_pipx_even_when_source_and_deps_are_current(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "source-current")
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: False)
    monkeypatch.setattr(
        cli.package_metadata,
        "find_distribution",
        lambda: (cli.package_metadata.DISTRIBUTION_NAME, object()),
    )
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda branch: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    pipx_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: pipx_calls.append(args)
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_verify_pipx_installation", lambda: (object(), None))

    result = cli._perform_update("main")

    assert result.returncode == 0
    assert pipx_calls == [
        ["pipx", "install", "--force", "--editable", cli._install_spec()]
    ]


def test_partial_migration_is_not_reported_as_already_up_to_date(monkeypatch):
    latest = "latest-commit"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: latest)
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: False)
    monkeypatch.setattr(cli, "_installed_commit", lambda: "installed-old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda branch: latest)
    update_calls = []
    monkeypatch.setattr(
        cli,
        "_perform_update",
        lambda branch: update_calls.append(branch)
        or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert update_calls == ["main"]
    assert "already up to date" not in result.stdout.lower()


def test_successful_pipx_process_is_rejected_when_exact_verification_fails(monkeypatch):
    raw = subprocess.CompletedProcess(["pipx", "install"], 0, stdout="done", stderr="")
    monkeypatch.setattr(
        cli,
        "_verify_pipx_installation",
        lambda: (None, "exposed omm still points at the old environment"),
    )

    result = cli._verified_pipx_install_result(raw)

    assert result.returncode == 1
    assert "failed exact verification" in result.stderr
    assert "old environment" in result.stderr


def test_update_reinstalls_when_editable_environment_uses_legacy_distribution_name(
    monkeypatch
):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "old-commit")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old-commit")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new-commit")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    monkeypatch.setattr(
        cli.package_metadata, "find_distribution", lambda: ("omm", object())
    )
    legacy_state = object()
    monkeypatch.setattr(cli, "_capture_legacy_pipx_state", lambda: (legacy_state, None))
    pipx_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: pipx_calls.append(args)
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    verification = object()
    monkeypatch.setattr(
        cli, "_verify_pipx_installation", lambda: (verification, None)
    )
    cleanup_calls = []
    monkeypatch.setattr(
        cli,
        "_cleanup_legacy_pipx_environment",
        lambda value: cleanup_calls.append(value)
        or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert pipx_calls == [
        ["pipx", "install", "--force", "--editable", cli._install_spec()]
    ]
    assert cleanup_calls == [verification]


def test_legacy_distribution_migration_failure_is_reported_and_skips_refresh(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "old-commit")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old-commit")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new-commit")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    monkeypatch.setattr(
        cli.package_metadata, "find_distribution", lambda: ("omm", object())
    )
    legacy_state = object()
    monkeypatch.setattr(cli, "_capture_legacy_pipx_state", lambda: (legacy_state, None))
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: subprocess.CompletedProcess(args, 1, stdout="", stderr="migration failed"),
    )
    monkeypatch.setattr(
        cli,
        "_rollback_legacy_pipx_migration",
        lambda state, reason: subprocess.CompletedProcess(
            [], 1, stdout="", stderr=f"{reason}; legacy restored"
        ),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "migration failed" in result.stderr
    assert refresh_calls == []


def test_legacy_distribution_is_not_removed_when_new_environment_verification_fails(
    monkeypatch
):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "old-commit")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old-commit")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new-commit")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    monkeypatch.setattr(cli.package_metadata, "find_distribution", lambda: ("omm", object()))
    legacy_state = object()
    monkeypatch.setattr(cli, "_capture_legacy_pipx_state", lambda: (legacy_state, None))
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        cli, "_verify_pipx_installation", lambda: (None, "wrong exposed app")
    )
    monkeypatch.setattr(
        cli,
        "_cleanup_legacy_pipx_environment",
        lambda verification: (_ for _ in ()).throw(AssertionError("must not uninstall legacy")),
    )
    monkeypatch.setattr(
        cli,
        "_rollback_legacy_pipx_migration",
        lambda state, reason: subprocess.CompletedProcess(
            [], 1, stdout="", stderr=f"{reason} Legacy environment was not removed."
        ),
    )
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "failed exact verification" in " ".join(result.stderr.split())
    assert "legacy environment was not removed" in result.stderr.lower()


def test_not_yet_migrated_legacy_install_uses_the_common_finalize_path(monkeypatch):
    legacy_state = object()
    install_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    finalized = subprocess.CompletedProcess([], 0, stdout="finalized", stderr="")
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )
    monkeypatch.setattr(cli.package_metadata, "find_distribution", lambda: ("omm", object()))
    monkeypatch.setattr(cli, "_capture_legacy_pipx_state", lambda: (legacy_state, None))
    monkeypatch.setattr(cli, "_src_head_commit", lambda: None)
    monkeypatch.setattr(cli, "_migrate_to_editable_install", lambda branch: install_result)
    finalize_calls = []
    monkeypatch.setattr(
        cli,
        "_finalize_legacy_pipx_migration",
        lambda result, state: finalize_calls.append((result, state)) or finalized,
    )

    result = cli._perform_update("main")

    assert result is finalized
    assert finalize_calls == [(install_result, legacy_state)]


def _pipx_snapshot(venvs_root: Path, *, version="0.2.119"):
    internal_bin = venvs_root / "omm-model" / "bin"
    return {
        "pipx_spec_version": "0.1",
        "venvs": {
            "omm-model": {
                "environment": "omm-model",
                "main_package": {
                    "package": "omm-model",
                    "package_version": version,
                    "apps": ["omm", "localfit-server"],
                    "app_paths": [
                        {
                            "__type__": "Path",
                            "__Path__": str(internal_bin / "omm"),
                        },
                        {
                            "__type__": "Path",
                            "__Path__": str(internal_bin / "localfit-server"),
                        },
                    ],
                },
            }
        },
    }


@pytest.mark.parametrize(
    ("metadata_version", "metadata_has_environment"),
    [("0.5", False), ("0.12", True)],
)
def test_verify_pipx_installation_checks_paths_metadata_apps_and_exact_versions(
    monkeypatch, tmp_path, metadata_version, metadata_has_environment
):
    venvs_root = tmp_path / "venvs"
    bin_dir = tmp_path / "bin"
    internal_bin = venvs_root / "omm-model" / "bin"
    internal_bin.mkdir(parents=True)
    bin_dir.mkdir()
    internal_omm = internal_bin / "omm"
    internal_omm.write_text("internal")
    (internal_bin / "localfit-server").write_text("server")
    (bin_dir / "omm").symlink_to(internal_omm)
    (bin_dir / "localfit-server").symlink_to(internal_bin / "localfit-server")
    snapshot = _pipx_snapshot(venvs_root)
    metadata = snapshot["venvs"]["omm-model"]
    metadata["pipx_metadata_version"] = metadata_version
    if not metadata_has_environment:
        metadata.pop("environment")
        metadata["main_package"]["app_paths"].reverse()
    snapshot["venvs"]["omm-model"] = {"metadata": metadata}
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{venvs_root}\n", stderr="")
        if args == ["pipx", "environment", "--value", "PIPX_BIN_DIR"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{bin_dir}\n", stderr="")
        if args == ["pipx", "list", "--json"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(snapshot), stderr="")
        if args in ([str(internal_omm), "--version"], [str(bin_dir / "omm"), "--version"]):
            return subprocess.CompletedProcess(args, 0, stdout="omm 0.2.119\n", stderr="")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(cli, "_run_pipx_query", fake_run)
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.119")
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    verification, error = cli._verify_pipx_installation()

    assert error is None
    assert verification is not None
    assert verification.internal_omm == internal_omm
    assert verification.exposed_omm == bin_dir / "omm"
    assert calls[:3] == [
        ["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"],
        ["pipx", "environment", "--value", "PIPX_BIN_DIR"],
        ["pipx", "list", "--json"],
    ]


def test_verify_pipx_installation_rejects_a_missing_secondary_app_link(
    monkeypatch, tmp_path
):
    venvs_root = tmp_path / "venvs"
    bin_dir = tmp_path / "bin"
    internal_bin = venvs_root / "omm-model" / "bin"
    internal_bin.mkdir(parents=True)
    bin_dir.mkdir()
    internal_omm = internal_bin / "omm"
    internal_omm.write_text("internal")
    (internal_bin / "localfit-server").write_text("server")
    (bin_dir / "omm").symlink_to(internal_omm)
    snapshot = _pipx_snapshot(venvs_root)
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout=f"{venvs_root}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{bin_dir}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(snapshot), stderr=""),
        ]
    )
    monkeypatch.setattr(cli, "_run_pipx_query", lambda args, **kwargs: next(responses))
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.119")
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    verification, error = cli._verify_pipx_installation()

    assert verification is None
    assert "localfit-server" in error


def test_capture_legacy_state_requires_the_running_legacy_venv(monkeypatch, tmp_path):
    venvs_root = tmp_path / "venvs"
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(
        cli,
        "_pipx_environment_value",
        lambda name: ((venvs_root if name == "PIPX_LOCAL_VENVS" else bin_dir), None),
    )
    monkeypatch.setattr(cli.sys, "prefix", str(venvs_root / "some-other-environment"))
    monkeypatch.setattr(
        cli,
        "_pipx_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before snapshot capture")),
    )

    state, error = cli._capture_legacy_pipx_state()

    assert state is None
    assert "not inside the legacy pipx omm environment" in error


def test_verify_pipx_installation_rejects_wrong_main_package(monkeypatch, tmp_path):
    venvs_root = tmp_path / "venvs"
    snapshot = _pipx_snapshot(venvs_root)
    snapshot["venvs"]["omm-model"]["main_package"]["package"] = "not-omm"
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout=f"{venvs_root}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{tmp_path / 'bin'}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(snapshot), stderr=""),
        ]
    )
    monkeypatch.setattr(cli, "_run_pipx_query", lambda args, **kwargs: next(responses))

    verification, error = cli._verify_pipx_installation()

    assert verification is None
    assert "wrong main package" in error


def test_cleanup_removes_only_current_legacy_env_and_accepts_preserved_new_link(
    monkeypatch, tmp_path
):
    venvs_root = tmp_path / "venvs"
    legacy_env = venvs_root / "omm"
    legacy_env.mkdir(parents=True)
    snapshot = _pipx_snapshot(venvs_root)
    snapshot["venvs"]["omm"] = {
        "environment": "omm",
        "main_package": {"package": "omm", "apps": ["omm"]},
    }
    verification = cli._PipxInstallVerification(
        local_venvs=venvs_root,
        bin_dir=tmp_path / "bin",
        snapshot=snapshot,
        internal_omm=tmp_path / "internal-omm",
        exposed_omm=tmp_path / "omm",
        expected_version="0.2.119",
    )
    monkeypatch.setattr(cli.sys, "prefix", str(legacy_env))
    calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_query",
        lambda args, **kwargs: calls.append(args)
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_verify_pipx_installation", lambda: (verification, None))

    result = cli._cleanup_legacy_pipx_environment(verification)

    assert result.returncode == 0
    assert calls == [["pipx", "uninstall", "omm"]]


def test_cleanup_repairs_a_missing_link_without_requiring_pipx_expose(monkeypatch, tmp_path):
    venvs_root = tmp_path / "venvs"
    legacy_env = venvs_root / "omm"
    legacy_env.mkdir(parents=True)
    snapshot = _pipx_snapshot(venvs_root)
    snapshot["venvs"]["omm"] = {
        "main_package": {"package": "omm", "apps": ["omm"]},
    }
    verification = cli._PipxInstallVerification(
        local_venvs=venvs_root,
        bin_dir=tmp_path / "bin",
        snapshot=snapshot,
        internal_omm=tmp_path / "internal-omm",
        exposed_omm=tmp_path / "omm",
        expected_version="0.2.119",
    )
    monkeypatch.setattr(cli.sys, "prefix", str(legacy_env))
    query_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_query",
        lambda args, **kwargs: query_calls.append(args)
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    verifications = iter([(None, "link missing"), (verification, None)])
    monkeypatch.setattr(cli, "_verify_pipx_installation", lambda: next(verifications))
    install_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: install_calls.append(args)
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    result = cli._cleanup_legacy_pipx_environment(verification)

    assert result.returncode == 0
    assert query_calls == [["pipx", "uninstall", "omm"]]
    assert install_calls == [
        ["pipx", "install", "--force", "--editable", cli._install_spec()]
    ]


def test_cleanup_failure_keeps_verified_new_install_and_warns(monkeypatch, tmp_path):
    from io import StringIO

    venvs_root = tmp_path / "venvs"
    legacy_env = venvs_root / "omm"
    legacy_env.mkdir(parents=True)
    snapshot = _pipx_snapshot(venvs_root)
    snapshot["venvs"]["omm"] = {
        "environment": "omm",
        "main_package": {"package": "omm", "apps": ["omm"]},
    }
    verification = cli._PipxInstallVerification(
        local_venvs=venvs_root,
        bin_dir=tmp_path / "bin",
        snapshot=snapshot,
        internal_omm=tmp_path / "internal-omm",
        exposed_omm=tmp_path / "omm",
        expected_version="0.2.119",
    )
    monkeypatch.setattr(cli.sys, "prefix", str(legacy_env))
    calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_query",
        lambda args, **kwargs: calls.append(args)
        or subprocess.CompletedProcess(args, 1, stdout="", stderr="environment is locked"),
    )
    monkeypatch.setattr(cli, "_verify_pipx_installation", lambda: (verification, None))
    warning_output = StringIO()
    monkeypatch.setattr(cli, "err_console", Console(file=warning_output, highlight=False))

    result = cli._cleanup_legacy_pipx_environment(verification)

    assert result.returncode == 0
    assert calls == [["pipx", "uninstall", "omm"]]
    assert "pipx uninstall omm" in warning_output.getvalue()
    assert "environment is locked" in warning_output.getvalue()


def test_cleanup_never_uninstalls_an_unrelated_legacy_named_environment(monkeypatch, tmp_path):
    from io import StringIO

    snapshot = _pipx_snapshot(tmp_path / "venvs")
    snapshot["venvs"]["omm"] = {
        "environment": "omm",
        "main_package": {"package": "unrelated-omm", "apps": ["omm"]},
    }
    verification = cli._PipxInstallVerification(
        local_venvs=tmp_path / "venvs",
        bin_dir=tmp_path / "bin",
        snapshot=snapshot,
        internal_omm=tmp_path / "internal-omm",
        exposed_omm=tmp_path / "omm",
        expected_version="0.2.119",
    )
    monkeypatch.setattr(
        cli,
        "_run_pipx_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not uninstall")),
    )
    warning_output = StringIO()
    monkeypatch.setattr(cli, "err_console", Console(file=warning_output, highlight=False))

    result = cli._cleanup_legacy_pipx_environment(verification)

    assert result.returncode == 0
    assert "could not be identified safely" in warning_output.getvalue()


def test_failed_new_install_rolls_back_all_legacy_apps_and_verifies_omm(
    monkeypatch, tmp_path
):
    venvs_root = tmp_path / "venvs"
    legacy_bin = venvs_root / "omm" / "bin"
    exposed_bin = tmp_path / "bin"
    legacy_bin.mkdir(parents=True)
    exposed_bin.mkdir()
    internal_omm = legacy_bin / "omm"
    internal_server = legacy_bin / "localfit-server"
    internal_omm.write_text("legacy omm")
    internal_server.write_text("legacy server")
    exposed_omm = exposed_bin / "omm"
    exposed_server = exposed_bin / "localfit-server"
    exposed_omm.write_text("unverified new omm")
    exposed_server.write_text("unverified new server")
    snapshot = _pipx_snapshot(venvs_root)
    snapshot["venvs"]["omm"] = {
        "main_package": {
            "package": "omm",
            "package_version": "0.2.118",
            "apps": ["omm", "localfit-server"],
            "app_paths": [str(internal_server), str(internal_omm)],
        }
    }
    state = cli._LegacyPipxState(
        local_venvs=venvs_root,
        bin_dir=exposed_bin,
        snapshot=snapshot,
        internal_omm=internal_omm,
        exposed_omm=exposed_omm,
        owned_apps=((internal_omm, exposed_omm), (internal_server, exposed_server)),
        expected_version="0.2.118",
    )
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.119")
    monkeypatch.setattr(cli, "_pipx_snapshot", lambda: (snapshot, None))
    calls = []

    def fake_query(args, **kwargs):
        calls.append(args)
        if args == ["pipx", "uninstall", "omm-model"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args in ([str(internal_omm), "--version"], [str(exposed_omm), "--version"]):
            return subprocess.CompletedProcess(args, 0, stdout="omm 0.2.118\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(cli, "_run_pipx_query", fake_query)

    result = cli._rollback_legacy_pipx_migration(state, "new install failed")

    assert result.returncode == 1
    assert "restored and verified" in result.stderr
    assert calls[0] == ["pipx", "uninstall", "omm-model"]
    assert exposed_omm.samefile(internal_omm)
    assert exposed_server.samefile(internal_server)


def test_update_fast_path_falls_back_to_pipx_when_deps_changed(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)
    pipx_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: pipx_calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_verify_pipx_installation", lambda: (object(), None))
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert pipx_calls == [["pipx", "install", "--force", "--editable", cli._install_spec()]]
    assert refresh_calls == [1]


def test_update_reports_error_when_git_update_fails(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="fetch failed"),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "fetch failed" in result.stderr
    assert refresh_calls == []


def test_update_reports_error_when_pipx_missing(monkeypatch):
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
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
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
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


def test_update_reports_error_when_pipx_permission_denied(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new" * 13 + "new")
    monkeypatch.setattr(
        cli, "_git_update_src", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)

    def _raise(*args, **kwargs):
        raise PermissionError("pipx exists but is not executable")

    monkeypatch.setattr(cli.subprocess, "Popen", _raise)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "update failed" in result.stderr.lower()
    assert refresh_calls == []


def test_update_skips_reinstall_when_already_up_to_date(monkeypatch):
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: True)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: same_commit)
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
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: True)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: same_commit)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    recorded = []
    monkeypatch.setattr(cli.version_check, "record", lambda head, *a, **k: recorded.append(head))

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


def test_installed_commit_ignores_stale_src_for_vcs_snapshot(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "from-stale-src-clone")

    class _FakeDist:
        def read_text(self, name):
            return '{"url": "https://x", "vcs_info": {"commit_id": "from-direct-url"}}'

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _FakeDist())

    assert cli._installed_commit() == "from-direct-url"


def test_installed_commit_uses_src_for_matching_editable_install(monkeypatch, tmp_path):
    src = tmp_path / "source with spaces"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "from-src-clone")
    monkeypatch.setattr(
        cli.package_metadata,
        "direct_url",
        lambda: {"url": src.as_uri(), "dir_info": {"editable": True}},
    )

    assert cli._installed_commit() == "from-src-clone"


def test_editable_install_rejects_a_different_or_noneditable_source(monkeypatch, tmp_path):
    src = tmp_path / "src"
    other = tmp_path / "other"
    monkeypatch.setattr(cli, "SRC_DIR", src)

    assert cli._editable_install_uses_src(
        {"url": other.as_uri(), "dir_info": {"editable": True}}
    ) is False
    assert cli._editable_install_uses_src(
        {"url": src.as_uri(), "dir_info": {"editable": False}}
    ) is False
    assert cli._editable_install_uses_src(
        {"url": src.as_uri(), "dir_info": {"editable": True}}
    ) is True


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
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )
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
    monkeypatch.setattr(cli, "_verify_pipx_installation", lambda: (object(), None))

    result = cli._migrate_to_editable_install()

    assert result.returncode == 0
    assert run_calls == [
        [
            "git", "clone", "--filter=blob:none", "--branch", "main",
            "--single-branch", "--quiet", cli._BARE_REPO_URL, str(tmp_clone),
        ],
        ["git", "-C", str(tmp_clone), "rev-parse", "HEAD"],
    ]
    assert verify_calls == [(tmp_clone, "newcommit", cli.trust.current_trust_anchor())]
    assert progress_calls == [["pipx", "install", "--force", "--editable", str(src)]]
    assert (src / "marker").read_text() == "cloned"
    assert not tmp_clone.exists()


def test_migrate_restores_existing_src_when_pipx_install_fails(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "marker").write_text("old source")
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            clone = Path(args[-1])
            clone.mkdir(parents=True)
            (clone / "marker").write_text("new source")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="newcommit\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.trust, "verify_commit", lambda *args: (True, "ok"))
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: subprocess.CompletedProcess(args, 1, stdout="", stderr="pipx failed"),
    )

    result = cli._migrate_to_editable_install()

    assert result.returncode == 1
    assert (src / "marker").read_text() == "old source"
    assert not (tmp_path / "src.new").exists()
    assert list(tmp_path.glob("src.previous-*")) == []


def test_migrate_restores_existing_src_when_pipx_verification_fails(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "marker").write_text("old source")
    monkeypatch.setattr(cli, "SRC_DIR", src)

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            clone = Path(args[-1])
            clone.mkdir(parents=True)
            (clone / "marker").write_text("new source")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="newcommit\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.trust, "verify_commit", lambda *args: (True, "ok"))
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: subprocess.CompletedProcess(args, 0, stdout="done", stderr=""),
    )
    monkeypatch.setattr(
        cli,
        "_verify_pipx_installation",
        lambda: (None, "wrong executable target"),
    )

    result = cli._migrate_to_editable_install()

    assert result.returncode == 1
    assert "failed exact verification" in result.stderr
    assert (src / "marker").read_text() == "old source"
    assert list(tmp_path.glob("src.previous-*")) == []


def test_migrate_removes_new_src_when_pipx_command_is_missing(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            clone = Path(args[-1])
            clone.mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="newcommit\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.trust, "verify_commit", lambda *args: (True, "ok"))
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: (_ for _ in ()).throw(FileNotFoundError("pipx")),
    )

    with pytest.raises(FileNotFoundError, match="pipx"):
        cli._migrate_to_editable_install()

    assert not src.exists()
    assert not (tmp_path / "src.new").exists()
    assert list(tmp_path.glob("src.previous-*")) == []


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
        if args[-2:] == ["rev-parse", "HEAD"]:
            stdout = "oldcommit\n"
        elif args[-2:] == ["rev-parse", "origin/main"]:
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
        "verify_update",
        lambda repo_dir, current, target, anchor: verify_calls.append(
            (repo_dir, current, target, anchor)
        ) or (True, "ok"),
    )

    result = cli._git_update_src()

    assert result.returncode == 0
    assert run_calls == [
        ["git", "-C", str(src), "remote", "get-url", "origin"],
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        ["git", "-C", str(src), "fetch", "--quiet", "origin", "main:refs/remotes/origin/main"],
        ["git", "-C", str(src), "rev-parse", "origin/main"],
        ["git", "-C", str(src), "checkout", "-B", "main", "origin/main", "--force", "--quiet"],
    ]
    assert verify_calls == [
        (src, "oldcommit", "newcommit", cli.trust.current_trust_anchor())
    ]


def test_git_update_src_skips_reset_when_signature_verification_fails(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        stdout = "newcommit\n" if args[-2:] == ["rev-parse", "origin/main"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.trust, "verify_update", lambda *a, **k: (False, "commit newcomm failed signature verification"))

    result = cli._git_update_src()

    assert result.returncode == 1
    assert "failed signature verification" in result.stderr
    assert ["git", "-C", str(src), "checkout", "-B", "main", "origin/main", "--force", "--quiet"] not in run_calls


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
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        ["git", "-C", str(src), "fetch", "--quiet", "origin", "main:refs/remotes/origin/main"],
    ]


def test_pipx_child_env_adds_the_user_scripts_directory(monkeypatch, tmp_path):
    system_bin = tmp_path / "system-bin"
    user_bin = tmp_path / "user-bin"
    monkeypatch.setenv("PATH", str(system_bin))
    monkeypatch.setattr(cli.sysconfig, "get_path", lambda *args, **kwargs: str(user_bin))

    env = cli._pipx_child_env()

    assert env["PATH"].split(cli.os.pathsep) == [
        str(system_bin),
        str(user_bin),
        str(Path.home() / ".local" / "bin"),
    ]
    assert env["PYTHONIOENCODING"] == "utf-8"


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


def test_update_with_quiet_flag_does_not_crash(monkeypatch):
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_editable_install_uses_src", lambda *args: True)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: same_commit)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["update", "--quiet"])

    assert result.exit_code == 0, result.stdout


def test_pipx_app_names_strip_windows_launcher_suffix():
    """pipx on Windows records apps as `omm.exe` / `localfit-server.exe`
    (verified on a real Windows 11 pipx 1.16 install); `omm update` used
    to compare that verbatim against {"omm", "localfit-server"} and refuse
    the legacy-environment migration with "exposes an unexpected app set"."""
    assert cli._pipx_app_names(["localfit-server.exe", "omm.exe"]) == ["localfit-server", "omm"]
    assert cli._pipx_app_names(["omm", "localfit-server"]) == ["omm", "localfit-server"]
    assert cli._pipx_app_names(["OMM.EXE"]) == ["OMM"]
    assert cli._pipx_app_names("omm") is None
    assert cli._pipx_app_names(["omm", 3]) is None
    assert set(cli._pipx_app_names(["localfit-server.exe", "omm.exe"])) == cli._PIPX_EXPECTED_APPS
