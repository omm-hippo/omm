from pathlib import Path

from typer.testing import CliRunner

from omm import cli, linker

runner = CliRunner()


def _stub_no_new_engines(monkeypatch):
    """Every engine added after LM Studio/Ollama defaults to not-installed,
    so these tests stay deterministic regardless of what's actually
    installed on the machine running them."""
    for key in ("jan", "anythingllm", "mstystudio", "textgenwebui", "koboldcpp"):
        monkeypatch.setattr(linker, f"is_{key}_installed", lambda: False)


def test_autoremove_reports_zero_when_nothing_broken(isolated_omm_home, monkeypatch):
    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(linker, "autoremove_lmstudio", lambda: 0)
    monkeypatch.setattr(linker, "autoremove_ollama", lambda: (0, 0))

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert "No broken symlinks found" in result.stdout


def test_autoremove_reports_counts_from_both_engines(isolated_omm_home, monkeypatch):
    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(linker, "autoremove_lmstudio", lambda: 2)
    monkeypatch.setattr(linker, "autoremove_ollama", lambda: (1, 1))

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert "2 broken LM Studio link(s)" in result.stdout
    assert "1 broken Ollama link(s)" in result.stdout


def test_autoremove_cleans_up_orphaned_part_and_gguf_files(isolated_omm_home, monkeypatch):
    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    orphan_part = cli.MODELS_DIR / "orphan.gguf.part"
    orphan_part.write_bytes(b"partial")
    orphan_full = cli.MODELS_DIR / "orphan2.gguf"
    orphan_full.write_bytes(b"complete")

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert "2" in result.stdout
    assert not orphan_part.exists()
    assert not orphan_full.exists()


def test_autoremove_cleans_nested_partial_and_resume_metadata(
    isolated_omm_home, monkeypatch
):
    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    nested = cli.MODELS_DIR / "quantized"
    nested.mkdir(parents=True)
    orphan_part = nested / "orphan.gguf.part"
    orphan_meta = nested / "orphan.gguf.part.meta"
    orphan_part.write_bytes(b"partial")
    orphan_meta.write_text("{}")

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert not orphan_part.exists()
    assert not orphan_meta.exists()
    assert not nested.exists()


def test_autoremove_leaves_registered_files_alone(isolated_omm_home, monkeypatch):
    from omm import registry

    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    kept = cli.MODELS_DIR / "kept.gguf"
    kept.write_bytes(b"data")
    registry.save_registry({"kept.gguf": {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert kept.exists()


def test_autoremove_skips_uninstalled_engines(isolated_omm_home, monkeypatch):
    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    lmstudio_calls = []
    ollama_calls = []
    monkeypatch.setattr(linker, "autoremove_lmstudio", lambda: lmstudio_calls.append(1) or 0)
    monkeypatch.setattr(linker, "autoremove_ollama", lambda: ollama_calls.append(1) or (0, 0))

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert lmstudio_calls == []
    assert ollama_calls == []


def test_autoremove_tolerates_permission_error_removing_incomplete_file(isolated_omm_home, monkeypatch):
    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    orphan = cli.MODELS_DIR / "orphan.gguf"
    orphan.write_bytes(b"junk")

    real_unlink = Path.unlink

    def _flaky_unlink(self, missing_ok=False):
        if self == orphan:
            raise OSError("permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert orphan.exists()
