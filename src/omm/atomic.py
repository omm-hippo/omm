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
        # Windows antivirus/indexing can hold the destination for a very
        # short interval. A single PermissionError used to make config writes
        # fail and benchmark-history writes silently lose an entry. Keep the
        # replacement atomic, but retry only this transient sharing failure.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.025 * (2**attempt), 0.5))
    finally:
        temporary.unlink(missing_ok=True)


def backup_corrupt_file(path: Path) -> Path | None:
    """Preserve unreadable state before callers continue with safe defaults."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    backup = path.with_name(f"{path.name}.corrupt-{digest}")
    if not backup.exists():
        atomic_write_text(backup, content)
    return backup
