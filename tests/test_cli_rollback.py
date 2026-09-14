"""Tests for `omm pin` / `omm unpin` / `omm rollback` (issue #295).

`test_cli_upgrade.py` covers the archive-before-replace side (pinning a
model, then upgrading it) - this file covers pin/unpin bookkeeping and the
rollback swap itself, seeding the "already archived" state directly instead
of going through the download/upgrade machinery every time.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def _entry(**overrides):
    entry = {
        "sha256": "new-hash",
        "version": "new-has",
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


def _seed_pinned_model_with_archive(old_bytes: bytes, new_bytes: bytes, **entry_overrides):
    """Set up `dest` = new_bytes, the archive slot = old_bytes, and a
    registry entry describing that state - exactly what a pinned model's
    `omm upgrade` leaves behind, without going through the download/upgrade
    machinery to get there."""
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(new_bytes)
    old_sha = hashlib.sha256(old_bytes).hexdigest()
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    cli.MODEL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = cli.MODEL_ARCHIVE_DIR / "model.gguf"
    archive_path.write_bytes(old_bytes)
    entry = _entry(
        sha256=new_sha,
        version=new_sha[:7],
        pinned=True,
        archive={
            "sha256": old_sha,
            "version": old_sha[:7],
            "size_bytes": len(old_bytes),
            "installed_at": "2026-07-01T00:00:00+00:00",
            "archived_at": "2026-07-19T00:00:00+00:00",
        },
        **entry_overrides,
    )
    registry.save_registry({"model.gguf": entry})
    return dest, archive_path, old_sha, new_sha


# --- omm pin / omm unpin -------------------------------------------------


def test_unpin_clears_flag_and_deletes_archive(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    dest, archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )

    result = runner.invoke(cli.app, ["unpin", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert not archive_path.exists()
    entry = registry.load_registry()["model.gguf"]
    assert "pinned" not in entry
    assert "archive" not in entry
    assert dest.exists()  # unpin never touches the installed model itself
    assert dest.read_bytes() == b"new-bytes"


def test_unpin_unknown_model_errors(isolated_omm_home):
    result = runner.invoke(cli.app, ["unpin", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_unpin_not_pinned_is_a_no_op(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"bytes")
    registry.save_registry({"model.gguf": _entry(sha256=hashlib.sha256(b"bytes").hexdigest())})

    result = runner.invoke(cli.app, ["unpin", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert "isn't pinned" in result.stdout


# --- omm rollback ----------------------------------------------------------


def test_rollback_restores_archived_bytes_and_swaps_the_slot(isolated_omm_home, monkeypatch):
    """(g): rollback restores the archived bytes into `dest`, updates the
    registry sha/version/size to the restored (old) values, relinks via
    `_link_model`, and D4 (swap) leaves the replaced (new) bytes in the
    archive slot so a second rollback swaps forward again."""
    _no_engines(monkeypatch)
    dest, archive_path, old_sha, new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )

    real_link_model = cli._link_model
    link_calls = []

    def spy_link_model(dest_arg, repo_id, ollama_tag, **kw):
        link_calls.append((dest_arg, repo_id, ollama_tag))
        return real_link_model(dest_arg, repo_id, ollama_tag, **kw)

    monkeypatch.setattr(cli, "_link_model", spy_link_model)

    result = runner.invoke(cli.app, ["rollback", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert "rolled back to" in result.stdout
    assert dest.read_bytes() == b"old-bytes"
    assert archive_path.read_bytes() == b"new-bytes"
    assert link_calls and link_calls[0][0] == dest

    entry = registry.load_registry()["model.gguf"]
    assert entry["sha256"] == old_sha
    assert entry["version"] == old_sha[:7]
    assert entry["size_bytes"] == len(b"old-bytes")
    assert entry["pinned"] is True
    assert entry["archive"]["sha256"] == new_sha
    assert entry["archive"]["size_bytes"] == len(b"new-bytes")


def test_rollback_twice_swaps_forward_again(isolated_omm_home, monkeypatch):
    """D4: rollback swaps the current file into the archive slot, so
    rolling back a second time restores it - a toggle, not a one-way trip."""
    _no_engines(monkeypatch)
    dest, _archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )

    first = runner.invoke(cli.app, ["rollback", "model.gguf"])
    assert first.exit_code == 0, first.output
    assert dest.read_bytes() == b"old-bytes"

    second = runner.invoke(cli.app, ["rollback", "model.gguf"])
    assert second.exit_code == 0, second.output
    assert dest.read_bytes() == b"new-bytes"


def test_rollback_refuses_when_archive_checksum_mismatches(isolated_omm_home, monkeypatch):
    """(h): a corrupted archive must not be swapped in - refuse and leave
    `dest` untouched."""
    _no_engines(monkeypatch)
    dest, archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )
    archive_path.write_bytes(b"corrupted-bytes")

    result = runner.invoke(cli.app, ["rollback", "model.gguf"])

    assert result.exit_code == 1
    assert "does not match" in result.stderr
    assert dest.read_bytes() == b"new-bytes"


def test_rollback_without_archive_errors(isolated_omm_home, monkeypatch):
    """(i): no archive on record at all."""
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"only-version")
    registry.save_registry(
        {"model.gguf": _entry(sha256=hashlib.sha256(b"only-version").hexdigest())}
    )

    result = runner.invoke(cli.app, ["rollback", "model.gguf"])

    assert result.exit_code == 1
    assert "No archived version" in result.stderr


def test_rollback_missing_archive_file_errors(isolated_omm_home, monkeypatch):
    """Registry claims an archive exists, but the file itself is gone."""
    _no_engines(monkeypatch)
    dest, archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )
    archive_path.unlink()

    result = runner.invoke(cli.app, ["rollback", "model.gguf"])

    assert result.exit_code == 1
    assert "No archived version" in result.stderr
    assert dest.read_bytes() == b"new-bytes"


def test_rollback_unknown_model_errors(isolated_omm_home):
    result = runner.invoke(cli.app, ["rollback", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_rollback_link_failure_still_records_actual_disk_sha(isolated_omm_home, monkeypatch):
    """(j): the file swap already happened by the time relinking is
    attempted, so a relink failure must not leave the registry describing
    the pre-rollback file instead of what's actually on disk now."""
    _no_engines(monkeypatch)
    dest, _archive_path, old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )
    monkeypatch.setattr(
        cli,
        "_link_model",
        lambda *a, **k: (_ for _ in ()).throw(cli.linker.InsufficientLinkSpaceError("full")),
    )

    result = runner.invoke(cli.app, ["rollback", "model.gguf"])

    assert result.exit_code == 1
    assert "relinking failed" in " ".join(result.stderr.split())
    assert dest.read_bytes() == b"old-bytes"
    entry = registry.load_registry()["model.gguf"]
    assert entry["sha256"] == old_sha
    assert all(value is False for value in entry["linked"].values())


