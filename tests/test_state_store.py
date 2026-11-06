"""Regression coverage for task-neutral state persistence and the shared legacy lock."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from ticket_pipeline.lib import stack_store, state_store
from ticket_pipeline.lib.stack_store import CriterionFrame
from ticket_pipeline.lib.task_store import operate


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    directory = tmp_path / ".scaffold"
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", directory)
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", directory / ".criteria-stack.lock")
    monkeypatch.setattr(stack_store, "STACK_FILE", directory / ".criteria-stack.json")
    return directory


def test_atomic_json_write_creates_directory_and_round_trips(state_dir):
    target = state_dir / "tasks.json"
    expected = {"schema_version": 2, "features": {"F": {"tasks": {}}}}

    with state_store.state_lock():
        state_store.atomic_write_json(target, expected)

    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert (state_dir / ".criteria-stack.lock").is_file()
    assert not target.with_suffix(".json.tmp").exists()


def test_interrupted_atomic_replace_preserves_prior_state(state_dir, monkeypatch):
    target = state_dir / "tasks.json"
    original = {"schema_version": 2, "features": {"original": {}}}
    with state_store.state_lock():
        state_store.atomic_write_json(target, original)
    before = target.read_bytes()

    with monkeypatch.context() as patch:
        def fail_replace(_source, _target):
            raise OSError("Simulated interrupted replace")

        patch.setattr(state_store.os, "replace", fail_replace)
        with state_store.state_lock():
            with pytest.raises(OSError, match="Simulated interrupted replace"):
                state_store.atomic_write_json(target, {"features": {"replacement": {}}})

    assert target.read_bytes() == before
    with state_store.state_lock():
        state_store.atomic_write_json(target, {"schema_version": 2, "features": {}})
    assert json.loads(target.read_text(encoding="utf-8"))["features"] == {}


def test_legacy_stack_and_tasks_use_one_shared_lock(state_dir, monkeypatch):
    entered = []
    underlying_lock = state_store.state_lock

    @contextmanager
    def observe_lock(exclusive=True):
        entered.append(exclusive)
        with underlying_lock(exclusive=exclusive) as handle:
            yield handle

    monkeypatch.setattr(state_store, "state_lock", observe_lock)
    stack_store.push_frame(CriterionFrame(ticket="SA-598", criterion="Keep the lock"))
    feature = {
        "id": "F1",
        "title": "Feature",
        "objective": "Check shared persistence",
        "acceptance_criteria": ["State is durable"],
        "design_ref": "approved-context",
        "baseline": "a" * 40,
        "branch": "feature/task-store",
    }
    operate("init", "F1", feature)

    assert entered == [True, True]
    assert (state_dir / ".criteria-stack.lock").is_file()
    assert (state_dir / ".criteria-stack.json").exists()
    assert (state_dir / "tasks.json").exists()


def test_tasks_do_not_read_legacy_criteria_json(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / ".criteria-stack.json").write_text("malformed legacy data", encoding="utf-8")
    feature = {
        "id": "F1",
        "title": "Feature",
        "objective": "Independent of the criteria stack",
        "acceptance_criteria": ["Tasks initialize"],
        "design_ref": "approved-context",
        "baseline": "a" * 40,
        "branch": "feature/task-store",
    }

    result = operate("init", "F1", feature)
    assert result["feature"]["id"] == "F1"
    assert (state_dir / ".criteria-stack.json").read_text(encoding="utf-8") == (
        "malformed legacy data"
    )
