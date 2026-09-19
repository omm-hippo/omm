import pytest
import typer
from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


@pytest.fixture(autouse=True)
def _canonical_git_install(monkeypatch):
    """Keep Git-channel tests independent of whether .git is in the test image."""

    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )


def test_dash_dash_version_prints_installed_version_and_exits_eagerly(monkeypatch):
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.119")
    monkeypatch.setattr(
        cli,
        "_maybe_start_update_check",
        lambda ctx: (_ for _ in ()).throw(AssertionError("version must exit eagerly")),
    )

    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout == "omm 0.2.119\n"


def test_bare_omm_prints_version_only():
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("Ω omm ")
    assert "Commands" not in result.stdout


def test_bare_omm_shows_stable_channel_by_default(isolated_omm_home):
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert "stable" in result.stdout


def test_bare_omm_shows_beta_channel_when_selected(isolated_omm_home):
    config.update_config(update_channel="beta")

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert "beta" in result.stdout


def test_help_with_no_args_matches_dash_dash_help():
    result = runner.invoke(cli.app, ["help"])
    expected = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.stdout
    assert "Example usage" in result.stdout
    assert result.stdout == expected.stdout


def test_help_all_lists_every_command():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "cleanup" in result.stdout


def test_help_all_lists_pin_unpin_rollback():
    # #295: model revision pin/rollback are top-level Core commands, next
    # to upgrade/uninstall - not nested under `omm setting`.
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    for name in ("omm pin", "omm unpin", "omm rollback"):
        assert name in result.stdout, f"missing command: {name}"


def test_help_all_expands_nested_setting_subcommands():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    for name in ("telemetry", "upload", "version", "calibrate", "catalog-trust", "catalog-status", "catalog-rollback"):
        assert f"setting {name}" in result.stdout, f"missing setting subcommand: {name}"


def test_help_all_is_a_compact_listing_not_a_full_flag_dump():
    # `help --all` lists command names with a one-line summary (git/docker/
    # gh `-a` style), not each command's complete --help text. Per-command
    # flags belong to `omm <command> --help`, which this points readers at.
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "--skip-unfit" not in result.stdout
    assert "--manifest-url" not in result.stdout
    assert "full option list" in result.stdout
    assert len(result.stdout.splitlines()) < 100


def test_help_all_hints_at_flags_option():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "--flags" in result.stdout


def test_help_command_keeps_click_default_and_required_markers():
    # rich's markup=True by default would swallow "[default: 40]"/"[required]"
    # as (unknown, dropped) markup tags instead of printing them literally.
    log_result = runner.invoke(cli.app, ["help", "log"])
    assert log_result.exit_code == 0, log_result.stdout
    assert "[default: 40]" in log_result.stdout

    install_result = runner.invoke(cli.app, ["help", "install"])
    assert install_result.exit_code == 0, install_result.stdout
    assert "[required]" in install_result.stdout


def test_help_all_flags_keeps_default_markers():
    result = runner.invoke(cli.app, ["help", "--all", "--flags"])

    assert result.exit_code == 0, result.stdout
    assert "[default: 40]" in result.stdout


def test_help_all_flags_expands_each_command_option_list():
    result = runner.invoke(cli.app, ["help", "--all", "--flags"])

    assert result.exit_code == 0, result.stdout
    assert "--skip-unfit" in result.stdout
    assert "omm search" in result.stdout
    assert "omm setting theme" in result.stdout
    # The default-mode hint about --flags itself should not repeat once shown.
    assert "Add `--flags`" not in result.stdout


def test_help_all_flags_shows_positional_argument_rules():
    # search/verify take a positional query/model_name argument. `omm
    # search --help` already shows it under its own ARGUMENTS section
    # (issue #366 comment: "help --flags 의 출력에 명령어의 ARGUMENTS 규칙도
    # 출력하게 하면 좋을 것 같습니다") - the compact --flags listing should
    # surface the same rule (e.g. required) instead of hiding it.
    result = runner.invoke(cli.app, ["help", "--all", "--flags"])

    assert result.exit_code == 0, result.stdout
    assert "ARGUMENTS:" in result.stdout
    lines = [line.strip() for line in result.stdout.splitlines()]
    assert any(line.startswith("query") for line in lines)
    assert any(line.startswith("model_name") for line in lines)


def test_help_flags_curated_also_shows_positional_argument_rules():
    result = runner.invoke(cli.app, ["help", "--flags"])

    assert result.exit_code == 0, result.stdout
    assert "ARGUMENTS:" in result.stdout


