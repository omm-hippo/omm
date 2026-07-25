from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def test_compact_list_collapses_engine_columns(isolated_omm_home):
    registry.save_registry(
        {"model.gguf": {"size_bytes": 1024, "linked": {"ollama": True}}}
    )

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "Links" in result.stdout
    assert "Ollama" in result.stdout
