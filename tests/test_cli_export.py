import json
from pathlib import Path

from typer.testing import CliRunner

from omm import cli, registry, scan_import

runner = CliRunner()


def test_export_places_file_in_destination(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    source = cli.MODELS_DIR / filename
    source.write_bytes(b"model-bytes")
    registry.save_registry({filename: {"linked": {}}})
    destination = tmp_path / "out"

    result = runner.invoke(cli.app, ["export", filename, str(destination)])

    assert result.exit_code == 0, result.stdout
    exported = destination / filename
    assert exported.exists()
    assert exported.read_bytes() == b"model-bytes"
    assert not exported.is_symlink()


def test_export_does_not_touch_registry(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model-bytes")
    registry.save_registry({filename: {"linked": {}}})
    destination = tmp_path / "out"

    result = runner.invoke(cli.app, ["export", filename, str(destination)])

    assert result.exit_code == 0, result.stdout
    entry = registry.load_registry()[filename]
    assert "custom_links" not in entry


def test_export_missing_model_errors(isolated_omm_home, tmp_path):
    result = runner.invoke(cli.app, ["export", "nothing-here.gguf", str(tmp_path / "out")])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_export_hub_file_missing_errors(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    registry.save_registry({filename: {"linked": {}}})

    result = runner.invoke(cli.app, ["export", filename, str(tmp_path / "out")])

    assert result.exit_code == 1
    assert "hub file is missing" in result.stderr


def test_export_refuses_unowned_conflicting_destination_without_force(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model-bytes")
    registry.save_registry({filename: {"linked": {}}})
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / filename).write_bytes(b"someone-elses-file")

    result = runner.invoke(cli.app, ["export", filename, str(destination)])

    assert result.exit_code == 1
    assert (destination / filename).read_bytes() == b"someone-elses-file"


def test_export_force_reclaims_conflicting_destination(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model-bytes")
    registry.save_registry({filename: {"linked": {}}})
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / filename).write_bytes(b"someone-elses-file")

    result = runner.invoke(cli.app, ["export", filename, str(destination), "--force"])

    assert result.exit_code == 0, result.stdout
    assert (destination / filename).read_bytes() == b"model-bytes"


def test_export_writes_a_provenance_manifest(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    source = cli.MODELS_DIR / filename
    source.write_bytes(b"model-bytes")
    registry.save_registry(
        {
            filename: {
                "linked": {},
                "sha256": scan_import.sha256_file(source),
                "repo_id": "org/repo",
                "source": "https://huggingface.co/org/repo",
                "version": "abcdef1",
                "installed_at": "2026-01-01T00:00:00+00:00",
                "size_bytes": source.stat().st_size,
            }
        }
    )
    destination = tmp_path / "out"

    result = runner.invoke(cli.app, ["export", filename, str(destination)])

    assert result.exit_code == 0, result.stdout
    manifest_path = destination / f"{filename}.omm-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["sha256"] == scan_import.sha256_file(source)
    assert manifest["repo_id"] == "org/repo"
    assert manifest["source"] == "https://huggingface.co/org/repo"
    assert manifest["version"] == "abcdef1"
    assert manifest["installed_at"] == "2026-01-01T00:00:00+00:00"


def test_export_manifest_survives_gguf_header_read_failure(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    source = cli.MODELS_DIR / filename
    source.write_bytes(b"not a real gguf header")
    registry.save_registry({filename: {"linked": {}, "sha256": scan_import.sha256_file(source)}})
    destination = tmp_path / "out"

    result = runner.invoke(cli.app, ["export", filename, str(destination)])

    assert result.exit_code == 0, result.stdout
    manifest_path = destination / f"{filename}.omm-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "architecture" not in manifest
    assert "parameter_count" not in manifest


def test_export_never_creates_a_symlink_even_when_hardlink_fails(isolated_omm_home, tmp_path, monkeypatch):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model-bytes")
    registry.save_registry({filename: {"linked": {}}})
    destination = tmp_path / "out"

    def _fail_hardlink(self, target):
        raise OSError("cross-device link (simulated)")

    monkeypatch.setattr(Path, "hardlink_to", _fail_hardlink)

    result = runner.invoke(cli.app, ["export", filename, str(destination)])

    assert result.exit_code == 0, result.stdout
    exported = destination / filename
    assert exported.exists()
    assert not exported.is_symlink()
    assert exported.read_bytes() == b"model-bytes"
