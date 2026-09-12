from pathlib import Path
from types import SimpleNamespace
import hashlib
import pytest

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


@pytest.mark.parametrize("method", ["hardlink", "copy"])
def test_upgrade_refreshes_registered_custom_destination(
    isolated_omm_home, tmp_path, monkeypatch, method
):
    host_system = cli.platform.system()
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)
    # Simulate only the link strategy. Mutating the shared platform module
    # also made the updater use Windows-only subprocess flags on POSIX CI.
    monkeypatch.setattr(cli.linker, "platform", SimpleNamespace(system=lambda: "Windows"))
    # A local venv installed from another checkout can skip update checks.
    # Exercise the editable-install branch on every host, without a real
    # background process or network request, so that cannot hide this leak.
    monkeypatch.setattr(cli, "_installed_commit", lambda: "a" * 40)
    monkeypatch.setattr(cli.version_check, "cached_remote_head_if_fresh", lambda *a: (False, None, None))
    monkeypatch.setattr(cli.version_check, "should_start_check", lambda *a: True)
    monkeypatch.setattr(cli.version_check, "mark_checking", lambda *a: True)
    spawned = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))
    if method == "copy":
        monkeypatch.setattr(Path, "hardlink_to", lambda *a, **k: (_ for _ in ()).throw(OSError("cross-drive")))
        monkeypatch.setattr(Path, "symlink_to", lambda *a, **k: (_ for _ in ()).throw(OSError("no privilege")))
    source = cli.MODELS_DIR / "model.gguf"
    source.write_bytes(b"old-model")
    registry.save_registry({source.name: _entry(repo_id=None)})
    target = tmp_path / "custom-app"
    linked = runner.invoke(cli.app, ["link", "--to", str(target)])
    assert linked.exit_code == 0, linked.output
    destination = target / source.name
    monkeypatch.setattr(cli, "download_file", lambda url, path, **kw: Path(path).write_bytes(b"new-model"))

    result = runner.invoke(cli.app, ["upgrade", source.name])

    assert result.exit_code == 0, result.output
    assert source.read_bytes() == destination.read_bytes() == b"new-model"
    assert registry.load_registry()[source.name]["sha256"] == hashlib.sha256(b"new-model").hexdigest()
    assert cli.platform.system() == host_system
    assert spawned
    for _args, kwargs in spawned:
        if host_system == "Windows":
            assert kwargs["creationflags"] == (
                cli.subprocess.DETACHED_PROCESS | cli.subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            assert kwargs["start_new_session"] is True
            assert "creationflags" not in kwargs


def test_upgrade_preserves_user_replacement_at_custom_destination(
    isolated_omm_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)
    source = cli.MODELS_DIR / "model.gguf"
    source.write_bytes(b"old-model")
    registry.save_registry({source.name: _entry(repo_id=None)})
    target = tmp_path / "custom-app"
    linked = runner.invoke(cli.app, ["link", "--to", str(target)])
    assert linked.exit_code == 0, linked.output
    destination = target / source.name
    destination.unlink()
    destination.write_bytes(b"user-owned-model")
    monkeypatch.setattr(cli, "download_file", lambda url, path, **kw: Path(path).write_bytes(b"new-model"))

    result = runner.invoke(cli.app, ["upgrade", source.name])

    assert result.exit_code == 0, result.output
    assert source.read_bytes() == b"new-model"
    assert destination.read_bytes() == b"user-owned-model"
    assert "could not be refreshed" in " ".join(result.stderr.split())


def test_upgrade_single_repo_model_up_to_date(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"old-bytes")
    same_hash = hashlib.sha256(b"old-bytes").hexdigest()
    registry.save_registry({"model.gguf": _entry(sha256=same_hash)})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: same_hash)

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "already up to date" in result.stdout


