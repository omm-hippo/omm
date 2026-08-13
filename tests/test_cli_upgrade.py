from pathlib import Path

from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def _entry(**overrides):
    entry = {
        "sha256": "old-hash",
        "version": "old-has",
        "source": "https://huggingface.co/org/repo/resolve/main/model.gguf",
        "size_bytes": 9,
        "installed_at": "2026-07-19T00:00:00+00:00",
        "repo_id": "org/repo",
        "ollama_name": "model",
        "linked": {"lmstudio": False, "ollama": False},
    }
    entry.update(overrides)
    return entry


def _no_engines(monkeypatch):
    monkeypatch.setattr(cli.linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_ollama_installed", lambda: False)


def test_upgrade_single_repo_model_up_to_date(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"old-bytes")
    registry.save_registry({"model.gguf": _entry(sha256="same-hash")})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: "same-hash")

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "already up to date" in result.stdout


def test_upgrade_single_repo_model_redownloads_on_hash_mismatch(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"old-bytes")
    registry.save_registry({"model.gguf": _entry(sha256="old-hash")})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: "new-hash")

    def fake_download(url, dest):
        Path(dest).write_bytes(b"new-bytes-from-upstream")

    monkeypatch.setattr(cli, "download_file", fake_download)

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "updated to" in result.stdout
    assert (cli.MODELS_DIR / "model.gguf").read_bytes() == b"new-bytes-from-upstream"
    updated = registry.load_registry()["model.gguf"]
    assert updated["sha256"] != "old-hash"
    assert updated["version"] == updated["sha256"][:7]


def test_upgrade_skips_when_remote_hash_unknown(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"old-bytes")
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: None)
    monkeypatch.setattr(
        cli, "download_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download"))
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "could not check for updates" in result.stderr
    assert registry.load_registry()["model.gguf"]["sha256"] == "old-hash"


def test_upgrade_direct_url_install_matches_hash_leaves_file_untouched(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"same-bytes")
    import hashlib

    same_hash = hashlib.sha256(b"same-bytes").hexdigest()
    registry.save_registry({"model.gguf": _entry(repo_id=None, sha256=same_hash)})

    def fake_download(url, dest_path):
        Path(dest_path).write_bytes(b"same-bytes")

    monkeypatch.setattr(cli, "download_file", fake_download)

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "already up to date" in result.stdout
    assert dest.read_bytes() == b"same-bytes"
    assert not (cli.MODELS_DIR / "model.gguf.update").exists()


def test_upgrade_direct_url_install_swaps_in_new_file_atomically(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"old-bytes")
    registry.save_registry({"model.gguf": _entry(repo_id=None, sha256="old-hash")})

    def fake_download(url, dest_path):
        Path(dest_path).write_bytes(b"brand-new-bytes")

    monkeypatch.setattr(cli, "download_file", fake_download)

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "updated to" in result.stdout
    assert dest.read_bytes() == b"brand-new-bytes"
    assert not (cli.MODELS_DIR / "model.gguf.update").exists()


def test_upgrade_direct_url_install_reports_skipped_when_finalize_fails(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"old-bytes")
    registry.save_registry({"model.gguf": _entry(repo_id=None, sha256="old-hash")})

    def fake_download(url, dest_path):
        Path(dest_path).write_bytes(b"brand-new-bytes")

    monkeypatch.setattr(cli, "download_file", fake_download)
    monkeypatch.setattr(
        Path, "replace", lambda self, target: (_ for _ in ()).throw(OSError("permission denied"))
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "failed to finalize" in result.stderr
    assert dest.read_bytes() == b"old-bytes"
    assert not (cli.MODELS_DIR / "model.gguf.update").exists()


def test_upgrade_all_confirmation_cancelled_leaves_registry_untouched(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: False)
    monkeypatch.setattr(
        cli, "remote_file_sha256", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not check"))
    )

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 0, result.stdout
    assert "Cancelled" in result.stderr
    assert registry.load_registry()["model.gguf"]["sha256"] == "old-hash"


def test_upgrade_all_yes_flag_skips_prompt_without_a_tty(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    registry.save_registry({"model.gguf": _entry(sha256="same-hash")})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: "same-hash")

    result = runner.invoke(cli.app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "0 updated, 1 up to date, 0 skipped" in result.stdout


def test_upgrade_all_yes_flag_before_subcommand_skips_prompt(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    registry.save_registry({"model.gguf": _entry(sha256="same-hash")})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: "same-hash")

    result = runner.invoke(cli.app, ["--yes", "upgrade"])

    assert result.exit_code == 0, result.stdout
    assert "0 updated, 1 up to date, 0 skipped" in result.stdout


def test_upgrade_all_without_yes_errors_without_a_tty(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 1


def test_upgrade_all_reports_summary_counts(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "same.gguf").write_bytes(b"a")
    (cli.MODELS_DIR / "changed.gguf").write_bytes(b"b")
    registry.save_registry(
        {
            "same.gguf": _entry(sha256="same-hash", repo_id="org/same"),
            "changed.gguf": _entry(sha256="old-hash", repo_id="org/changed"),
        }
    )
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)

    def fake_remote_hash(provider, repo_id, filename):
        return "same-hash" if repo_id == "org/same" else "new-hash"

    monkeypatch.setattr(cli, "remote_file_sha256", fake_remote_hash)

    def fake_download(url, dest):
        Path(dest).write_bytes(b"new-content")

    monkeypatch.setattr(cli, "download_file", fake_download)

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 0, result.stdout
    assert "1 updated, 1 up to date, 0 skipped" in result.stdout


def test_upgrade_all_with_empty_registry_reports_nothing_to_do(isolated_omm_home):
    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 0, result.stdout
    assert "No models installed" in result.stdout


def test_upgrade_errors_for_uninstalled_model(isolated_omm_home):
    result = runner.invoke(cli.app, ["upgrade", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_upgrade_all_dry_run_reports_without_checking(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    registry.save_registry(
        {
            "a.gguf": _entry(repo_id="org/a"),
            "b.gguf": _entry(repo_id="org/b"),
        }
    )
    monkeypatch.setattr(
        cli,
        "remote_file_sha256",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not check")),
    )

    result = runner.invoke(cli.app, ["upgrade", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Would check for updates: a.gguf" in result.stdout
    assert "Would check for updates: b.gguf" in result.stdout
    assert registry.load_registry()["a.gguf"]["sha256"] == "old-hash"


def test_upgrade_single_model_dry_run_reports_without_checking(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    registry.save_registry({"model.gguf": _entry(repo_id="org/repo")})
    monkeypatch.setattr(
        cli,
        "remote_file_sha256",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not check")),
    )
    calls = []
    monkeypatch.setattr(cli, "_update_one", lambda filename, entry: calls.append(filename))

    result = runner.invoke(cli.app, ["upgrade", "model.gguf", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Would check for updates: model.gguf" in result.stdout
    assert calls == []
    assert registry.load_registry()["model.gguf"]["sha256"] == "old-hash"
