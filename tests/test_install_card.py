from __future__ import annotations

import hashlib

import pytest

from omm import cli, install_card, registry
from omm.downloader import DownloadError
from omm.hub import ResolvedModel


def test_cards_separate_planned_checks_from_results(tmp_path):
    plan = install_card.plan(
        provider="huggingface",
        repository="org/repo",
        filename="model.gguf",
        size_bytes=1024,
        destination=tmp_path / "model.gguf",
        url="https://huggingface.co/org/repo/model.gguf",
        expected_sha256="a" * 64,
    )
    result = install_card.result(
        url="https://huggingface.co/org/repo/model.gguf",
        actual_size=1024,
        expected_size=1024,
        actual_sha256="a" * 64,
        expected_sha256="a" * 64,
        reused_verified_cache=False,
    )
    assert plan["planned_checks"]["sha256"] == "provider/pinned digest match"
    assert result["sha256"] == "matched expected digest"
    assert "does not prove" in result["meaning"]


def test_install_card_reuses_registry_verified_bytes_without_a_provider_lookup(
    isolated_omm_home, monkeypatch
):
    content = b"verified-cache"
    digest = hashlib.sha256(content).hexdigest()
    resolved = ResolvedModel(
        "https://huggingface.co/org/repo/model.gguf",
        "model.gguf",
        "org/repo",
        None,
    )
    destination = cli.MODELS_DIR / resolved.filename
    destination.write_bytes(content)
    registry.save_registry(
        {
            resolved.filename: {
                "source": resolved.url,
                "repo_id": resolved.repo_id,
                "provider": resolved.provider,
                "sha256": digest,
            }
        }
    )
    monkeypatch.setattr(cli, "remote_file_size", lambda *a: pytest.fail("no network metadata"))
    monkeypatch.setattr(cli, "remote_file_sha256", lambda *a: pytest.fail("no network metadata"))
    card = cli._install_plan_for(resolved)

    assert card["source"] == "verified OMM cache"
    assert resolved.expected_sha256 == digest


def test_known_provider_size_mismatch_is_rejected_and_new_file_removed(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    monkeypatch.setattr(cli, "download_file", lambda url, path, **kwargs: path.write_bytes(b"short"))
    monkeypatch.setattr(cli, "sha256_file", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(cli, "_ensure_install_disk_capacity", lambda *a, **k: None)
    digest = hashlib.sha256(b"short").hexdigest()

    with pytest.raises(DownloadError, match="provider reported"):
        cli._prepare_install_artifact(
            url="https://example.test/model.gguf",
            filename="model.gguf",
            repo_id="org/repo",
            provider="huggingface",
            dest=destination,
            expected_sha256=digest,
            expected_size_bytes=99,
            source_metadata_checked=True,
            force=False,
            skip_unfit=False,
            stop_event=None,
            only_engine=None,
            opts=cli.GlobalOptions(),
        )
    assert not destination.exists()
