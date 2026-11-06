"""Atomic JSON persistence and locking for the Scaffold task graph.

Historical criteria-stack files are not read, migrated or removed.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

SCAFFOLD_DIR = Path.cwd() / ".scaffold"
STATE_LOCK_FILE = SCAFFOLD_DIR / ".tasks.lock"

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt  # type: ignore


@contextmanager
def state_lock(exclusive: bool = True) -> object:
    """Hold the shared task-state lock, refusing non-files and link traversal.

    Use the same lock inode for ordinary task transactions and administrative
    cleaning. Never unlink the lock as part of a reset.
    """
    if SCAFFOLD_DIR.is_symlink() or STATE_LOCK_FILE.is_symlink():
        raise ValueError("Refusing symbolic link in Scaffold task-state paths")
    SCAFFOLD_DIR.mkdir(parents=True, exist_ok=True)
    if not SCAFFOLD_DIR.is_dir():
        raise ValueError("Scaffold state path must be a directory")
    if STATE_LOCK_FILE.exists() and not STATE_LOCK_FILE.is_file():
        raise ValueError("Scaffold task lock must be a regular file")

    flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
    # Reject a symlink installed after the preflight, where supported.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # A substituted FIFO must not block the process at open().
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(STATE_LOCK_FILE, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Scaffold task lock must be a regular file")
        lock_handle = os.fdopen(descriptor, "a+b")
    except BaseException:
        os.close(descriptor)
        raise

    try:
        if lock_handle.tell() == 0:
            lock_handle.write(b"0")
            lock_handle.flush()
        if fcntl is not None:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_handle.fileno(), mode)
        else:  # pragma: no cover - Windows
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield lock_handle
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        lock_handle.close()


def atomic_write_json(path: Path, value: object) -> None:
    """Replace JSON under the caller's lock without following a fixed temp path.

    A securely created sibling temporary file avoids pre-existing .tmp symlinks.
    A failed write/replace leaves the original JSON unchanged and removes the
    newly created temporary file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("Refusing symbolic link in Scaffold task-state paths")
    payload = json.dumps(value, indent=2) + "\n"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    if os.name != "nt":  # pragma: no cover - platform-specific durability
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            dir_fd = os.open(str(path.parent), flags)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
