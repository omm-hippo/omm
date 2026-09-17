from __future__ import annotations

import json
from typer.testing import CliRunner

from omm import cli, config, doctor, support_report


def test_default_report_is_allowlisted_and_excludes_sensitive_material(
    isolated_omm_home, monkeypatch
):
    secret = "hf_SUPER_SECRET"
    model = "private-owner/private-model.gguf"
    personal = "/Users/alice/School/private"
    config.OMM_HOME.joinpath("error_reports_pending.json").write_text(
        json.dumps([
            {
                "error_type": "DownloadError",
                "error_message": f"{secret} {model} {personal}",
                "catalog_ref": model,
            }
        ]),
        encoding="utf-8",
    )
    logs = config.OMM_HOME / "logs"
    logs.mkdir()
    logs.joinpath("prior.jsonl").write_text(
        json.dumps({"argv": ["install", "<arg>"], "cwd": personal}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(support_report, "_version", lambda: "1.2.3")
    monkeypatch.setattr(support_report, "_install_source", lambda: "pipx")
    diagnostic = doctor.DoctorReport(
        (
            doctor.DoctorCheck("PASS", "installation", personal),
            doctor.DoctorCheck("WARN", "Ollama tag: secret-model", model),
        )
    )

    report = support_report.build(diagnostic)
    rendered = support_report.preview(report)

    assert report["command_name"] == "install"
    assert report["error_type"] == "DownloadError"
    assert report["diagnostics"] == {
        "status": "WARN",
        "counts": {"PASS": 1, "WARN": 1, "FAIL": 0},
    }
    for forbidden in (secret, model, personal, "secret-model", "error_message", "catalog_ref"):
        assert forbidden not in rendered


def test_optional_groups_are_only_added_when_selected(monkeypatch):
    monkeypatch.setattr(support_report, "latest_command_name", lambda: None)
    monkeypatch.setattr(support_report, "latest_error_type", lambda: None)
    diagnostic = doctor.DoctorReport((doctor.DoctorCheck("PASS", "registry", "private"),))
    plain = support_report.build(diagnostic)
    selected = support_report.build(diagnostic, include=["os", "checks"])
    assert "os" not in plain and "checks" not in plain
    assert "os" in selected and selected["checks"] == [{"name": "registry", "status": "PASS"}]


def test_cli_previews_before_local_save_and_never_uploads(
    isolated_omm_home, tmp_path, monkeypatch
):
    diagnostic = doctor.DoctorReport((doctor.DoctorCheck("PASS", "registry", "private"),))
    monkeypatch.setattr(cli.doctor_mod, "collect_report", lambda **kwargs: diagnostic)
    output = tmp_path / "support.json"
    result = CliRunner().invoke(
        cli.app, ["support-bundle", "--save", str(output), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "Complete local support-bundle preview" in result.output
    assert "No upload, GitHub issue, browser action, or message was created" in result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["diagnostics"]["status"] == "PASS"
