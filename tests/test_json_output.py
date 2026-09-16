import json

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.json_output import write_document


def test_json_preserves_unicode_and_long_values_without_ansi(capsys):
    value = {"model": "모델" * 120, "ok": True}
    write_document(value)
    output = capsys.readouterr()
    assert json.loads(output.out) == value
    assert "\x1b" not in output.out


def test_invalid_json_number_leaves_no_partial_output(capsys):
    with pytest.raises(ValueError):
        write_document({"speed": float("nan")})
    assert capsys.readouterr().out == ""


def test_missing_model_has_one_structured_error(isolated_omm_home):
    result = CliRunner().invoke(cli.app, ["info", "missing.gguf", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["exit_code"] == 1


def test_benchmark_interrupt_is_structured(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_select_benchmark_engine", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    result = CliRunner().invoke(cli.app, ["benchmark", "sample", "--json"])
    assert result.exit_code == 130
    assert json.loads(result.stdout)["status"] == "cancelled"


def test_unexpected_failure_keeps_the_exception_and_structured_output(monkeypatch):
    def broken_scan():
        raise OSError("hardware query failed")
    monkeypatch.setattr(cli, "scan_hardware", broken_scan)
    result = CliRunner().invoke(cli.app, ["scan", "--json"])
    assert result.exit_code == 1
    assert isinstance(result.exception, OSError)
    assert json.loads(result.stdout)["error"]["exit_code"] == 1


def test_late_failure_does_not_append_a_second_json_document(monkeypatch):
    def broken_scan():
        cli._print_json(data={"partial": True})
        raise OSError("late failure")
    monkeypatch.setattr(cli, "scan_hardware", broken_scan)
    result = CliRunner().invoke(cli.app, ["scan", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"partial": True}
