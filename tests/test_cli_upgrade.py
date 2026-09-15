from pathlib import Path
from types import SimpleNamespace
import hashlib
import pytest

from typer.testing import CliRunner

from omm import cli, config, registry
from omm.hub import ResolvedModel

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


# --- omm pin / unpin (issue #295, re-pointed at install --force by #322) ---


def test_pin_marks_registry_without_copying_anything(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"bytes")
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["pin", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert registry.load_registry()["model.gguf"]["pinned"] is True
    assert not cli.MODEL_ARCHIVE_DIR.exists() or not any(cli.MODEL_ARCHIVE_DIR.rglob("*"))


def test_pin_unknown_model_errors(isolated_omm_home):
    result = runner.invoke(cli.app, ["pin", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_pin_message_points_at_install_force(isolated_omm_home, monkeypatch):
    """(#322, S2-T5) pin's guidance now names the command that actually
    replaces a pinned model's bytes - `omm install --force`, not the
    removed `omm upgrade` re-download."""
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"bytes")
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["pin", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert "omm install --force" in result.stdout
    assert "omm upgrade" not in result.stdout


def test_unpin_message_does_not_mention_upgrade(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    (cli.MODELS_DIR / "model.gguf").write_bytes(b"bytes")
    registry.save_registry({"model.gguf": _entry(pinned=True)})

    result = runner.invoke(cli.app, ["unpin", "--help"])

    assert result.exit_code == 0, result.output
    assert "omm upgrade" not in result.stdout
    assert "install --force" in result.stdout


# --- install --force pin-archive hook (ported from omm upgrade, #322) -----
#
# `_update_one` used to archive a pinned model's about-to-be-replaced bytes
# before swapping in the new ones; that hook now lives inside
# `_prepare_install_artifact`'s `if force:` block (D-PIN-HOOK=A). These
# tests port the old `omm upgrade` coverage (issue #295) onto
# `cli._install_impl(..., force=True)` directly - the same style
# tests/test_install_impl.py already uses for this function, and simpler
# than driving the full `install` Typer command through resolve_model.


def _force_resolved(filename="model.gguf", expected_sha256=None):
    return ResolvedModel(
        url="https://huggingface.co/org/repo/resolve/main/" + filename,
        filename=filename,
        repo_id="org/repo",
        provider="huggingface",
        expected_sha256=expected_sha256,
    )


def _atomic_fake_download(content: bytes):
    """A `download_file` stub that lands bytes the same way the real
    downloader does: a fresh temp file swapped into place with
    `Path.replace` (a new inode), not an in-place write. Archiving a
    pinned model hard-links the archive to the pre-download `dest` inode
    (`_copy_or_hardlink_with_space_check`); an in-place `write_bytes` on
    `dest` would silently corrupt that archive too, since both names would
    still point at the same inode."""

    def _download(url, path, **kw):
        path = Path(path)
        tmp = path.with_name(path.name + ".test-download-tmp")
        tmp.write_bytes(content)
        tmp.replace(path)

    return _download


def _stub_install_force_common(monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    _no_engines(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: 100)
    # Plenty of room on every preflight check - these tests are about the
    # archive hook, not disk-space accounting (that's covered separately in
    # tests/test_install_impl.py).
    monkeypatch.setattr(cli, "_ensure_install_disk_capacity", lambda *a, **k: None)


def test_install_force_without_pin_does_not_archive(isolated_omm_home, monkeypatch):
    """Regression: an unpinned model's --force reinstall must not create an
    archive - only `omm pin` opts a model into that storage cost."""
    _stub_install_force_common(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    old_bytes = b"old-bytes"
    dest.write_bytes(old_bytes)
    registry.save_registry(
        {"model.gguf": _entry(sha256=hashlib.sha256(old_bytes).hexdigest())}
    )
    monkeypatch.setattr(cli, "download_file", lambda url, path, **kw: Path(path).write_bytes(b"new-bytes"))
    resolved = _force_resolved(expected_sha256=hashlib.sha256(b"new-bytes").hexdigest())

    cli._install_impl(resolved, force=True)

    assert dest.read_bytes() == b"new-bytes"
    assert not (cli.MODEL_ARCHIVE_DIR / "model.gguf").exists()
    assert "archive" not in registry.load_registry()["model.gguf"]


def test_install_force_pinned_model_archives_old_version(isolated_omm_home, monkeypatch):
    _stub_install_force_common(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    old_bytes = b"old-bytes"
    dest.write_bytes(old_bytes)
    old_sha = hashlib.sha256(old_bytes).hexdigest()
    registry.save_registry({"model.gguf": _entry(sha256=old_sha, pinned=True)})
    monkeypatch.setattr(cli, "download_file", _atomic_fake_download(b"new-bytes"))
    resolved = _force_resolved(expected_sha256=hashlib.sha256(b"new-bytes").hexdigest())

    cli._install_impl(resolved, force=True)

    assert dest.read_bytes() == b"new-bytes"
    archive_path = cli.MODEL_ARCHIVE_DIR / "model.gguf"
    assert archive_path.read_bytes() == old_bytes
    entry = registry.load_registry()["model.gguf"]
    assert entry["archive"]["sha256"] == old_sha
    assert entry["sha256"] == hashlib.sha256(b"new-bytes").hexdigest()
    assert entry["pinned"] is True


def test_install_force_pinned_model_twice_keeps_a_single_archive_slot(isolated_omm_home, monkeypatch):
    _stub_install_force_common(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"v1")
    registry.save_registry(
        {"model.gguf": _entry(sha256=hashlib.sha256(b"v1").hexdigest(), pinned=True)}
    )

    monkeypatch.setattr(cli, "download_file", _atomic_fake_download(b"v2"))
    cli._install_impl(
        _force_resolved(expected_sha256=hashlib.sha256(b"v2").hexdigest()), force=True
    )

    monkeypatch.setattr(cli, "download_file", _atomic_fake_download(b"v3"))
    cli._install_impl(
        _force_resolved(expected_sha256=hashlib.sha256(b"v3").hexdigest()), force=True
    )

    archive_path = cli.MODEL_ARCHIVE_DIR / "model.gguf"
    assert archive_path.read_bytes() == b"v2"
    assert dest.read_bytes() == b"v3"
    assert sum(1 for p in cli.MODEL_ARCHIVE_DIR.rglob("*") if p.is_file() and not p.name.endswith(".lock")) == 1


def test_install_force_pinned_model_insufficient_archive_space_cancels_install(
    isolated_omm_home, monkeypatch, capsys
):
    """D3: when the archive copy can't be made (no hard link, no room for a
    real copy), the whole reinstall is cancelled - the installed file is
    left exactly as it was."""
    _stub_install_force_common(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    old_bytes = b"old-bytes"
    dest.write_bytes(old_bytes)
    registry.save_registry(
        {"model.gguf": _entry(sha256=hashlib.sha256(old_bytes).hexdigest(), pinned=True)}
    )
    monkeypatch.setattr(cli, "download_file", lambda url, path, **kw: pytest.fail("must not download"))
    monkeypatch.setattr(
        Path, "hardlink_to", lambda *a, **k: (_ for _ in ()).throw(OSError("cross-drive"))
    )
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    resolved = _force_resolved(expected_sha256=hashlib.sha256(b"new-bytes").hexdigest())

    with pytest.raises(cli.DownloadError):
        cli._install_impl(resolved, force=True)

    # The detailed reason comes from `_archive_before_replace` itself
    # (printed to stderr before this function's own DownloadError, whose
    # message is intentionally generic - see D-PIN-HOOK=A).
    stderr = " ".join(capsys.readouterr().err.split())
    assert "not enough disk space" in stderr
    assert "Reinstall cancelled" in stderr
    assert dest.read_bytes() == old_bytes
    assert not (cli.MODEL_ARCHIVE_DIR / "model.gguf").exists()


def test_install_force_pinned_model_skips_archive_when_installed_file_is_tampered(
    isolated_omm_home, monkeypatch, capsys
):
    """D7: if the installed file doesn't match the registry's recorded
    checksum, it isn't safe to call it "the pinned version" - skip the
    archive (with a warning) but still let the reinstall repair the file."""
    _stub_install_force_common(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"tampered")
    expected = hashlib.sha256(b"known-good").hexdigest()
    registry.save_registry({"model.gguf": _entry(sha256=expected, pinned=True)})
    monkeypatch.setattr(
        cli, "download_file", lambda url, path, **kw: Path(path).write_bytes(b"known-good")
    )
    resolved = _force_resolved(expected_sha256=expected)

    cli._install_impl(resolved, force=True)

    assert dest.read_bytes() == b"known-good"
    assert not (cli.MODEL_ARCHIVE_DIR / "model.gguf").exists()


def test_install_force_skip_does_not_archive_pinned_model(isolated_omm_home, monkeypatch):
    """New case (#322): when the source already matches (the same-file
    skip fires), nothing is replaced, so nothing is archived either."""
    _stub_install_force_common(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    same_bytes = b"same-bytes"
    dest.write_bytes(same_bytes)
    same_sha = hashlib.sha256(same_bytes).hexdigest()
    monkeypatch.setattr(cli, "sha256_file", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    registry.save_registry({"model.gguf": _entry(sha256=same_sha, pinned=True)})
    monkeypatch.setattr(cli, "download_file", lambda *a, **k: pytest.fail("must not re-download"))
    resolved = _force_resolved(expected_sha256=same_sha)

    cli._install_impl(resolved, force=True)

    assert dest.read_bytes() == same_bytes
    assert not (cli.MODEL_ARCHIVE_DIR / "model.gguf").exists()
    assert "archive" not in registry.load_registry()["model.gguf"]


def test_install_force_and_list_tolerate_entries_with_no_pin_or_archive_fields(
    isolated_omm_home, monkeypatch
):
    """Old registries never had `pinned`/`archive` keys at all - both
    `install --force` and `list` must work exactly as before for such an
    entry."""
    _stub_install_force_common(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"old-bytes")
    entry = _entry(sha256=hashlib.sha256(b"old-bytes").hexdigest())
    assert "pinned" not in entry and "archive" not in entry
    registry.save_registry({"model.gguf": entry})
    monkeypatch.setattr(cli, "download_file", lambda url, path, **kw: Path(path).write_bytes(b"new-bytes"))
    resolved = _force_resolved(expected_sha256=hashlib.sha256(b"new-bytes").hexdigest())

    cli._install_impl(resolved, force=True)
    assert dest.read_bytes() == b"new-bytes"

    list_result = runner.invoke(cli.app, ["list"])
    assert list_result.exit_code == 0, list_result.output
    assert "pinned" not in list_result.stdout


# --- omm upgrade: suggestion scan + opt-in install (#322 redesign) --------


def _stub_upgrade_pipeline(
    monkeypatch,
    *,
    repo_files=(),
    candidates=None,
    predict_tps=None,
    fits=True,
    remote_sizes=None,
):
    """Stub every network/hardware lookup `_scan_for_upgrades` makes, so
    `omm upgrade` tests exercise the real scan + table + install-loop code
    in cli.py without touching a network or real hardware. `upgrade.py`'s
    own matching algorithm (find_successor/find_quant_upgrade) has its own
    dedicated coverage in tests/test_upgrade_scan.py."""
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli, "calculate_memory_budget", lambda hw: object())
    monkeypatch.setattr(cli, "_candidate_fits_budget", lambda hw, budget, c: fits)
    monkeypatch.setattr(
        cli,
        "_load_recommendation_with_change_note",
        lambda config: (
            {
                "trees": [{}] if predict_tps is not None else None,
                "candidates": list(candidates or []),
            },
            False,
        ),
    )
    if predict_tps is not None:
        monkeypatch.setattr(cli.predictor, "predict_speed", lambda trees, hw, candidate: predict_tps)
    monkeypatch.setattr(cli, "fetch_repo_files", lambda provider, repo_id: (list(repo_files), None))
    sizes = remote_sizes or {}
    monkeypatch.setattr(
        cli, "remote_file_size", lambda provider, repo_id, filename: sizes.get(filename, 1024)
    )


def test_upgrade_reports_nothing_when_no_better_option_exists(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.Q4_K_M.gguf": _entry(repo_id="org/repo")})
    _stub_upgrade_pipeline(monkeypatch, repo_files=["model.Q4_K_M.gguf"], candidates=[])

    result = runner.invoke(cli.app, ["upgrade", "model.Q4_K_M.gguf"])

    assert result.exit_code == 0, result.output
    assert "nothing better found" in result.stdout


def test_upgrade_suggests_higher_quant_from_same_repo_and_installs_on_yes(
    isolated_omm_home, monkeypatch
):
    registry.save_registry({"model.Q4_K_M.gguf": _entry(repo_id="org/repo")})
    _stub_upgrade_pipeline(
        monkeypatch, repo_files=["model.Q4_K_M.gguf", "model.Q6_K.gguf"], candidates=[]
    )
    # Not a fresh install: forcing _stdin_is_tty True below would otherwise
    # let the real first-run setup wizard fire on this isolated_omm_home
    # before this test's own scenario runs.
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: True)
    install_calls = []
    resolve_calls = []

    def fake_resolve(ref):
        resolve_calls.append(ref)
        return ResolvedModel(
            url="https://example.test/model.Q6_K.gguf",
            filename="model.Q6_K.gguf",
            repo_id="org/repo",
            provider="huggingface",
        )

    monkeypatch.setattr(cli, "_resolve_model_interactive", fake_resolve)
    monkeypatch.setattr(
        cli, "_install_impl", lambda resolved, **kw: install_calls.append(resolved) or object()
    )

    result = runner.invoke(cli.app, ["upgrade", "model.Q4_K_M.gguf"])

    assert result.exit_code == 0, result.output
    assert "Q4_K_M" in result.stdout
    assert "Q6_K" in result.stdout
    assert len(install_calls) == 1
    assert install_calls[0].filename == "model.Q6_K.gguf"
    assert resolve_calls == ["hf:org/repo:model.Q6_K.gguf"]


def test_upgrade_declined_suggestion_installs_nothing(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.Q4_K_M.gguf": _entry(repo_id="org/repo")})
    _stub_upgrade_pipeline(
        monkeypatch, repo_files=["model.Q4_K_M.gguf", "model.Q6_K.gguf"], candidates=[]
    )
    # Not a fresh install: forcing _stdin_is_tty True below would otherwise
    # let the real first-run setup wizard fire on this isolated_omm_home
    # before this test's own scenario runs.
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: False)
    monkeypatch.setattr(
        cli, "_install_impl", lambda *a, **k: pytest.fail("must not install a declined suggestion")
    )

    result = runner.invoke(cli.app, ["upgrade", "model.Q4_K_M.gguf"])

    assert result.exit_code == 0, result.output


def test_upgrade_dry_run_prints_table_and_installs_nothing(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.Q4_K_M.gguf": _entry(repo_id="org/repo")})
    _stub_upgrade_pipeline(
        monkeypatch, repo_files=["model.Q4_K_M.gguf", "model.Q6_K.gguf"], candidates=[]
    )
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: pytest.fail("must not prompt in --dry-run")
    )
    monkeypatch.setattr(
        cli, "_install_impl", lambda *a, **k: pytest.fail("must not install in --dry-run")
    )

    result = runner.invoke(cli.app, ["upgrade", "model.Q4_K_M.gguf", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Q6_K" in result.stdout


def test_upgrade_prefers_successor_over_same_repo_quant(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.Q4_K_M.gguf": _entry(repo_id="org/repo")})
    candidates = [
        {
            "name": "model-v1",
            "repo_id": "org/repo",
            "filename": "model.Q4_K_M.gguf",
            "provider": "huggingface",
            "description": "v1",
        },
        {
            "name": "model-v2",
            "repo_id": "org/repo-v2",
            "filename": "model-v2.Q4_K_M.gguf",
            "provider": "huggingface",
            "description": "v2",
            "supersedes": ["model-v1"],
        },
    ]
    _stub_upgrade_pipeline(
        monkeypatch,
        repo_files=["model.Q4_K_M.gguf", "model.Q6_K.gguf"],
        candidates=candidates,
    )

    result = runner.invoke(cli.app, ["upgrade", "model.Q4_K_M.gguf", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "model-v2.Q4_K_M.gguf" in result.stdout
    assert "Q6_K" in result.stdout
    assert result.stdout.index("model-v2.Q4_K_M.gguf") < result.stdout.index("Q6_K")


def test_upgrade_skips_quant_check_for_direct_url_install_but_still_matches_successor(
    isolated_omm_home, monkeypatch
):
    registry.save_registry({"model.gguf": _entry(repo_id=None)})
    _stub_upgrade_pipeline(monkeypatch, repo_files=[], candidates=[])
    # Override the helper's own (safe) fetch_repo_files stub - a direct-URL
    # install (repo_id=None) must never even attempt (A)'s repo lookup.
    monkeypatch.setattr(
        cli, "fetch_repo_files", lambda *a, **k: pytest.fail("must not check a direct-URL install")
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert "nothing better found" in result.stdout


def test_upgrade_all_continues_past_one_models_provider_failure(isolated_omm_home, monkeypatch):
    registry.save_registry(
        {
            "first.Q4_K_M.gguf": _entry(repo_id="org/first"),
            "second.Q4_K_M.gguf": _entry(repo_id="org/second"),
        }
    )
    _stub_upgrade_pipeline(monkeypatch, repo_files=[], candidates=[])

    def fake_fetch(provider, repo_id):
        if repo_id == "org/first":
            raise cli.ModelResolutionError("boom")
        return ["second.Q4_K_M.gguf", "second.Q6_K.gguf"], None

    monkeypatch.setattr(cli, "fetch_repo_files", fake_fetch)

    result = runner.invoke(cli.app, ["upgrade", "all"])

    assert result.exit_code == 0, result.output
    assert "first.Q4_K_M.gguf: could not be checked" in " ".join(result.stderr.split())
    assert "Q6_K" in result.stdout


def test_upgrade_all_mixed_approval_installs_only_approved(isolated_omm_home, monkeypatch):
    registry.save_registry(
        {
            "first.Q4_K_M.gguf": _entry(repo_id="org/first"),
            "second.Q4_K_M.gguf": _entry(repo_id="org/second"),
        }
    )

    def fake_fetch(provider, repo_id):
        prefix = "first" if repo_id == "org/first" else "second"
        return [f"{prefix}.Q4_K_M.gguf", f"{prefix}.Q6_K.gguf"], None

    _stub_upgrade_pipeline(monkeypatch, candidates=[])
    monkeypatch.setattr(cli, "fetch_repo_files", fake_fetch)
    # Not a fresh install: forcing _stdin_is_tty True below would otherwise
    # let the real first-run setup wizard fire on this isolated_omm_home
    # before this test's own scenario runs.
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)

    answers = iter([True, False])
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: next(answers))
    monkeypatch.setattr(
        cli, "_resolve_model_interactive",
        lambda ref: ResolvedModel(url="https://x", filename=ref.split(":")[-1], repo_id="org/x", provider="huggingface"),
    )
    install_calls = []
    monkeypatch.setattr(
        cli, "_install_impl", lambda resolved, **kw: install_calls.append(resolved.filename) or object()
    )

    result = runner.invoke(cli.app, ["upgrade", "all"])

    assert result.exit_code == 0, result.output
    assert len(install_calls) == 1


def test_upgrade_without_tty_prints_table_and_exits_zero(isolated_omm_home, monkeypatch):
    """D-NONTTY=A: a non-interactive caller (the default for CliRunner,
    same as any script or CI job) gets the table and a hint, and exits 0 -
    `omm upgrade` never installs anything without explicit opt-in."""
    registry.save_registry({"model.Q4_K_M.gguf": _entry(repo_id="org/repo")})
    _stub_upgrade_pipeline(
        monkeypatch, repo_files=["model.Q4_K_M.gguf", "model.Q6_K.gguf"], candidates=[]
    )
    monkeypatch.setattr(
        cli, "_install_impl", lambda *a, **k: pytest.fail("must not install without --yes/a tty")
    )

    result = runner.invoke(cli.app, ["upgrade", "model.Q4_K_M.gguf"])

    assert result.exit_code == 0, result.output
    assert "Q6_K" in result.stdout
    assert "--yes" in result.stdout


def test_upgrade_yes_installs_every_suggestion_without_prompting(isolated_omm_home, monkeypatch):
    registry.save_registry(
        {
            "first.Q4_K_M.gguf": _entry(repo_id="org/first"),
            "second.Q4_K_M.gguf": _entry(repo_id="org/second"),
        }
    )

    def fake_fetch(provider, repo_id):
        prefix = "first" if repo_id == "org/first" else "second"
        return [f"{prefix}.Q4_K_M.gguf", f"{prefix}.Q6_K.gguf"], None

    _stub_upgrade_pipeline(monkeypatch, candidates=[])
    monkeypatch.setattr(cli, "fetch_repo_files", fake_fetch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: pytest.fail("--yes must skip the prompt")
    )
    monkeypatch.setattr(
        cli, "_resolve_model_interactive",
        lambda ref: ResolvedModel(url="https://x", filename=ref.split(":")[-1], repo_id="org/x", provider="huggingface"),
    )
    install_calls = []
    monkeypatch.setattr(
        cli, "_install_impl", lambda resolved, **kw: install_calls.append(resolved.filename) or object()
    )

    result = runner.invoke(cli.app, ["upgrade", "all", "--yes"])

    assert result.exit_code == 0, result.output
    assert len(install_calls) == 2


def test_upgrade_errors_for_uninstalled_model(isolated_omm_home):
    result = runner.invoke(cli.app, ["upgrade", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_upgrade_all_with_empty_registry_reports_nothing_to_do(isolated_omm_home):
    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 0, result.stdout
    assert "No models installed" in result.stdout


def test_upgrade_prints_predicted_speed_as_information_only(isolated_omm_home, monkeypatch):
    """A candidate's predicted tok/s is shown for information, never used
    to gate the suggestion - a higher quant is suggested even when its
    predicted speed happens to be lower (spec: informational only)."""
    registry.save_registry({"model.Q4_K_M.gguf": _entry(repo_id="org/repo")})
    _stub_upgrade_pipeline(
        monkeypatch,
        repo_files=["model.Q4_K_M.gguf", "model.Q6_K.gguf"],
        candidates=[],
        predict_tps=5.0,
    )

    result = runner.invoke(cli.app, ["upgrade", "model.Q4_K_M.gguf", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Q6_K" in result.stdout
    assert "tok/s" in result.stdout


def test_upgrade_install_failure_does_not_abort_remaining_suggestions(isolated_omm_home, monkeypatch):
    registry.save_registry(
        {
            "first.Q4_K_M.gguf": _entry(repo_id="org/first"),
            "second.Q4_K_M.gguf": _entry(repo_id="org/second"),
        }
    )

    def fake_fetch(provider, repo_id):
        prefix = "first" if repo_id == "org/first" else "second"
        return [f"{prefix}.Q4_K_M.gguf", f"{prefix}.Q6_K.gguf"], None

    _stub_upgrade_pipeline(monkeypatch, candidates=[])
    monkeypatch.setattr(cli, "fetch_repo_files", fake_fetch)
    monkeypatch.setattr(
        cli, "_resolve_model_interactive",
        lambda ref: ResolvedModel(url="https://x", filename=ref.split(":")[-1], repo_id="org/x", provider="huggingface"),
    )
    install_calls = []

    def fake_install(resolved, **kw):
        install_calls.append(resolved.filename)
        if len(install_calls) == 1:
            raise cli.DownloadError("boom")
        return object()

    monkeypatch.setattr(cli, "_install_impl", fake_install)

    result = runner.invoke(cli.app, ["upgrade", "all", "--yes"])

    assert result.exit_code == 0, result.output
    assert len(install_calls) == 2
