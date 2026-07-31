from pathlib import Path

from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def test_uninstall_all_removes_every_registered_model(isolated_omm_home, monkeypatch):
    for filename in ("a.gguf", "b.gguf"):
        (cli.MODELS_DIR / filename).write_bytes(b"fake-gguf")
    registry.save_registry(
        {
            "a.gguf": {"linked": {"lmstudio": False, "ollama": False}},
            "b.gguf": {"linked": {"lmstudio": False, "ollama": False}},
        }
    )
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)

    result = runner.invoke(cli.app, ["uninstall", "all"])

    assert result.exit_code == 0, result.stdout
    assert registry.load_registry() == {}
    assert not (cli.MODELS_DIR / "a.gguf").exists()
    assert not (cli.MODELS_DIR / "b.gguf").exists()


def test_uninstall_all_yes_flag_skips_prompt_without_a_tty(isolated_omm_home, monkeypatch):
    (cli.MODELS_DIR / "a.gguf").write_bytes(b"fake-gguf")
    registry.save_registry({"a.gguf": {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["uninstall", "all", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert registry.load_registry() == {}


def test_uninstall_all_yes_flag_before_subcommand_skips_prompt(isolated_omm_home, monkeypatch):
    (cli.MODELS_DIR / "a.gguf").write_bytes(b"fake-gguf")
    registry.save_registry({"a.gguf": {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["--yes", "uninstall", "all"])

    assert result.exit_code == 0, result.stdout
    assert registry.load_registry() == {}


def test_uninstall_all_without_yes_errors_without_a_tty(isolated_omm_home):
    registry.save_registry({"a.gguf": {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["uninstall", "all"])

    assert result.exit_code == 1
    assert registry.load_registry() != {}


def test_uninstall_all_cancelled_leaves_registry_untouched(isolated_omm_home, monkeypatch):
    registry.save_registry({"a.gguf": {"linked": {"lmstudio": False, "ollama": False}}})
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: False)

    result = runner.invoke(cli.app, ["uninstall", "all"])

    assert result.exit_code == 0, result.stdout
    assert registry.load_registry() != {}


def test_uninstall_all_with_empty_registry_reports_nothing_to_do(isolated_omm_home):
    result = runner.invoke(cli.app, ["uninstall", "all"])

    assert result.exit_code == 0, result.stdout
    assert "No models installed" in result.stdout


def test_uninstall_all_dry_run_reports_without_removing(isolated_omm_home, monkeypatch):
    for filename in ("a.gguf", "b.gguf"):
        (cli.MODELS_DIR / filename).write_bytes(b"fake-gguf")
    registry.save_registry(
        {
            "a.gguf": {"linked": {"lmstudio": False, "ollama": False}},
            "b.gguf": {"linked": {"lmstudio": False, "ollama": False}},
        }
    )
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda message, default=False: (_ for _ in ()).throw(AssertionError("should not prompt"))
    )

    result = runner.invoke(cli.app, ["uninstall", "all", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Would uninstall: a.gguf" in result.stdout
    assert "Would uninstall: b.gguf" in result.stdout
    assert set(registry.load_registry().keys()) == {"a.gguf", "b.gguf"}
    assert (cli.MODELS_DIR / "a.gguf").exists()
    assert (cli.MODELS_DIR / "b.gguf").exists()


def test_uninstall_single_dry_run_reports_without_removing(isolated_omm_home):
    filename = "model.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")
    registry.save_registry({filename: {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["uninstall", filename, "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert f"Would uninstall: {filename}" in result.stdout
    assert filename in registry.load_registry()
    assert dest.exists()


def test_remove_accepts_filename_without_gguf_suffix(isolated_omm_home):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")
    registry.save_registry({filename: {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["uninstall", "tinyllama-1.1b-chat-v1.0.Q4_K_M"])

    assert result.exit_code == 0, result.stdout
    assert f"Removed {filename}" in result.stdout
    assert registry.load_registry() == {}
    assert not dest.exists()


def test_remove_cleans_up_orphaned_part_file(isolated_omm_home):
    part = cli.MODELS_DIR / "orphan.gguf.part"
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"partial")

    result = runner.invoke(cli.app, ["uninstall", "orphan.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "orphan.gguf" in result.stdout
    assert not part.exists()


def test_remove_cleans_up_unregistered_complete_download(isolated_omm_home):
    dest = cli.MODELS_DIR / "orphan.gguf"
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"complete-but-unregistered")

    result = runner.invoke(cli.app, ["uninstall", "orphan.gguf"])

    assert result.exit_code == 0, result.stdout
    assert not dest.exists()


def test_uninstall_unregistered_part_dry_run_does_not_delete(isolated_omm_home, monkeypatch):
    part = cli.MODELS_DIR / "ghost.gguf.part"
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"partial")

    def _fail_if_called(filename):
        raise AssertionError("dry-run must not invoke real cleanup")

    monkeypatch.setattr(cli, "_cleanup_incomplete_install", _fail_if_called)

    result = runner.invoke(cli.app, ["uninstall", "ghost.gguf", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Would clean up incomplete install of ghost.gguf" in result.stdout
    assert part.exists()


def test_uninstall_unregistered_complete_download_dry_run_does_not_delete(isolated_omm_home, monkeypatch):
    dest = cli.MODELS_DIR / "ghost.gguf"
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"complete-but-unregistered")

    def _fail_if_called(filename):
        raise AssertionError("dry-run must not invoke real cleanup")

    monkeypatch.setattr(cli, "_cleanup_incomplete_install", _fail_if_called)

    result = runner.invoke(cli.app, ["uninstall", "ghost.gguf", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Would clean up incomplete install of ghost.gguf" in result.stdout
    assert dest.exists()


def test_uninstall_unregistered_dry_run_still_errors_when_nothing_on_disk(isolated_omm_home):
    result = runner.invoke(cli.app, ["uninstall", "nothing-here.gguf", "--dry-run"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_remove_still_errors_when_nothing_on_disk(isolated_omm_home):
    result = runner.invoke(cli.app, ["uninstall", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_remove_does_not_clean_path_outside_model_hub(isolated_omm_home):
    victim = cli.MODELS_DIR.parent / "victim.gguf"
    victim.write_bytes(b"keep")

    result = runner.invoke(cli.app, ["uninstall", "../victim.gguf"])

    assert result.exit_code == 1
    assert victim.read_bytes() == b"keep"


def test_remove_collision_drops_only_requested_registry_key(
    isolated_omm_home,
):
    shared_path = cli.MODELS_DIR / "Model.gguf"
    shared_path.write_bytes(b"shared")
    registry.save_registry(
        {
            "Model.gguf": {"linked": {}},
            "model.gguf": {"linked": {}},
        }
    )

    result = runner.invoke(cli.app, ["uninstall", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert shared_path.read_bytes() == b"shared"
    assert set(registry.load_registry()) == {"Model.gguf"}


def test_uninstall_tolerates_permission_error_removing_model_file(isolated_omm_home, monkeypatch):
    filename = "a.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")
    registry.save_registry({filename: {"linked": {"lmstudio": False, "ollama": False}}})

    real_unlink = Path.unlink

    def _flaky_unlink(self, missing_ok=False):
        if self == dest:
            raise OSError("permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    result = runner.invoke(cli.app, ["uninstall", filename])

    assert result.exit_code == 0, result.stdout
    assert filename not in registry.load_registry()