def test_upgrade_single_repo_model_redownloads_on_hash_mismatch(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"old-bytes")
    registry.save_registry({"model.gguf": _entry(sha256="old-hash")})
    expected = hashlib.sha256(b"new-bytes-from-upstream").hexdigest()
    monkeypatch.setattr(
        cli, "remote_file_sha256", lambda provider, repo_id, filename: expected
    )

    def fake_download(url, dest, **_kw):
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

    def fake_download(url, dest_path, **_kw):
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

    def fake_download(url, dest_path, **_kw):
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

    def fake_download(url, dest_path, **_kw):
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
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"same")
    same_hash = hashlib.sha256(b"same").hexdigest()
    registry.save_registry({"model.gguf": _entry(sha256=same_hash)})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: same_hash)

    result = runner.invoke(cli.app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "0 updated, 1 up to date, 0 skipped" in result.stdout


def test_upgrade_all_yes_flag_before_subcommand_skips_prompt(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"same")
    same_hash = hashlib.sha256(b"same").hexdigest()
    registry.save_registry({"model.gguf": _entry(sha256=same_hash)})
    monkeypatch.setattr(cli, "remote_file_sha256", lambda provider, repo_id, filename: same_hash)

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
            "same.gguf": _entry(
                sha256=hashlib.sha256(b"a").hexdigest(), repo_id="org/same"
            ),
            "changed.gguf": _entry(sha256="old-hash", repo_id="org/changed"),
        }
    )
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)

    def fake_remote_hash(provider, repo_id, filename):
        return (
            hashlib.sha256(b"a").hexdigest()
            if repo_id == "org/same"
            else hashlib.sha256(b"new-content").hexdigest()
        )

    monkeypatch.setattr(cli, "remote_file_sha256", fake_remote_hash)

    def fake_download(url, dest, **_kw):
        Path(dest).write_bytes(b"new-content")

    monkeypatch.setattr(cli, "download_file", fake_download)

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 0, result.stdout
    assert "1 updated, 1 up to date, 0 skipped" in result.stdout


def test_upgrade_repo_hash_mismatch_preserves_installed_file(
    isolated_omm_home, monkeypatch
):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"known-good")
    registry.save_registry({"model.gguf": _entry(sha256="old-hash")})
    monkeypatch.setattr(
        cli, "remote_file_sha256", lambda provider, repo_id, filename: "expected-hash"
    )
    monkeypatch.setattr(
        cli, "download_file", lambda url, path, **_kw: Path(path).write_bytes(b"tampered")
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0
    assert "does not match provider metadata" in result.stderr
    assert dest.read_bytes() == b"known-good"
    assert not (cli.MODELS_DIR / "model.gguf.update").exists()


def test_upgrade_preserves_installed_file_when_link_volume_has_no_capacity(
    isolated_omm_home, monkeypatch
):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"known-good")
    registry.save_registry({"model.gguf": _entry(sha256="old-hash")})
    expected = hashlib.sha256(b"new-content").hexdigest()
    monkeypatch.setattr(cli, "remote_file_sha256", lambda *args: expected)
    monkeypatch.setattr(
        cli,
        "download_file",
        lambda url, path, **kwargs: Path(path).write_bytes(b"new-content"),
    )
    monkeypatch.setattr(
        cli,
        "_ensure_install_disk_capacity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            cli.InsufficientDiskSpaceError("copy volume is full")
        ),
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert "installed file was preserved" in " ".join(result.stderr.split())
    assert dest.read_bytes() == b"known-good"
    assert not (cli.MODELS_DIR / "model.gguf.update").exists()


def test_upgrade_repairs_tampered_local_file_even_when_registry_matches_remote(
    isolated_omm_home, monkeypatch
):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"tampered")
    expected = hashlib.sha256(b"known-good").hexdigest()
    registry.save_registry({"model.gguf": _entry(sha256=expected)})
    monkeypatch.setattr(
        cli, "remote_file_sha256", lambda provider, repo_id, filename: expected
    )
    monkeypatch.setattr(
        cli,
        "download_file",
        lambda url, path, **_kw: Path(path).write_bytes(b"known-good"),
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert dest.read_bytes() == b"known-good"
    assert "updated to" in result.stdout


def test_upgrade_direct_url_repairs_tampered_local_file(
    isolated_omm_home, monkeypatch
):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"tampered")
    expected = hashlib.sha256(b"known-good").hexdigest()
    registry.save_registry(
        {"model.gguf": _entry(repo_id=None, sha256=expected)}
    )
    monkeypatch.setattr(
        cli,
        "download_file",
        lambda url, path, **_kw: Path(path).write_bytes(b"known-good"),
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert dest.read_bytes() == b"known-good"
    assert "updated to" in result.stdout


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
