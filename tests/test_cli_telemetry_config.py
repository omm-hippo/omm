from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


def test_telemetry_endpoint_none_clears_endpoint(isolated_omm_home):
    config.update_config(telemetry_endpoint="https://example.com", telemetry_backend="self_hosted")

    result = runner.invoke(cli.app, ["setting", "telemetry", "--endpoint", "none"])

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["telemetry_endpoint"] is None
    assert saved["telemetry_backend"] == "local"


def test_telemetry_accepts_local_self_hosted_endpoint(isolated_omm_home):
    result = runner.invoke(
        cli.app,
        ["setting", "telemetry", "--endpoint", "http://127.0.0.1:8000/v1/benchmarks"],
    )

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["telemetry_backend"] == "self_hosted"


def test_upload_enable_requires_explicit_endpoint_before_opt_in(isolated_omm_home):
    runner.invoke(cli.app, ["setting", "telemetry", "--endpoint", "none"])

    result = runner.invoke(cli.app, ["setting", "upload", "benchmark", "--enable"])

    assert result.exit_code == 1
    assert config.load_config()["telemetry_send_policy"] == "ask"


def test_upload_enable_succeeds_once_endpoint_is_configured(isolated_omm_home):
    runner.invoke(cli.app, ["setting", "telemetry", "--endpoint", "https://example.com"])

    result = runner.invoke(cli.app, ["setting", "upload", "benchmark", "--enable"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["telemetry_send_policy"] == "always"


def test_upload_ask_resets_policy_to_ask(isolated_omm_home):
    config.update_config(telemetry_send_policy="always", telemetry_endpoint="https://example.com")

    result = runner.invoke(cli.app, ["setting", "upload", "benchmark", "--ask"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["telemetry_send_policy"] == "ask"


def test_upload_disable_sets_never_policy(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "upload", "benchmark", "--disable"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["telemetry_send_policy"] == "never"


def test_upload_rejects_multiple_policy_flags(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "upload", "benchmark", "--enable", "--disable"])

    assert result.exit_code == 1
    assert "only one" in result.stderr.lower()