def test_help_all_excludes_hidden_commands():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "_bg-version-check" not in result.stdout


def test_help_all_setting_subcommand_usage_line_is_not_double_prefixed():
    # Regression guard: the nested-group renderer once built each setting
    # subcommand's context with the already-prefixed name (e.g.
    # "setting calibrate") as a child of a context whose own info_name was
    # "setting", so Click's usage-line builder walked the parent chain and
    # duplicated the prefix into "setting setting calibrate".
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "setting setting" not in result.stdout
    assert "setting calibrate" in result.stdout


def test_help_with_command_name_shows_that_commands_help():
    result = runner.invoke(cli.app, ["help", "install"])

    assert result.exit_code == 0, result.stdout
    assert "Download a model" in result.stdout


def test_help_with_unknown_command_errors():
    result = runner.invoke(cli.app, ["help", "no-such-command"])

    assert result.exit_code == 1
    assert "No such command" in result.stderr


def test_help_all_lists_exit_code_contract():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "0 success, 1 failure, 2 usage error" in result.stdout


def test_help_all_shows_setting_group_description():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "View or change omm settings" in result.stdout


def test_help_accepts_global_flags_after_the_subcommand_name():
    # Human help accepts the presentation/confirmation flags after its name.
    # --json has a separate unsupported_json contract because help is text.
    for flag in ("--quiet", "-q", "--yes", "-y", "--no-color"):
        result = runner.invoke(cli.app, ["help", flag])
        assert result.exit_code == 0, (flag, result.stdout, result.stderr)


def test_help_all_summarises_a_command_in_one_sentence():
    """`help --all` prints each command's first docstring paragraph, so a
    docstring written as one unbroken paragraph dumped its whole
    implementation note into what should be a one-line summary."""
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "Reinstall omm from the latest source" in result.stdout
    assert "SRC_DIR" not in result.stdout


def test_command_help_still_carries_the_full_description():
    """Shortening the summary must not lose the detail - it moves into the
    body, which `omm update --help` still shows."""
    result = runner.invoke(cli.app, ["update", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "SRC_DIR" in result.stdout


def test_help_all_indents_a_wrapped_summary_under_the_summary_column():
    """A summary too long for the terminal used to wrap back to column 0,
    where the continuation read as if it belonged to no command."""
    result = runner.invoke(cli.app, ["help", "--all"])
    lines = result.stdout.splitlines()

    row = next(i for i, line in enumerate(lines) if line.strip().startswith("omm upgrade"))
    summary_column = lines[row].index("Look for a better model")

    assert lines[row + 1].strip(), "expected this summary to wrap at 80 columns"
    assert lines[row + 1].startswith(" " * summary_column)


def test_help_flags_without_all_expands_the_curated_commands_only():
    # Issue #337: `omm help --flags` used to print exactly what `omm help`
    # prints. It must keep the curated (short) command list but expand
    # each listed command's option list beneath its usage line.
    plain = runner.invoke(cli.app, ["help"])
    result = runner.invoke(cli.app, ["help", "--flags"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout != plain.stdout
    assert "omm search TEXT" in result.stdout
    assert "--skip-unfit" in result.stdout
    assert "omm engine install" in result.stdout
    assert "omm upgrade [MODEL]" in result.stdout
    assert "Further help:" in result.stdout
    # Still curated: commands only reachable through `--all` stay hidden.
    assert "omm pin" not in result.stdout
    assert "omm rollback" not in result.stdout
    assert "omm setting theme" not in result.stdout


def test_help_flags_is_shorter_than_help_all_flags():
    curated = runner.invoke(cli.app, ["help", "--flags"])
    full = runner.invoke(cli.app, ["help", "--all", "--flags"])

    assert curated.exit_code == 0 and full.exit_code == 0
    assert len(curated.stdout.splitlines()) < len(full.stdout.splitlines())


def test_root_help_sections_name_real_commands():
    # The curated sections are hand-written; a typo would silently drop
    # that command's flags from `help --flags` and lie in `omm help`.
    command = typer.main.get_command(cli.app)
    for _title, entries in cli._ROOT_HELP_SECTIONS:
        for entry in entries:
            cmd = command
            for part in entry.split():
                if not part.islower():
                    break
                assert part in cmd.commands, f"unknown command in curated help: {entry}"
                cmd = cmd.commands[part]
