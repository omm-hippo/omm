from typer.testing import CliRunner

from omm import cli, config, runlog

runner = CliRunner()


def test_omm_log_prints_history(isolated_omm_home):
    runlog.start(["list"])
    runlog.finish(0, "ok")
    result = runner.invoke(cli.app, ["log"])
    assert result.exit_code == 0
    assert "omm list" in result.output


def test_omm_log_rebuild(isolated_omm_home):
    runlog.start(["search"])
    runlog.finish(0, "ok")
    (config.OMM_HOME / "logs" / "history.log").unlink()
    result = runner.invoke(cli.app, ["log", "--rebuild"])
    assert result.exit_code == 0
    assert (config.OMM_HOME / "logs" / "history.log").exists()
    assert "omm search" in result.output


def test_omm_log_grep(isolated_omm_home):
    for name in ("list", "search"):
        runlog.start([name])
        runlog.finish(0, "ok")
    result = runner.invoke(cli.app, ["log", "--grep", "search"])
    assert result.exit_code == 0
    assert "omm search" in result.output
    assert "omm list" not in result.output


def test_omm_log_empty(isolated_omm_home):
    result = runner.invoke(cli.app, ["log"])
    assert result.exit_code == 0
    assert "no run log" in result.output.lower()