def test_rollback_insufficient_space_to_set_current_version_aside_cancels(
    isolated_omm_home, monkeypatch
):
    """Mirrors upgrade's D3 behavior: if the current-before-rollback file
    can't be set aside (no hard link, no room to copy it), the rollback is
    cancelled and `dest` is left exactly as it was."""
    _no_engines(monkeypatch)
    dest, archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )
    monkeypatch.setattr(
        Path, "hardlink_to", lambda *a, **k: (_ for _ in ()).throw(OSError("cross-drive"))
    )
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))

    result = runner.invoke(cli.app, ["rollback", "model.gguf"])

    assert result.exit_code == 1
    assert "not enough disk space" in " ".join(result.stderr.split())
    assert dest.read_bytes() == b"new-bytes"
    assert archive_path.read_bytes() == b"old-bytes"


# --- lifecycle: uninstall / cleanup (D6) ------------------------------------


def test_uninstall_deletes_the_archived_version_too(isolated_omm_home, monkeypatch):
    """(k): uninstall removes a pinned model's archive along with the hub
    file and the registry entry."""
    _no_engines(monkeypatch)
    dest, archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )

    result = runner.invoke(cli.app, ["uninstall", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert not dest.exists()
    assert not archive_path.exists()
    assert "model.gguf" not in registry.load_registry()


def test_cleanup_reaps_orphan_archives_and_staging_but_preserves_pinned(
    isolated_omm_home, monkeypatch
):
    """(l): cleanup reclaims an archive whose model is no longer registered
    at all, and any leftover `.staging` temp file, but never touches the
    archive of a model that's still registered (pinned or not)."""
    _no_engines(monkeypatch)
    _dest, archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )
    orphan = cli.MODEL_ARCHIVE_DIR / "orphan.gguf"
    orphan.write_bytes(b"leftover-from-a-removed-model")
    staging = cli.MODEL_ARCHIVE_DIR / "model.gguf.staging"
    staging.write_bytes(b"interrupted-archive-write")

    result = runner.invoke(cli.app, ["cleanup"])

    assert result.exit_code == 0, result.output
    assert "orphaned archive file(s)" in result.stdout
    assert archive_path.exists()
    assert archive_path.read_bytes() == b"old-bytes"
    assert not orphan.exists()
    assert not staging.exists()


def test_cleanup_preserves_archive_of_a_registered_but_unpinned_model(
    isolated_omm_home, monkeypatch
):
    """The D6 orphan rule is keyed on registry presence, not on `pinned` -
    an archive belonging to a still-registered model is always kept."""
    _no_engines(monkeypatch)
    _dest, archive_path, _old_sha, _new_sha = _seed_pinned_model_with_archive(
        b"old-bytes", b"new-bytes"
    )
    registry.remove_fields("model.gguf", "pinned")
    assert "pinned" not in registry.load_registry()["model.gguf"]

    result = runner.invoke(cli.app, ["cleanup"])

    assert result.exit_code == 0, result.output
    assert archive_path.exists()


def test_cleanup_never_reclaims_an_archive_lock_file(isolated_omm_home):
    # filelock leaves `<archive>.lock` behind on POSIX. It has no registry
    # entry, so it looks like an orphan - but deleting one another process
    # holds would let a second process lock a fresh inode at the same time.
    cli.MODEL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    orphan = cli.MODEL_ARCHIVE_DIR / "orphan.gguf"
    lock = cli.MODEL_ARCHIVE_DIR / "orphan.gguf.lock"
    orphan.write_bytes(b"old")
    lock.write_bytes(b"")

    cli._cleanup_orphan_archives()

    assert not orphan.exists()
    assert lock.exists()

