"""File persistence shared by Scaffold tasks and the legacy criteria CLI.

The historical .criteria-stack.lock remains the common lock until the old CLI
is retired; changing its path here would allow concurrent unsynchronized writes.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

SCAFFOLD_DIR = Path.cwd() / ".scaffold"
STATE_LOCK_FILE = SCAFFOLD_DIR / ".criteria-stack.lock"

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt  # type: ignore


@contextmanager
def state_lock(exclusive: bool = True) -> object:
    SCAFFOLD_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = STATE_LOCK_FILE.open("a+b")
    if lock_handle.tell() == 0:
        lock_handle.write(b"0")
        lock_handle.flush()
    try:
        if fcntl is not None:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_handle.fileno(), mode)
        else:  # pragma: no cover - Windows
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
        yield lock_handle
    finally:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        lock_handle.close()


def atomic_write_json(path: Path, value: object) -> None:
    """Write under the caller's lock; replace only after flushing complete JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, indent=2) + "\n"
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)

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


