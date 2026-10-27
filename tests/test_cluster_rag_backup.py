from datetime import datetime
import hashlib
import shutil
import sqlite3
import stat

import pytest

from omm import config
from omm.cluster import rag_backup
from omm.cluster.errors import ClusterError
from omm.documents import DocumentStore


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OMM_HOME", tmp_path)
    return tmp_path


def _names(path=None):
    db = sqlite3.connect(path or rag_backup.live_path())
    try:
        return sorted(row[0] for row in db.execute("SELECT name FROM collections"))
    finally:
        db.close()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_marker_none_without_store():
    assert rag_backup.store_marker() is None
    assert rag_backup.snapshot() is None


def test_marker_changes_after_write():
    DocumentStore().create_collection("one")
    first = rag_backup.store_marker()
    assert first
    DocumentStore().create_collection("two")
    assert rag_backup.store_marker() != first


def test_snapshot_is_reused_until_marker_changes():
    DocumentStore().create_collection("one")
    a = rag_backup.snapshot()
    b = rag_backup.snapshot()
    assert a == b and _sha(a[0]) == a[1] and a[2] == rag_backup.store_marker()
    assert "one" in _names(a[0])
    DocumentStore().create_collection("two")
    c = rag_backup.snapshot()
    assert c[1] != a[1] and "two" in _names(c[0])


def test_install_backup_rejects_wrong_sha(tmp_path):
    DocumentStore().create_collection("one")
    file, _, marker = rag_backup.snapshot()
    with pytest.raises(ClusterError):
        rag_backup.install_backup(file, sha256="0" * 64, marker=marker)
    assert not rag_backup.backup_path().exists()


def test_install_backup_rejects_a_file_that_is_not_sqlite(tmp_path):
    junk = tmp_path / "junk"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(ClusterError):
        rag_backup.install_backup(junk, sha256=_sha(junk), marker="m")


def test_install_backup_mode_and_marker(tmp_path):
    DocumentStore().create_collection("one")
    file, sha, _ = rag_backup.snapshot()
    incoming = tmp_path / "incoming.sqlite3"
    shutil.copyfile(file, incoming)
    rag_backup.install_backup(incoming, sha256=sha, marker="m1")
    assert rag_backup.backup_marker() == "m1"
    assert rag_backup.backup_saved_at()
    assert stat.S_IMODE(rag_backup.backup_path().stat().st_mode) == 0o600
    assert "one" in _names(rag_backup.backup_path())


def test_adopt_moves_own_store_aside(tmp_path):
    DocumentStore().create_collection("company")
    file, sha, marker = rag_backup.snapshot()
    incoming = tmp_path / "incoming.sqlite3"
    shutil.copyfile(file, incoming)
    rag_backup.install_backup(incoming, sha256=sha, marker=marker)
    rag_backup.live_path().unlink()
    DocumentStore().create_collection("mine")
    aside = rag_backup.adopt_backup_as_live(datetime(2026, 10, 27, 12, 0, 0))
    assert aside.name == "library.sqlite3.before-handover-20261027120000"
    assert "mine" in _names(aside)
    assert "company" in _names() and "mine" not in _names()
    assert not rag_backup.backup_path().exists()


def test_adopt_without_own_store_returns_none(tmp_path):
    DocumentStore().create_collection("company")
    file, sha, marker = rag_backup.snapshot()
    incoming = tmp_path / "incoming.sqlite3"
    shutil.copyfile(file, incoming)
    rag_backup.install_backup(incoming, sha256=sha, marker=marker)
    rag_backup.live_path().unlink()
    assert rag_backup.adopt_backup_as_live(datetime(2026, 10, 27)) is None
    assert "company" in _names()


def test_demote_then_delete():
    DocumentStore().create_collection("company")
    rag_backup.demote_live_to_backup()
    assert not rag_backup.live_path().exists()
    assert rag_backup.backup_marker()
    assert "company" in _names(rag_backup.backup_path())
    rag_backup.delete_backup()
    assert not rag_backup.backup_path().exists() and rag_backup.backup_marker() is None
