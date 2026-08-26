"""Cross-process locks and crash-safe small-file replacement."""

from __future__ import annotations

import os
import hashlib
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock


def _replace_temporary(temporary: Path, path: Path) -> None:
    """Atomically replace ``path`` and make the directory entry durable."""
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(0.025 * (2**attempt), 0.5))

    # fsyncing only the file does not guarantee that the rename itself
    # survives a crash. Directory fsync is supported on POSIX; Windows and
    # some filesystems reject it, in which case the atomic replacement still
    # provides the strongest available behavior.
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


@contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Serialize writers without putting a lock inside the protected file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=timeout)
    with lock:
        yield


def atomic_write_text(path: Path, content: str) -> None:
    """Write, fsync, and replace so interruption never leaves partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_temporary(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Binary counterpart to :func:`atomic_write_text`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_temporary(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def backup_corrupt_file(path: Path) -> Path | None:
    """Preserve unreadable state before callers continue with safe defaults."""
    try:
        content = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(content).hexdigest()[:12]
    backup = path.with_name(f"{path.name}.corrupt-{digest}")
    if not backup.exists():
        atomic_write_bytes(backup, content)
    return backup
