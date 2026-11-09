"""Regression coverage for task-state persistence after retiring the criteria stack."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from ticket_pipeline.lib import state_store
from ticket_pipeline.lib.task_store import operate


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    directory = tmp_path / ".scaffold"
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", directory)
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", directory / ".tasks.lock")
    return directory


def feature():
    return {
        "id": "F1",
        "title": "Feature",
        "objective": "Check task-state persistence",
        "acceptance_criteria": ["Tasks initialize and remain durable"],
        "design_ref": "approved-context",
        "baseline": "a" * 40,
        "branch": "feature/task-store",
    }


def test_atomic_json_write_creates_directory_and_round_trips(state_dir):
    target = state_dir / "tasks.json"
    expected = {"schema_version": 2, "features": {"F": {"tasks": {}}}}

    with state_store.state_lock():
        state_store.atomic_write_json(target, expected)

    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert (state_dir / ".tasks.lock").is_file()
    assert not (state_dir / ".criteria-stack.lock").exists()
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


def test_read_and_write_tasks_use_one_lock(state_dir, monkeypatch):
    entries = []
    underlying_lock = state_store.state_lock

    @contextmanager
    def observe_lock(exclusive=True):
        entries.append(exclusive)
        with underlying_lock(exclusive=exclusive) as handle:
            yield handle

    monkeypatch.setattr(state_store, "state_lock", observe_lock)

    operate("init", "F1", feature())
    listed = operate("list", "F1")

    assert listed["feature"]["id"] == "F1"
    assert entries == [True, True]
    assert (state_dir / ".tasks.lock").is_file()
    assert len(list((state_dir / "features").glob("F1.*/ledger.jsonl"))) == 1
    assert not (state_dir / "tasks.json").exists()
    assert not (state_dir / ".criteria-stack.lock").exists()


def test_existing_ledger_is_readable_after_the_lock_file_is_lost(state_dir):
    original = operate("init", "F1", feature())["feature"]
    (path,) = (state_dir / "features").glob("F1.*/ledger.jsonl")
    before = path.read_bytes()
    (state_dir / ".tasks.lock").unlink()

    result = operate("list", "F1")["feature"]

    assert result == original
    assert path.read_bytes() == before
    assert (state_dir / ".tasks.lock").exists()


def test_historical_criteria_files_are_ignored_and_preserved(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    old_json = state_dir / ".criteria-stack.json"
    old_lock = state_dir / ".criteria-stack.lock"
    old_json.write_bytes(b"malformed historic criteria JSON")
    old_lock.write_bytes(b"historic lock contents")
    original_json = old_json.read_bytes()
    original_lock = old_lock.read_bytes()

    result = operate("init", "F1", feature())
    assert result["feature"]["id"] == "F1"
    assert operate("summary", "F1")["summary"]["id"] == "F1"
    assert old_json.read_bytes() == original_json
    assert old_lock.read_bytes() == original_lock
    assert (state_dir / ".tasks.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires privileges")
@pytest.mark.parametrize("target", ["directory", "lock"])
def test_task_state_refuses_symlinked_lock_paths(state_dir, monkeypatch, target):
    if target == "directory":
        real_dir = state_dir.parent / "real"
        real_dir.mkdir()
        state_dir.symlink_to(real_dir, target_is_directory=True)
    else:
        state_dir.mkdir()
        real_lock = state_dir / "real.lock"
        real_lock.write_bytes(b"x")
        (state_dir / ".tasks.lock").symlink_to(real_lock)
    with pytest.raises(ValueError, match="symbolic link"):
        operate("init", "F1", feature())
    assert not (state_dir / "features").exists()


@pytest.mark.parametrize("action", ["list", "next", "summary", "start", "add"])
def test_actions_on_an_untouched_repository_create_no_state(state_dir, action):
    with pytest.raises(ValueError, match="Unknown feature"):
        operate(action, "F1", [] if action == "add" else None, "F1" if action == "start" else None)
    assert not state_dir.exists()
