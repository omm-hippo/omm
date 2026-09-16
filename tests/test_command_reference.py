"""The CLI is the source of truth for docs/commands.json, README, omm.run."""

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from omm import cli
from omm.command_reference import DOCS_BASE_URL, build_reference, docs_epilog, docs_url

REPO_ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _paths(reference):
    return [tuple(entry["path"]) for entry in reference["commands"]]


def _by_path(reference, *path):
    return next(e for e in reference["commands"] if tuple(e["path"]) == path)


def test_reference_builds_with_the_expected_envelope():
    reference = build_reference(cli.app)
    assert reference["schema_version"] == 1
    assert reference["generated_by"] == "scripts/export_command_reference.py"
    assert reference["docs_base_url"] == DOCS_BASE_URL
    assert isinstance(reference["omm_version"], str) and reference["omm_version"]
    assert reference["commands"], "no commands were collected"


def test_reference_covers_top_level_groups_and_subcommands():
    paths = _paths(build_reference(cli.app))
    for expected in [("install",), ("list",), ("setting",), ("setting", "version"), ("engine", "install")]:
        assert expected in paths


def test_reference_is_sorted_and_hides_hidden_commands():
    paths = _paths(build_reference(cli.app))
    assert paths == sorted(paths)
    assert not [p for p in paths if p[-1].startswith("_")]


def test_summary_description_and_aliases_come_from_the_docstring():
    reference = build_reference(cli.app)
    listing = _by_path(reference, "list")
    assert listing["summary"] == "Show models installed via omm and their linked status."
    assert listing["aliases"] == ["ls"]
    # The `Alias: ls` trailer belongs in `aliases`, never in the prose.
    assert "Alias" not in listing["description"]
    assert _by_path(reference, "uninstall")["aliases"] == ["rm"]
    assert _by_path(reference, "upgrade")["aliases"] == ["up"]


def test_global_flags_are_marked_and_command_flags_are_not():
    install = _by_path(build_reference(cli.app), "install")
    flagged = {
        flag: option["global"] for option in install["options"] for flag in option["flags"]
    }
    assert flagged["--json"] is True
    assert flagged["--quiet"] is True
    assert flagged["--skip-unfit"] is False
    assert [a["name"] for a in install["arguments"]] == ["model_name"]
    assert install["arguments"][0]["required"] is True


def test_usage_is_plain_text_prefixed_with_the_real_program_path():
    reference = build_reference(cli.app)
    assert _by_path(reference, "install")["usage"].startswith("omm install ")
    assert _by_path(reference, "setting", "version")["usage"].startswith("omm setting version")
    assert "\x1b" not in json.dumps(reference)
    assert not any(e["usage"].lower().startswith("usage:") for e in reference["commands"])


def test_docs_url_shape():
    assert docs_url(["install"]) == "https://omm.run/commands/install"
    assert docs_url(["setting", "version"]) == "https://omm.run/commands/setting#version"
    reference = build_reference(cli.app)
    for entry in reference["commands"]:
        assert entry["docs_url"] == docs_url(entry["path"])
        assert entry["docs_url"].startswith(DOCS_BASE_URL + "/")


def test_help_shows_the_documentation_epilog():
    result = runner.invoke(cli.app, ["list", "--help"])
    assert result.exit_code == 0
    assert "https://omm.run/commands/list" in result.output
    assert docs_epilog(["list"]).split(":", 1)[0] in result.output


def test_subcommand_help_shows_an_anchored_documentation_epilog():
    result = runner.invoke(cli.app, ["setting", "version", "--help"])
    assert result.exit_code == 0
    assert "https://omm.run/commands/setting#version" in result.output


def test_root_help_links_the_command_reference():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "Command reference: https://omm.run/commands" in result.output


def test_help_all_still_lists_every_command():
    result = runner.invoke(cli.app, ["help", "--all"])
    assert result.exit_code == 0
    for name in ("install", "uninstall", "setting"):
        assert name in result.output


def test_committed_commands_json_matches_the_cli():
    committed = json.loads((REPO_ROOT / "docs" / "commands.json").read_text(encoding="utf-8"))
    current = build_reference(cli.app)
    drop = lambda doc: {k: v for k, v in doc.items() if k != "omm_version"}  # noqa: E731
    assert drop(committed) == drop(current), (
        "docs/commands.json is stale - run `python scripts/export_command_reference.py`"
    )


def test_check_docs_sync_script_passes_on_this_repo():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_docs_sync.py")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
