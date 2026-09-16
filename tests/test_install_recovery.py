from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from omm import cli, config, install_state, registry
from omm.downloader import DownloadError
from omm.hub import ResolvedModel


@pytest.fixture
def install_fixture(isolated_omm_home, tmp_path, monkeypatch):
    data = b"verified model file used for filesystem recovery, not inference"
    digest = hashlib.sha256(data).hexdigest()
    resolved = ResolvedModel(url="https://example.test/model.gguf", filename="model.gguf",
                             repo_id=None, provider=None, expected_sha256=digest)
    downloads = []
    def download(_url, path, **_kwargs):
        downloads.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(cli, "download_file", download)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "_ensure_install_disk_capacity", lambda *a, **k: None)
    # Two non-inference runners exercise real file linking in sandbox dirs.
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key in {"jan", "koboldcpp"})
    destinations = {key: tmp_path / key / "model.gguf" for key in ("jan", "koboldcpp")}
    def link(key, source, **_kwargs):
        cli.linker.link_file(source, destinations[key])
    monkeypatch.setattr(cli.linker, "link_engine", link)
    return resolved, downloads, destinations, link


def test_restart_reuses_verified_bytes_and_repairs_partial_links(install_fixture, monkeypatch):
    resolved, downloads, destinations, link = install_fixture
    def interrupted(key, source, **kwargs):
        if key == "koboldcpp":
            raise KeyboardInterrupt
        return link(key, source, **kwargs)
    monkeypatch.setattr(cli.linker, "link_engine", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli._install_impl(resolved, no_upload=True)
    before = install_state.read_record(resolved.filename)
    assert before["phase"] == "linking"
    assert before["status"] == "interrupted"
    assert before["linked"]["jan"] is True
    assert registry.load_registry() == {}
    # Even a previously completed link is checked again, not trusted from JSON.
    destinations["jan"].unlink()
    monkeypatch.setattr(cli.linker, "link_engine", link)
    result = cli._install_impl(resolved, no_upload=True)
    assert len(downloads) == 1
    assert result.linked["jan"] and result.linked["koboldcpp"]
    assert all(path.read_bytes() == downloads[0].read_bytes() for path in destinations.values())
    assert registry.load_registry()[resolved.filename]["sha256"] == resolved.expected_sha256
    assert install_state.read_record(resolved.filename)["status"] == "complete"


def test_resume_rejects_changed_file_instead_of_trusting_checkpoint(install_fixture, monkeypatch):
    resolved, downloads, _, _ = install_fixture
    monkeypatch.setattr(cli.linker, "link_engine", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        cli._install_impl(resolved, no_upload=True)
    downloads[0].write_bytes(b"changed after interruption")
    with pytest.raises(DownloadError, match="does not match"):
        cli._install_impl(resolved, no_upload=True)
    assert downloads[0].read_bytes() == b"changed after interruption"
    assert len(downloads) == 1


def test_registry_write_failure_is_recoverable(install_fixture, monkeypatch):
    resolved, downloads, destinations, _ = install_fixture
    original = registry.upsert_entry
    monkeypatch.setattr(registry, "upsert_entry", lambda *a, **k: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        cli._install_impl(resolved, no_upload=True)
    assert install_state.read_record(resolved.filename)["status"] == "failed"
    monkeypatch.setattr(registry, "upsert_entry", original)
    cli._install_impl(resolved, no_upload=True)
    assert len(downloads) == 1
    assert all(path.exists() for path in destinations.values())


def test_other_install_and_cleanup_cannot_race_the_link_stage(install_fixture, monkeypatch):
    resolved, downloads, _, link = install_fixture
    entered, release = threading.Event(), threading.Event()
    def wait_link(key, source, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return link(key, source, **kwargs)
    monkeypatch.setattr(cli.linker, "link_engine", wait_link)
    with ThreadPoolExecutor(max_workers=1) as executor:
        work = executor.submit(cli._install_impl, resolved, no_upload=True)
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(DownloadError, match="Another install"):
                cli._install_impl(resolved, no_upload=True)
            assert cli._cleanup_incomplete_installs() == 0
            assert downloads[0].exists()
        finally:
            release.set()
        work.result(timeout=5)


def test_corrupt_record_is_preserved_but_never_used_as_proof(install_fixture):
    resolved, _, _, _ = install_fixture
    path = install_state._path(resolved.filename)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    cli._install_impl(resolved, no_upload=True)
    assert list(path.parent.glob("*.corrupt-*"))
    assert install_state.read_record(resolved.filename)["status"] == "complete"


def test_journal_never_persists_url_credentials(install_fixture):
    resolved, _, _, _ = install_fixture
    resolved = ResolvedModel(url="https://example.test/model.gguf?token=private", filename=resolved.filename,
                             repo_id=None, provider=None, expected_sha256=resolved.expected_sha256)
    cli._install_impl(resolved, no_upload=True)
    raw = install_state._path(resolved.filename).read_text(encoding="utf-8")
    assert "private" not in raw and "https://" not in raw


def test_missing_journal_diagnostics_do_not_create_files(tmp_path, monkeypatch):
    root = tmp_path / "absent"
    monkeypatch.setattr(config, "OMM_HOME", root)
    assert install_state.pending_records() == []
    assert not root.exists()


def test_process_death_before_registration_resumes_in_a_new_process(tmp_path):
    """Exercise actual process exit, persisted files, and a fresh registry reader.

    File download is a local fixture and runners are disabled: this proves
    filesystem recovery, not generation or a remote provider integration.
    """
    source = '''
import hashlib, os, sys
from omm import cli, registry, install_state
from omm.hub import ResolvedModel
data = b"process recovery fixture"
digest = hashlib.sha256(data).hexdigest()
resolved = ResolvedModel(url="https://example.test/model.gguf", filename="model.gguf",
                         repo_id=None, provider=None, expected_sha256=digest)
cli.linker.is_engine_installed = lambda key: False
cli.predictor.load_cached_model = lambda: None
def download(url, path, **kwargs):
    assert sys.argv[1] == "crash", "must reuse the verified file on restart"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
cli.download_file = download
if sys.argv[1] == "crash":
    registry.upsert_entry = lambda *a, **k: os._exit(17)
cli._install_impl(resolved, no_upload=True)
assert registry.load_registry()["model.gguf"]["sha256"] == digest
assert install_state.read_record("model.gguf")["status"] == "complete"
'''
    env = {**os.environ, "OMM_HOME": str(tmp_path / "process-home")}
    crash = subprocess.run([sys.executable, "-c", source, "crash"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert crash.returncode == 17, crash.stderr
    journal = next((tmp_path / "process-home" / "install-journal").glob("*.json"))
    assert json.loads(journal.read_text(encoding="utf-8"))["status"] == "running"
    resume = subprocess.run([sys.executable, "-c", source, "resume"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert resume.returncode == 0, resume.stderr
    assert "Resuming interrupted install" in resume.stdout
