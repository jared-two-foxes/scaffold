from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

SCAFFOLD_DIR = Path.cwd() / ".scaffold"
STACK_FILE = SCAFFOLD_DIR / ".criteria-stack.json"
STACK_LOCK_FILE = SCAFFOLD_DIR / ".criteria-stack.lock"

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt  # type: ignore


@dataclass
class CriterionFrame:
    ticket: str
    criterion: str
    status: str = "pending"
    origin: str = "ticket"
    plan_context: str = ""


@contextmanager
def _stack_lock(exclusive: bool = True) -> object:
    SCAFFOLD_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = STACK_LOCK_FILE.open("a+b")
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


def _require_string_field(item: dict, key: str, index: int, default: str) -> str:
    value = item.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"Stack entry {index} field '{key}' must be a string")
    return value.strip() or default


def _parse_stack(text: str) -> list[CriterionFrame]:
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("Stack file must contain a JSON list")

    frames: list[CriterionFrame] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Stack entry {index} must be a JSON object")

        if "ticket" not in item or "criterion" not in item:
            raise ValueError(f"Stack entry {index} is missing required fields")

        ticket_raw = item["ticket"]
        criterion_raw = item["criterion"]
        if not isinstance(ticket_raw, str) or not isinstance(criterion_raw, str):
            raise ValueError(f"Stack entry {index} must use string ticket and criterion fields")

        ticket = ticket_raw.strip()
        criterion = criterion_raw.strip()
        if not ticket or not criterion:
            raise ValueError(f"Stack entry {index} has empty required fields")

        frames.append(
            CriterionFrame(
                ticket=ticket,
                criterion=criterion,
                status=_require_string_field(item, "status", index, "pending"),
                origin=_require_string_field(item, "origin", index, "ticket"),
                plan_context=_require_string_field(item, "plan_context", index, ""),
            )
        )
    return frames


def _load_stack_unlocked() -> list[CriterionFrame]:
    if not STACK_FILE.exists():
        return []
    text = STACK_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return _parse_stack(text)


def load_stack() -> list[CriterionFrame]:
    use_exclusive = fcntl is None
    with _stack_lock(exclusive=use_exclusive):
        return _load_stack_unlocked()


def _save_stack_unlocked(frames: list[CriterionFrame]) -> None:
    SCAFFOLD_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STACK_FILE.with_suffix(STACK_FILE.suffix + ".tmp")
    payload = json.dumps([asdict(frame) for frame in frames], indent=2) + "\n"
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, STACK_FILE)

    if os.name != "nt":  # pragma: no cover - platform-specific durability
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            dir_fd = os.open(str(SCAFFOLD_DIR), flags)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def save_stack(frames: list[CriterionFrame]) -> None:
    with _stack_lock():
        _save_stack_unlocked(frames)


def push_frame(frame: CriterionFrame) -> None:
    with _stack_lock():
        stack = _load_stack_unlocked()
        stack.insert(0, frame)
        _save_stack_unlocked(stack)


def pop_frame() -> CriterionFrame | None:
    with _stack_lock():
        stack = _load_stack_unlocked()
        if not stack:
            return None
        top = stack.pop(0)
        _save_stack_unlocked(stack)
        return top


def clear_stack() -> None:
    with _stack_lock():
        _save_stack_unlocked([])
