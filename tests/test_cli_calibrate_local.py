from typer.testing import CliRunner

from omm import cli, config, registry

runner = CliRunner()


def test_calibrate_records_local_factor(isolated_omm_home, monkeypatch):
    filename = "model-1B-Q4.gguf"
    registry.save_registry(
        {
            filename: {
                "repo_id": "org/model",
                "size_bytes": 1024,
                "ollama_name": "model",
                "linked": {"ollama": True},
            }
        }
    )
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{"leaf": True, "value": 20.0}]})
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed_interval",
        lambda *args, **kwargs: (20.0, 20.0, 20.0),
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)

    result = runner.invoke(cli.app, ["setting", "calibrate", filename])

    assert result.exit_code == 0, result.stdout
    assert "correction ×1.50" in result.stdout
    assert config.CALIBRATION_PATH.exists()


def test_calibrate_reports_clean_error_when_write_fails(isolated_omm_home, monkeypatch):
    filename = "model-1B-Q4.gguf"
    registry.save_registry(
        {
            filename: {
                "repo_id": "org/model",
                "size_bytes": 1024,
                "ollama_name": "model",
                "linked": {"ollama": True},
            }
        }
    )
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{"leaf": True, "value": 20.0}]})
    monkeypatch.setattr(
        cli.predictor, "predict_speed_interval", lambda *args, **kwargs: (20.0, 20.0, 20.0)
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)
    monkeypatch.setattr(
        cli.calibration,
        "record_calibration",
        lambda hardware, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = runner.invoke(cli.app, ["setting", "calibrate", filename])

    assert result.exit_code == 1
    assert "disk full" in result.stderr
