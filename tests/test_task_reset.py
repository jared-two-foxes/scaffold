"""Close-service tests: ``scaffold clean`` closes features and never deletes ledgers."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import replace

import pytest

from ticket_pipeline.lib import ledger, state_store
from ticket_pipeline.lib.task_reset import AffectedFeature, apply_reset, plan_reset
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


def ledger_file(workspace, identifier):
    (path,) = (workspace / "features").glob(f"{identifier}.*/ledger.jsonl")
    return path


def test_preview_is_read_only_and_counts_recursive_issues(workspace):
    init("F1", "F2")
    operate(
        "decompose",
        "F1",
        [
            {
                "id": "child",
                "title": "Child issue",
                "objective": "Implement child",
                "acceptance_criteria": ["Child works"],
            }
        ],
        "F1",
    )
    before = ledger_file(workspace, "F1").read_bytes()

    plan = plan_reset(feature_id="F1")
    assert plan.scope == "feature"
    assert plan.feature_id == "F1"
    assert plan.affected_features == (AffectedFeature(id="F1", task_count=2),)
    assert plan.total_tasks == 2
    assert plan.will_change is True
    assert len(plan.state_digest) == 64
    assert ledger_file(workspace, "F1").read_bytes() == before

    other = operate("list", "F2")["feature"]
    applied = apply_reset(plan)
    assert applied == plan
    # Nothing is deleted: the closed ledger is retained, verifiable and still shows its history.
    kept = ledger.Ledger(ledger_file(workspace, "F1").parent)
    assert kept.entries[-1]["type"] == "feature-close"
    assert kept.state["closed"] == {"reason": "clean"} and len(kept.state["tasks"]) == 2
    assert ledger_file(workspace, "F1").read_bytes().startswith(before)
    assert operate("list", "F2")["feature"] == other
    with pytest.raises(ValueError, match="Unknown feature"):
        operate("list", "F1")


def test_all_closes_every_open_feature_and_keeps_the_lock_file(workspace):
    init("F1", "F2")
    lock_inode = (workspace / ".tasks.lock").stat().st_ino
    applied = apply_reset(plan_reset(all_features=True))
    assert [item.id for item in applied.affected_features] == ["F1", "F2"]
    assert ledger.all_open() == []
    assert len(list((workspace / "features").glob("*/ledger.jsonl"))) == 2
    assert (workspace / ".tasks.lock").stat().st_ino == lock_inode


def test_noop_feature_reset_never_creates_or_rewrites_anything(workspace):
    plan = plan_reset(feature_id="MISSING")
    assert plan.will_change is False and plan.affected_features == ()
    assert apply_reset(plan) == plan
    assert not (workspace / "features").exists()
    init("F1")
    before = ledger_file(workspace, "F1").read_bytes()
    apply_reset(plan_reset(feature_id="MISSING"))
    assert ledger_file(workspace, "F1").read_bytes() == before


def test_all_reset_does_not_create_absent_store(workspace):
    plan = plan_reset(all_features=True)
    assert plan.will_change is False
    apply_reset(plan)
    assert not (workspace / "features").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"feature_id": "F1", "all_features": True},
        {"all_features": False},
        {"feature_id": ""},
        {"feature_id": "  "},
        {"feature_id": "../escape"},
        {"feature_id": 7},
        {"all_features": 1},
    ],
)
def test_reset_requires_exactly_one_valid_scope(workspace, kwargs):
    with pytest.raises(ValueError):
        plan_reset(**kwargs)
    assert not workspace.exists()


def test_intervening_feature_change_aborts_without_closing_anything(workspace):
    init("F1")
    plan = plan_reset(feature_id="F1")
    late = {"id": "late", "title": "Late", "objective": "Late", "acceptance_criteria": ["Done"]}
    operate("add", "F1", [late])
    before = ledger_file(workspace, "F1").read_bytes()
    with pytest.raises(ValueError, match="changed since preview"):
        apply_reset(plan)
    assert ledger_file(workspace, "F1").read_bytes() == before
    assert ledger.open_current("F1") is not None


def test_noop_preview_cannot_close_a_newly_created_feature(workspace):
    plan = plan_reset(feature_id="F1")
    init("F1")
    with pytest.raises(ValueError, match="changed since preview"):
        apply_reset(plan)
    assert ledger.open_current("F1") is not None


def test_stale_plan_after_completed_close_is_rejected(workspace):
    init("F1")
    plan = plan_reset(feature_id="F1")
    apply_reset(plan)
    with pytest.raises(ValueError, match="changed since preview"):
        apply_reset(plan)


def test_forged_or_modified_preview_is_rejected(workspace):
    init("F1", "F2")
    plan = plan_reset(feature_id="F1")
    for forged in (
        replace(plan, total_tasks=99),
        replace(plan, state_digest="0" * 64),
        replace(plan, scope="all", feature_id=None),
        replace(plan, feature_id="F2"),
    ):
        with pytest.raises(ValueError):
            apply_reset(forged)
    with pytest.raises(ValueError, match="valid reset plan"):
        apply_reset(object())
    assert {led.state["id"] for led in ledger.all_open()} == {"F1", "F2"}


@pytest.mark.parametrize("mutation", [b'"objective"', b'"seq":0'])
def test_corrupt_ledger_is_reported_and_never_rewritten(workspace, mutation):
    init("F1")
    path = ledger_file(workspace, "F1")
    path.write_bytes(path.read_bytes().replace(mutation, mutation[:-2] + b"x" + mutation[-1:], 1))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        plan_reset(feature_id="F1")
    with pytest.raises(ValueError):
        plan_reset(all_features=True)
    assert path.read_bytes() == before


def test_failed_append_leaves_the_ledger_unchanged(workspace, monkeypatch):
    init("F1")
    path = ledger_file(workspace, "F1")
    before = path.read_bytes()
    plan = plan_reset(feature_id="F1")

    def fail(*_args):
        raise OSError("injected append failure")

    with monkeypatch.context() as patch:
        patch.setattr(ledger.os, "fsync", fail)
        with pytest.raises(OSError, match="injected append failure"):
            apply_reset(plan)
    assert path.read_bytes() == before
    assert ledger.open_current("F1") is not None
    apply_reset(plan_reset(feature_id="F1"))  # retry succeeds
    assert ledger.open_current("F1") is None


def test_preview_and_apply_both_acquire_exclusive_shared_lock(workspace, monkeypatch):
    init("F1")
    entries = []
    underlying = state_store.state_lock

    @contextmanager
    def observe(exclusive=True):
        entries.append(exclusive)
        with underlying(exclusive=exclusive) as handle:
            yield handle

    monkeypatch.setattr(state_store, "state_lock", observe)
    apply_reset(plan_reset(feature_id="F1"))
    assert entries == [True, True]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_reset_refuses_symlinked_features_directory(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.mkdir()
    (workspace / "features").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        plan_reset(all_features=True)
    assert list(outside.iterdir()) == []


def test_reset_refuses_a_file_where_the_features_directory_belongs(workspace):
    workspace.mkdir()
    (workspace / "features").write_bytes(b"not a directory")
    with pytest.raises(ValueError, match="must be a directory"):
        plan_reset(all_features=True)
    assert (workspace / "features").read_bytes() == b"not a directory"


def test_clean_then_reinitialize_same_id_gets_a_fresh_instance_and_keeps_the_old_one(workspace):
    init("F1")
    old = ledger.open_current("F1")
    old_instance = old.state["workflow"]["instance_id"]
    apply_reset(plan_reset(feature_id="F1"))
    init("F1")
    fresh = ledger.open_current("F1")
    assert fresh.state["workflow"]["instance_id"] != old_instance
    assert fresh.directory != old.directory and old.directory.is_dir()
    assert ledger.Ledger(old.directory).state["closed"] == {"reason": "clean"}
    listed = operate("list", "F1")["feature"]["workflow"]["instance_id"]
    assert listed == fresh.state["workflow"]["instance_id"]


def test_a_closed_feature_accepts_no_further_work(workspace):
    init("F1")
    apply_reset(plan_reset(feature_id="F1"))
    for action in ("add", "start", "next"):
        with pytest.raises(ValueError, match="Unknown feature"):
            payload = [] if action == "add" else None
            operate(action, "F1", payload, "F1" if action == "start" else None)
