import json
from pathlib import Path

from typer.testing import CliRunner

from omm import cli, doctor
from omm.engines.base import JsonResponse, RuntimeHealth


runner = CliRunner()


def _report(*checks):
    return doctor.DoctorReport(tuple(checks))


def test_doctor_root_skips_every_mutating_prelude(monkeypatch):
    monkeypatch.setattr(doctor, "read_theme_read_only", lambda: "dark")
    monkeypatch.setattr(
        doctor,
        "collect_report",
        lambda **kwargs: _report(
            doctor.DoctorCheck("PASS", "installation", "read-only fixture")
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("doctor must not run a mutating root prelude")

    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "_maybe_start_update_check", forbidden)
    monkeypatch.setattr(cli, "_maybe_run_onboarding", forbidden)
    monkeypatch.setattr(cli, "_maybe_auto_import", forbidden)
    monkeypatch.setattr(cli.telemetry, "flush_pending", forbidden)
    monkeypatch.setattr(cli.error_report, "flush_pending", forbidden)

    result = runner.invoke(cli.app, ["doctor", "--no-color"])

    assert result.exit_code == 0, result.stdout
    assert "PASS" in result.stdout
    assert "read-only fixture" in result.stdout


def test_doctor_json_is_machine_readable_and_supported_before_or_after_command(
    monkeypatch,
):
    monkeypatch.setattr(doctor, "read_theme_read_only", lambda: "dark")
    monkeypatch.setattr(
        doctor,
        "collect_report",
        lambda **kwargs: _report(
            doctor.DoctorCheck("WARN", "ollama server", "not running")
        ),
    )

    for args in (["--json", "doctor"], ["doctor", "--json"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.stdout
        assert "has no effect" not in result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "WARN"
        assert payload["checks"] == [
            {"status": "WARN", "name": "ollama server", "detail": "not running"}
        ]


def test_doctor_exits_nonzero_only_for_definite_failures(monkeypatch):
    monkeypatch.setattr(doctor, "read_theme_read_only", lambda: "dark")
    reports = iter(
        [
            _report(doctor.DoctorCheck("WARN", "ollama server", "not running")),
            _report(doctor.DoctorCheck("FAIL", "registry", "invalid JSON")),
        ]
    )
    monkeypatch.setattr(doctor, "collect_report", lambda **kwargs: next(reports))

    warning = runner.invoke(cli.app, ["doctor", "--no-color"])
    failure = runner.invoke(cli.app, ["doctor", "--no-color"])

    assert warning.exit_code == 0, warning.stdout
    assert failure.exit_code == 1, failure.stdout
    assert "FAIL" in failure.stdout


def test_read_registry_missing_does_not_create_omm_home(tmp_path):
    registry_path = tmp_path / ".omm" / "models.json"

    registry, error = doctor._read_registry_read_only(registry_path)

    assert registry == {}
    assert error is None
    assert not registry_path.parent.exists()


def test_read_theme_missing_or_corrupt_never_creates_or_repairs_config(
    monkeypatch, tmp_path
):
    config_path = tmp_path / ".omm" / "config.json"
    monkeypatch.setattr(doctor.config, "CONFIG_PATH", config_path)

    assert doctor.read_theme_read_only() == "dark"
    assert not config_path.parent.exists()

    config_path.parent.mkdir()
    config_path.write_text("{not json", encoding="utf-8")
    before = config_path.read_bytes()

    assert doctor.read_theme_read_only() == "dark"
    assert config_path.read_bytes() == before
    assert list(config_path.parent.iterdir()) == [config_path]


def test_read_registry_corruption_is_reported_without_backup_or_rewrite(tmp_path):
    registry_path = tmp_path / "models.json"
    registry_path.write_text("{not json", encoding="utf-8")
    before = registry_path.read_bytes()

    registry, error = doctor._read_registry_read_only(registry_path)

    assert registry is None
    assert "invalid JSON" in error
    assert registry_path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [registry_path]


def test_find_pipx_uses_executable_fallback_outside_path(monkeypatch, tmp_path):
    pipx = tmp_path / "user-scripts" / "pipx"
    pipx.parent.mkdir()
    pipx.write_text("#!/bin/sh\n", encoding="utf-8")
    pipx.chmod(0o755)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_pipx_candidate_paths", lambda: (pipx,))

    assert doctor._find_pipx() == pipx


def test_installation_checks_verify_editable_source_commit_and_module(monkeypatch, tmp_path):
    source = tmp_path / "source"
    module_path = source / "src" / "omm" / "cli.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("# fixture\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "omm-model"\nversion = "0.2.148"\n',
        encoding="utf-8",
    )
    command_path = tmp_path / "bin" / "omm"
    command_path.parent.mkdir()
    command_path.write_text("#!/bin/sh\n", encoding="utf-8")
    command_path.chmod(0o755)

    monkeypatch.setattr(
        doctor.package_metadata,
        "install_source",
        lambda: doctor.package_metadata.InstallSource.PIPX,
    )
    monkeypatch.setattr(doctor.package_metadata, "version", lambda: "0.2.148")
    monkeypatch.setattr(
        doctor.package_metadata,
        "direct_url",
        lambda: {"url": source.as_uri(), "dir_info": {"editable": True}},
    )
    monkeypatch.setattr(doctor, "_git_head", lambda path: "deadbeef" * 5)
    monkeypatch.setattr(doctor, "_find_pipx", lambda: tmp_path / "pipx")
    monkeypatch.setattr(doctor, "_pipx_version", lambda path: "1.16.7")

    checks = doctor._installation_checks(module_path, command_path)

    assert all(check.status == "PASS" for check in checks)
    details = "\n".join(check.detail for check in checks)
    assert "0.2.148" in details
    assert str(source) in details
    assert "deadbee" in details
    assert "pipx 1.16.7" in details


def test_installation_checks_fail_when_editable_source_does_not_supply_running_module(
    monkeypatch, tmp_path
):
    source = tmp_path / "claimed-source"
    source.mkdir()
    running_module = tmp_path / "different-source" / "src" / "omm" / "cli.py"
    running_module.parent.mkdir(parents=True)
    running_module.write_text("# fixture\n", encoding="utf-8")

    monkeypatch.setattr(
        doctor.package_metadata,
        "install_source",
        lambda: doctor.package_metadata.InstallSource.PIPX,
    )
    monkeypatch.setattr(doctor.package_metadata, "version", lambda: "0.2.148")
    monkeypatch.setattr(
        doctor.package_metadata,
        "direct_url",
        lambda: {"url": source.as_uri(), "dir_info": {"editable": True}},
    )
    monkeypatch.setattr(doctor, "_find_pipx", lambda: None)

    checks = doctor._installation_checks(running_module, Path("/tmp/omm"))

    source_check = next(check for check in checks if check.name == "editable source")
    assert source_check.status == "FAIL"
    assert "does not contain the running OMM module" in source_check.detail


def test_installation_checks_warn_on_partial_update_version_mismatch(
    monkeypatch, tmp_path
):
    source = tmp_path / "source"
    module_path = source / "src" / "omm" / "cli.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("# fixture\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "omm-model"\nversion = "0.2.149"\n',
        encoding="utf-8",
    )
    command_path = tmp_path / "omm"
    command_path.write_text("#!/bin/sh\n", encoding="utf-8")
    command_path.chmod(0o755)

    monkeypatch.setattr(
        doctor.package_metadata,
        "install_source",
        lambda: doctor.package_metadata.InstallSource.PIPX,
    )
    monkeypatch.setattr(doctor.package_metadata, "version", lambda: "0.2.148")
    monkeypatch.setattr(
        doctor.package_metadata,
        "direct_url",
        lambda: {"url": source.as_uri(), "dir_info": {"editable": True}},
    )
    monkeypatch.setattr(doctor, "_git_head", lambda path: "a" * 40)
    monkeypatch.setattr(doctor, "_find_pipx", lambda: tmp_path / "pipx")
    monkeypatch.setattr(doctor, "_pipx_version", lambda path: "1.16.7")

    checks = doctor._installation_checks(module_path, command_path)

    version = next(check for check in checks if check.name == "version agreement")
    assert version.status == "WARN"
    assert "package metadata=0.2.148" in version.detail
    assert "editable source=0.2.149" in version.detail


def test_ollama_checks_compare_saved_runtime_tags_with_actual_api_tags(monkeypatch):
    class _FakeAdapter:
        def health(self):
            return RuntimeHealth(True, version="0.30.10")

    monkeypatch.setattr(doctor.linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(
        doctor.linker, "find_ollama_executable", lambda: Path("/opt/homebrew/bin/ollama")
    )
    monkeypatch.setattr(doctor, "OllamaAdapter", _FakeAdapter)
    monkeypatch.setattr(doctor, "_ollama_api_tags", lambda: {"qwen3:4b"})
    registry = {
        "fixed.gguf": {
            "linked": {"ollama": True},
            "ollama_name": "qwen3-4b",
            "ollama_runtime_name": "qwen3:4b",
        },
        "legacy.gguf": {
            "linked": {"ollama": True},
            "ollama_name": "legacy-flat-tag",
        },
        "missing-tag.gguf": {"linked": {"ollama": True}},
        "intentionally-unlinked.gguf": {
            "linked": {"ollama": False},
            "ollama_name": "not-expected",
        },
    }

    checks = doctor._ollama_checks(registry)

    fixed = next(check for check in checks if check.name == "Ollama tag: fixed.gguf")
    legacy = next(check for check in checks if check.name == "Ollama tag: legacy.gguf")
    missing = next(
        check for check in checks if check.name == "Ollama tag: missing-tag.gguf"
    )
    assert fixed.status == "PASS"
    assert "stored=qwen3-4b" in fixed.detail
    assert "runtime=qwen3:4b" in fixed.detail
    assert legacy.status == "WARN"
    assert "not present in /api/tags" in legacy.detail
    assert missing.status == "WARN"
    assert "no Ollama runtime tag" in missing.detail
    assert all("intentionally-unlinked" not in check.name for check in checks)


def test_ollama_api_tags_uses_exact_read_only_tags_endpoint(monkeypatch):
    calls = []

    class _FakeClient:
        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return JsonResponse(
                {"models": [{"name": "qwen3:4b"}, {"model": "other:latest"}]},
                {},
            )

    monkeypatch.setattr(doctor, "LoopbackJsonClient", lambda base_url: _FakeClient())

    tags = doctor._ollama_api_tags()

    assert tags == {"qwen3:4b", "other:latest"}
    assert calls == [
        (
            "GET",
            "/api/tags",
            {"timeout": 10, "default_failure": "server_unavailable"},
        )
    ]


def test_ollama_server_unavailable_is_warning_and_skips_tag_listing(monkeypatch):
    class _FakeAdapter:
        def health(self):
            return RuntimeHealth(False, failure_reason="server_unavailable")

    monkeypatch.setattr(doctor.linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(doctor.linker, "find_ollama_executable", lambda: None)
    monkeypatch.setattr(doctor, "OllamaAdapter", _FakeAdapter)
    monkeypatch.setattr(
        doctor,
        "_ollama_api_tags",
        lambda: (_ for _ in ()).throw(AssertionError("must not list tags when health failed")),
    )

    checks = doctor._ollama_checks(
        {"model.gguf": {"linked": {"ollama": True}, "ollama_name": "model"}}
    )

    server = next(check for check in checks if check.name == "Ollama server")
    tags = next(check for check in checks if check.name == "Ollama tags")
    assert server.status == "WARN"
    assert tags.status == "WARN"
    assert "not checked" in tags.detail
