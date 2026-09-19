import json
import platform
from pathlib import Path

import pytest
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


def test_doctor_prints_unique_remediations_after_the_summary(monkeypatch):
    monkeypatch.setattr(doctor, "read_theme_read_only", lambda: "dark")
    remediation = doctor.DoctorRemediation(
        "Start Ollama, then run omm doctor again.",
        ("omm", "link", "model.gguf", "--engine", "ollama"),
    )
    monkeypatch.setattr(
        doctor,
        "collect_report",
        lambda **kwargs: _report(
            doctor.DoctorCheck("WARN", "Ollama server", "not running", remediation),
            doctor.DoctorCheck("WARN", "Ollama tags", "not checked", remediation),
        ),
    )

    result = runner.invoke(cli.app, ["doctor", "--no-color"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.index("Overall: WARN") < result.stdout.index("How to fix")
    assert result.stdout.count("Start Ollama, then run omm doctor again.") == 1
    assert result.stdout.count("omm link model.gguf --engine ollama") == 1
    assert "No changes were made" in result.stdout


def test_doctor_json_includes_structured_remediation_arguments(monkeypatch):
    monkeypatch.setattr(doctor, "read_theme_read_only", lambda: "dark")
    monkeypatch.setattr(
        doctor,
        "collect_report",
        lambda **kwargs: _report(
            doctor.DoctorCheck(
                "WARN",
                "Ollama tag: model with space.gguf",
                "not visible",
                doctor.DoctorRemediation(
                    "Repair the link.",
                    ("omm", "link", "model with space.gguf", "--engine", "ollama"),
                ),
            )
        ),
    )

    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["checks"][0]["remediation"] == {
        "message": "Repair the link.",
        "command": ["omm", "link", "model with space.gguf", "--engine", "ollama"],
    }


def test_pass_check_rejects_a_remediation():
    with pytest.raises(ValueError, match="PASS doctor checks"):
        doctor.DoctorCheck(
            "PASS",
            "healthy",
            "ok",
            doctor.DoctorRemediation("No action should be attached."),
        )


def test_remediation_command_is_quoted_only_at_the_display_boundary(monkeypatch):
    remediation = doctor.DoctorRemediation(
        "Repair the link.",
        ("omm", "link", "model with space.gguf", "--engine", "ollama"),
    )

    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    assert remediation.display_command() == (
        "omm link 'model with space.gguf' --engine ollama"
    )
    assert remediation.as_dict()["command"] == [
        "omm",
        "link",
        "model with space.gguf",
        "--engine",
        "ollama",
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


def test_collect_report_gives_registry_repair_guidance_without_a_delete_command(
    monkeypatch, tmp_path
):
    registry_path = tmp_path / "models.json"
    registry_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(doctor.config, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(doctor, "_installation_checks", lambda *args: [])
    monkeypatch.setattr(doctor, "_ollama_checks", lambda registry: [])
    monkeypatch.setattr("omm.install_state.pending_records", lambda: [])
    monkeypatch.setattr("omm.runtime_profiles.interrupted_runs", lambda: [])

    report = doctor.collect_report(
        module_path=tmp_path / "cli.py", command_path=tmp_path / "omm"
    )

    registry = next(check for check in report.checks if check.name == "registry")
    assert registry.status == "FAIL"
    assert registry.remediation is not None
    assert "Back up" in registry.remediation.message
    assert "Do not delete" in registry.remediation.message
    assert registry.remediation.command == ()


def test_find_pipx_uses_executable_fallback_outside_path(monkeypatch, tmp_path):
    pipx = tmp_path / "user-scripts" / "pipx"
    pipx.parent.mkdir()
    pipx.write_text("#!/bin/sh\n", encoding="utf-8")
    pipx.chmod(0o755)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_pipx_candidate_paths", lambda: (pipx,))

    assert doctor._find_pipx() == pipx


def test_pipx_candidates_use_the_interpreter_user_scripts_scheme():
    """{USER_BASE}\\Scripts does not exist on Windows - CPython's nt_user
    scheme is {USER_BASE}\\Python<XY>\\Scripts - so the candidate list must
    come from sysconfig, not from a hand-built path."""
    import os
    import sysconfig

    expected = Path(sysconfig.get_path("scripts", f"{os.name}_user"))
    names = {os.path.normcase(str(p.parent.absolute())) for p in doctor._pipx_candidate_paths()}
    assert os.path.normcase(str(expected.absolute())) in names


def test_pipx_candidates_keep_the_local_bin_fallback():
    executable = "pipx.exe" if platform.system() == "Windows" else "pipx"
    assert (Path.home() / ".local" / "bin" / executable) in doctor._pipx_candidate_paths()


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


def test_installation_checks_warn_not_fail_on_fork_origin_unknown_source(
    monkeypatch, tmp_path
):
    # A fork/mirror clone installed editable has an origin outside the
    # allowed-repository whitelist, so install_source() conservatively
    # reports UNKNOWN even though the environment is perfectly healthy.
    # That must surface as WARN (exit 0), never FAIL (exit 1).
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
        lambda: doctor.package_metadata.InstallSource.UNKNOWN,
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

    installation_check = next(check for check in checks if check.name == "installation")
    assert installation_check.status == "WARN"
    assert all(check.status != "FAIL" for check in checks)


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
    assert version.remediation is not None
    assert version.remediation.command == ("pipx", "upgrade", "omm-model")


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
    assert legacy.remediation is not None
    assert legacy.remediation.command == (
        "omm",
        "link",
        "legacy.gguf",
        "--engine",
        "ollama",
    )
    assert missing.status == "WARN"
    assert "no Ollama runtime tag" in missing.detail
    assert missing.remediation is not None
    assert missing.remediation.command == (
        "omm",
        "link",
        "missing-tag.gguf",
        "--engine",
        "ollama",
    )
    assert all("intentionally-unlinked" not in check.name for check in checks)


def test_registered_ollama_tags_resolve_every_entry_in_one_batch(monkeypatch):
    """Single-entry resolution re-walks the Ollama manifest tree per legacy
    entry (issue #181); doctor iterates the whole registry, so it must use
    the batch API like cli.py does."""
    calls = []

    def fake_batch(entries, **kwargs):
        entries = list(entries)
        calls.append([name for name, _entry in entries])
        return {name: "qwen3:4b" for name, _entry in entries}

    monkeypatch.setattr(doctor.linker, "resolve_ollama_runtime_names_batch", fake_batch)

    def _boom(*a, **k):
        raise AssertionError("doctor must not use the per-entry resolver")

    monkeypatch.setattr(doctor.linker, "resolve_ollama_runtime_name", _boom)
    entries = {
        "qwen3-4b.gguf": {"linked": {"ollama": True}, "ollama_name": "qwen3-4b", "sha256": "a" * 64},
        "other-4b.gguf": {"linked": {"ollama": True}, "ollama_name": "other-4b", "sha256": "b" * 64},
    }

    mappings = doctor._registered_ollama_tags(entries)

    assert mappings == [
        ("qwen3-4b.gguf", "qwen3-4b", "qwen3:4b"),
        ("other-4b.gguf", "other-4b", "qwen3:4b"),
    ]
    assert calls == [["qwen3-4b.gguf", "other-4b.gguf"]]  # exactly one batch call


def test_registered_ollama_tags_fall_back_to_stored_names_when_the_batch_fails(monkeypatch):
    def fake_batch(entries, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(doctor.linker, "resolve_ollama_runtime_names_batch", fake_batch)
    entry = {
        "linked": {"ollama": True},
        "ollama_name": "stored",
        "ollama_runtime_name": " qwen3:4b ",
        "sha256": "a" * 64,
    }

    mappings = doctor._registered_ollama_tags({"f.gguf": entry})

    assert mappings == [("f.gguf", "stored", "qwen3:4b")]


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
    assert server.remediation is not None
    assert tags.status == "WARN"
    assert "not checked" in tags.detail
    assert tags.remediation is None
