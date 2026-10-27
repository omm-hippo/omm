"""The head's shared document store, copied to every member as a backup.

The head serves a consistent SQLite snapshot of OMM_HOME/documents; each
member keeps it apart from its own documents, under OMM_HOME/cluster. At a
handover the new head's backup becomes its live store, and the old head's
live store becomes its backup.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading

from omm import config
from omm.cluster.errors import ClusterError

_SIDECARS = ("-wal", "-shm", "-journal")
_lock = threading.Lock()


def live_path() -> Path:
    return config.OMM_HOME / "documents" / "library.sqlite3"


def backup_path() -> Path:
    return config.OMM_HOME / "cluster" / "rag-backup" / "library.sqlite3"


def _snapshot_dir() -> Path:
    return config.OMM_HOME / "cluster" / "rag-snapshot"


def _stamp(path: Path) -> str | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return f"{info.st_mtime_ns}:{info.st_size}"


def store_marker(path: Path | None = None) -> str | None:
    """Changes on every write to the store; cheap enough to compute on every poll."""
    path = path or live_path()
    db = _stamp(path)
    if db is None:
        return None
    wal = _stamp(path.with_name(path.name + "-wal"))
    return f"{db}|{wal}" if wal else db


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _refuse_links(path: Path) -> None:
    if config.OMM_HOME.is_symlink() or path.parent.is_symlink() or any(
            p.is_symlink() for p in (path, *(path.with_name(path.name + s) for s in _SIDECARS))):
        raise ClusterError(f"refusing to copy documents through a symbolic link at {path.parent}")


def _private_temp(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".library.", suffix=".tmp")
    os.close(descriptor)
    return Path(name)


def snapshot() -> tuple[Path, str, str] | None:
    """(file, sha256, marker) of a consistent copy of the live store; None when there is none."""
    source = live_path()
    with _lock:
        marker = store_marker(source)
        if marker is None:
            return None
        _refuse_links(source)
        target = _snapshot_dir() / "library.sqlite3"
        meta = _snapshot_dir() / "snapshot.json"
        try:
            saved = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if saved.get("marker") == marker and target.is_file():
            return target, saved["sha256"], marker
        for _ in range(3):
            temporary = _private_temp(_snapshot_dir())
            try:
                reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
                writer = sqlite3.connect(temporary)
                try:
                    reader.backup(writer)
                finally:
                    writer.close()
                    reader.close()
                after = store_marker(source)
                if after != marker:
                    marker = after
                    continue
                sha = _sha256(temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            meta.write_text(json.dumps({"marker": marker, "sha256": sha}), encoding="utf-8")
            return target, sha, marker
        raise ClusterError("the document store kept changing while it was being copied; try again")


def _meta_path() -> Path:
    return backup_path().with_name("backup.json")


def _meta() -> dict:
    try:
        data = json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(marker: str, sha: str) -> None:
    from omm.atomic import atomic_write_text

    atomic_write_text(_meta_path(), json.dumps({"marker": marker, "sha256": sha,
                                                "saved_at": datetime.now(timezone.utc).isoformat()}))


def install_backup(tmp_file: Path, *, sha256: str, marker: str) -> None:
    """Keep a snapshot from the head as this Mac's backup, once it proves to be that snapshot."""
    target = backup_path()
    _refuse_links(target)
    temporary = _private_temp(target.parent)
    try:
        with tmp_file.open("rb") as source, temporary.open("wb") as copy:
            for block in iter(lambda: source.read(1 << 20), b""):
                copy.write(block)
            copy.flush()
            os.fsync(copy.fileno())
        if _sha256(temporary) != sha256:
            raise ClusterError("the document snapshot from the head did not match its checksum")
        try:
            db = sqlite3.connect(f"file:{temporary}?mode=ro", uri=True)
            try:
                ok = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            finally:
                db.close()
        except sqlite3.Error:
            ok = False
        if not ok:
            raise ClusterError("the document snapshot from the head is not a readable database")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    _write_meta(marker, sha256)


def backup_marker() -> str | None:
    return _meta().get("marker") if backup_path().is_file() else None


def backup_saved_at() -> str | None:
    return _meta().get("saved_at") if backup_path().is_file() else None


def _move_with_sidecars(source: Path, target: Path) -> None:
    os.replace(source, target)
    for suffix in _SIDECARS:
        side = source.with_name(source.name + suffix)
        if side.exists():
            os.replace(side, target.with_name(target.name + suffix))


def adopt_backup_as_live(now: datetime) -> Path | None:
    """Make the backup this Mac's live store; returns where an existing live store was moved, if any."""
    backup, live = backup_path(), live_path()
    if not backup.is_file():
        raise ClusterError("this Mac has no copy of the head's documents yet")
    _refuse_links(live)
    aside = None
    if live.exists():
        aside = live.with_name(f"{live.name}.before-handover-{now:%Y%m%d%H%M%S}")
        _move_with_sidecars(live, aside)
    live.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.replace(backup, live)
    _meta_path().unlink(missing_ok=True)
    return aside


def demote_live_to_backup() -> None:
    """Keep this Mac's live store (it was the head's) as its backup copy."""
    live, backup = live_path(), backup_path()
    if not live.is_file():
        return
    _refuse_links(live)
    db = sqlite3.connect(live)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _move_with_sidecars(live, backup)
    os.chmod(backup, 0o600)
    _write_meta(store_marker(backup) or "", _sha256(backup))


def delete_backup() -> None:
    backup = backup_path()
    for path in (backup, *(backup.with_name(backup.name + s) for s in _SIDECARS), _meta_path()):
        path.unlink(missing_ok=True)
