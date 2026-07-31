from __future__ import annotations

from typer.testing import CliRunner

from omm import cli, config, onboarding

runner = CliRunner()


class _FakeCtx:
    def __init__(self, command="list", args=None, resilient_parsing=False):
        self.invoked_subcommand = command
        self.args = list(args or [])
        self.resilient_parsing = resilient_parsing


def test_maybe_run_onboarding_opens_on_first_interactive_invocation(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    opened = []
    monkeypatch.setattr(cli, "_run_onboarding", lambda current: opened.append(current))

    cli._maybe_run_onboarding(_FakeCtx(), skip_onboarding=False)

    assert len(opened) == 1
    assert opened[0]["onboarding_version"] == 0


def test_maybe_run_onboarding_skips_json_output(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(cli.sys, "argv", ["omm", "list", "--json"])
    monkeypatch.setattr(
        cli,
        "_run_onboarding",
        lambda current: (_ for _ in ()).throw(AssertionError("wizard opened")),
    )

    cli._maybe_run_onboarding(_FakeCtx(), skip_onboarding=False)


def test_json_cli_path_never_opens_onboarding(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(cli.sys, "argv", ["omm", "list", "--json"])
    monkeypatch.setattr(
        cli,
        "_run_onboarding",
        lambda current: (_ for _ in ()).throw(AssertionError("wizard opened")),
    )

    result = runner.invoke(cli.app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "[]"


def test_cancelled_flow_does_not_save_partial_choices(isolated_omm_home, monkeypatch):
    config.load_config()
    before = config.CONFIG_PATH.read_text()
    monkeypatch.setattr(onboarding, "detect_supported_engines", lambda: ())
    monkeypatch.setattr(
        onboarding,
        "inspect_storage",
        lambda path: onboarding.StorageInfo(path, 1024**3),
    )
    monkeypatch.setattr(onboarding, "choose_onboarding_action", lambda: "configure")
    monkeypatch.setattr(onboarding, "collect_onboarding", lambda *a, **k: None)
    monkeypatch.setattr(
        onboarding,
        "apply_onboarding",
        lambda state: (_ for _ in ()).throw(AssertionError("saved")),
    )

    assert cli._run_onboarding(config.load_config()) == "cancelled"
    assert config.CONFIG_PATH.read_text() == before


def test_setup_reruns_even_after_onboarding_was_completed(
    isolated_omm_home, monkeypatch
):
    config.update_config(onboarding_version=config.CURRENT_ONBOARDING_VERSION)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    opened = []
    monkeypatch.setattr(cli, "_run_onboarding", lambda current: opened.append(current))
    monkeypatch.setattr(
        cli.telemetry,
        "flush_pending",
        lambda: (_ for _ in ()).throw(AssertionError("network-like flush ran")),
    )

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code == 0, result.output
    assert len(opened) == 1


def test_setup_fails_fast_without_interactive_terminal(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code == 1
    assert "requires an interactive terminal" in result.stderr


def test_onboarding_runs_before_update_check(isolated_omm_home, monkeypatch):
    order = []
    monkeypatch.setattr(
        cli, "_maybe_run_onboarding", lambda *a, **k: order.append("onboarding")
    )
    monkeypatch.setattr(
        cli, "_maybe_start_update_check", lambda *a, **k: order.append("update-check")
    )
    monkeypatch.setattr(cli, "_maybe_auto_import", lambda *a, **k: None)
    monkeypatch.setattr(cli.telemetry, "flush_pending", lambda: 0)

    cli._root(_FakeCtx(), skip_onboarding=False)

    assert order == ["onboarding", "update-check"]
