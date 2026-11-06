"""Legacy criteria-frame storage; shares task-state persistence and locking."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from ticket_pipeline.lib import state_store

STACK_FILE = state_store.SCAFFOLD_DIR / ".criteria-stack.json"


@dataclass
class CriterionFrame:
    ticket: str
    criterion: str
    status: str = "pending"
    origin: str = "ticket"
    plan_context: str = ""


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
    use_exclusive = state_store.fcntl is None
    with state_store.state_lock(exclusive=use_exclusive):
        return _load_stack_unlocked()


def _save_stack_unlocked(frames: list[CriterionFrame]) -> None:
    state_store.atomic_write_json(STACK_FILE, [asdict(frame) for frame in frames])


def save_stack(frames: list[CriterionFrame]) -> None:
    with state_store.state_lock():
        _save_stack_unlocked(frames)


def push_frame(frame: CriterionFrame) -> None:
    with state_store.state_lock():
        stack = _load_stack_unlocked()
        stack.insert(0, frame)
        _save_stack_unlocked(stack)


def pop_frame() -> CriterionFrame | None:
    with state_store.state_lock():
        stack = _load_stack_unlocked()
        if not stack:
            return None
        top = stack.pop(0)
        _save_stack_unlocked(stack)
        return top


def clear_stack() -> None:
    with state_store.state_lock():
        _save_stack_unlocked([])
