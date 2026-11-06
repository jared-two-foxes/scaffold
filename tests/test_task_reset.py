"""Reset-service tests; no destructive administrative CLI is exposed yet."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace

import pytest

from ticket_pipeline.lib import state_store
from ticket_pipeline.lib.task_reset import (
    AffectedFeature,
    apply_reset,
    plan_reset,
)
from ticket_pipeline.lib.task_store import operate


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    state_dir = tmp_path / ".scaffold"
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", state_dir)
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", state_dir / ".tasks.lock")
    return state_dir


def feature(identifier):
    return {
        "id": identifier,
        "title": identifier,
        "objective": "Persist a feature",
        "design_ref": "approved-design",
        "acceptance_criteria": ["Completed and checkpointed"],
        "baseline": "a" * 40,
        "branch": "feature/test",
    }


def init(*identifiers):
    for identifier in identifiers:
        operate("init", identifier, feature(identifier))


def data(workspace):
    return json.loads((workspace / "tasks.json").read_text("utf-8"))


def test_preview_is_read_only_and_counts_recursive_issues(workspace):
    init("F1", "F2")
    operate(
        "decompose",
        "F1",
        [{
            "id": "child",
            "title": "Child issue",
            "objective": "Implement child",
            "acceptance_criteria": ["Child works"],
        }],
        "F1",
    )
    before = (workspace / "tasks.json").read_bytes()

    plan = plan_reset(feature_id="F1")
    assert plan.scope == "feature"
    assert plan.feature_id == "F1"
    assert plan.affected_features == (AffectedFeature(id="F1", task_count=2),)
    assert plan.total_tasks == 2
    assert plan.will_change is True
    assert len(plan.state_digest) == 64
    assert (workspace / "tasks.json").read_bytes() == before

    original_other = data(workspace)["features"]["F2"]
    applied = apply_reset(plan)
    assert applied == plan
    assert data(workspace)["features"] == {"F2": original_other}
    assert (workspace / ".tasks.lock").is_file()


def test_all_feature_reset_writes_empty_schema_v2_only(workspace):
    init("F1", "F2")
    outside = workspace / "unrelated.txt"
    outside.write_bytes(b"do not touch")
    old_stack = workspace / ".criteria-stack.json"
    old_stack.write_bytes(b"invalid old stack")
    old_lock = workspace / ".criteria-stack.lock"
    old_lock.write_bytes(b"previous lock")
    lock_path = workspace / ".tasks.lock"
    lock_before = lock_path.read_bytes()

    plan = plan_reset(all_features=True)
    assert plan.scope == "all"
    assert plan.feature_id is None
    assert plan.affected_features == (
        AffectedFeature(id="F1", task_count=1),
        AffectedFeature(id="F2", task_count=1),
    )
    assert plan.total_tasks == 2
    assert apply_reset(plan) == plan
    assert data(workspace) == {"schema_version": 2, "features": {}}
    assert outside.read_bytes() == b"do not touch"
    assert old_stack.read_bytes() == b"invalid old stack"
    assert old_lock.read_bytes() == b"previous lock"
    assert lock_path.read_bytes() == lock_before

    again = plan_reset(all_features=True)
    assert not again.will_change
    previous = (workspace / "tasks.json").read_bytes()
    apply_reset(again)
    assert (workspace / "tasks.json").read_bytes() == previous


def test_noop_feature_reset_never_creates_or_rewrites_task_state(workspace):
    preview = plan_reset(feature_id="missing")
    assert preview.affected_features == ()
    assert preview.total_tasks == 0
    assert not preview.will_change
    assert (workspace / ".tasks.lock").is_file()
    assert not (workspace / "tasks.json").exists()
    assert apply_reset(preview) == preview
    assert not (workspace / "tasks.json").exists()

    init("F1")
    before = (workspace / "tasks.json").read_bytes()
    missing = plan_reset(feature_id="unknown")
    assert not missing.will_change
    apply_reset(missing)
    assert (workspace / "tasks.json").read_bytes() == before


def test_all_reset_does_not_create_absent_store(workspace):
    plan = plan_reset(all_features=True)
    assert not plan.will_change
    apply_reset(plan)
    assert not (workspace / "tasks.json").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"feature_id": None, "all_features": False},
        {"feature_id": "F1", "all_features": True},
        {"feature_id": ""},
        {"feature_id": " "},
        {"feature_id": 12},
        {"all_features": 1},
    ],
)
def test_reset_requires_exactly_one_valid_scope(workspace, kwargs):
    with pytest.raises(ValueError):
        plan_reset(**kwargs)
    assert not (workspace / "tasks.json").exists()


def test_intervening_feature_change_aborts_without_deleting_anything(workspace):
    init("F1")
    plan = plan_reset(feature_id="F1")
    init("F2")  # A normal task transaction commits after the preview.
    current = (workspace / "tasks.json").read_bytes()

    with pytest.raises(ValueError, match="changed since preview"):
        apply_reset(plan)

    assert (workspace / "tasks.json").read_bytes() == current
    assert set(data(workspace)["features"]) == {"F1", "F2"}
    assert apply_reset(plan_reset(feature_id="F1")).will_change
    assert set(data(workspace)["features"]) == {"F2"}


def test_noop_preview_cannot_erase_newly_created_feature(workspace):
    plan = plan_reset(feature_id="F1")
    assert not plan.will_change
    init("F1")
    before = (workspace / "tasks.json").read_bytes()

    with pytest.raises(ValueError, match="changed since preview"):
        apply_reset(plan)
    assert (workspace / "tasks.json").read_bytes() == before


def test_stale_plan_after_completed_reset_is_rejected(workspace):
    init("F1")
    plan = plan_reset(feature_id="F1")
    apply_reset(plan)
    previous = (workspace / "tasks.json").read_bytes()

    with pytest.raises(ValueError, match="changed since preview"):
        apply_reset(plan)
    assert (workspace / "tasks.json").read_bytes() == previous


def test_forged_or_modified_preview_is_rejected(workspace):
    init("F1")
    plan = plan_reset(feature_id="F1")
    original = (workspace / "tasks.json").read_bytes()
    for changed in (
        replace(plan, total_tasks=5),
        replace(plan, state_digest="0" * 64),
        replace(plan, affected_features=()),
        replace(plan, will_change=False),
    ):
        with pytest.raises(ValueError, match="changed since preview"):
            apply_reset(changed)
    with pytest.raises(ValueError, match="valid reset plan"):
        apply_reset({"scope": "all"})
    assert (workspace / "tasks.json").read_bytes() == original


@pytest.mark.parametrize(
    "bad",
    [
        b"not json",
        b"",
        b'{"schema_version": 1, "features": {}}',
        b'{"schema_version": true, "features": {}}',
        b'{"schema_version": 2, "features": []}',
        b'{"schema_version": 2, "features": {"F1": []}}',
        b'{"schema_version": 2, "features": {"F1": {}}}',
        b'{"schema_version": 2, "features": {"F1": {"tasks": []}}}',
    ],
)
def test_corrupt_or_unsupported_persisted_state_is_never_rewritten(workspace, bad):
    workspace.mkdir()
    path = workspace / "tasks.json"
    path.write_bytes(bad)

    with pytest.raises(ValueError):
        plan_reset(all_features=True)
    with pytest.raises(ValueError):
        plan_reset(feature_id="F1")
    assert path.read_bytes() == bad


def test_atomic_write_failure_preserves_old_task_graph(workspace, monkeypatch):
    init("F1", "F2")
    plan = plan_reset(all_features=True)
    before = (workspace / "tasks.json").read_bytes()
    lock_before = (workspace / ".tasks.lock").read_bytes()
    with monkeypatch.context() as patch:

        def fail_replace(_source, _destination):
            raise OSError("injected replace failure")

        patch.setattr(state_store.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected replace failure"):
            apply_reset(plan)

    assert (workspace / "tasks.json").read_bytes() == before
    assert (workspace / ".tasks.lock").read_bytes() == lock_before
    assert set(data(workspace)["features"]) == {"F1", "F2"}
    apply_reset(plan)
    assert data(workspace) == {"schema_version": 2, "features": {}}


def test_preview_and_apply_both_acquire_exclusive_shared_lock(workspace, monkeypatch):
    init("F1")
    entries = []
    existing = state_store.state_lock

    @contextmanager
    def record_lock(exclusive=True):
        entries.append(exclusive)
        with existing(exclusive=exclusive) as handle:
            yield handle

    monkeypatch.setattr(state_store, "state_lock", record_lock)
    apply_reset(plan_reset(feature_id="F1"))
    assert entries == [True, True]
    assert (workspace / ".tasks.lock").is_file()


def test_reset_refuses_symlinked_task_json(workspace):
    if os.name == "nt":
        pytest.skip("symlink creation on Windows may require privileges")
    workspace.mkdir()
    outside = workspace.parent / "outside.json"
    outside.write_text(json.dumps({"schema_version": 2, "features": {}}))
    (workspace / "tasks.json").symlink_to(outside)
    before = outside.read_bytes()

    with pytest.raises(ValueError, match="symbolic link"):
        plan_reset(all_features=True)

    assert outside.read_bytes() == before
    assert (workspace / "tasks.json").is_symlink()


def test_reset_refuses_directory_instead_of_task_json(workspace):
    workspace.mkdir()
    (workspace / "tasks.json").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        plan_reset(all_features=True)
    assert (workspace / "tasks.json").is_dir()
