"""Transactions and CLI tests for the mandatory issue graph."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from flow import approve_direction, record_attempt, record_checkpoint, reference_for

from ticket_pipeline.lib import ledger, state_store
from ticket_pipeline.lib.task_store import operate

FEATURE = {
    "id": "F1",
    "title": "Feature",
    "objective": "Deliver feature",
    "design_ref": "approved-design",
    "acceptance_criteria": ["Works"],
    "baseline": "a" * 40,
    "branch": "feature/test",
}


def task(identifier, dependencies=()):
    return {
        "id": identifier,
        "title": identifier,
        "objective": "Implement " + identifier,
        "acceptance_criteria": ["Tests pass"],
        "dependencies": list(dependencies),
        "context": "Verification: focused tests",
        "constraints": ["No AI calls"],
    }


def evidence(identifier):
    return {
        "feature_id": "F1",
        "task_id": identifier,
        "result": "PASSED",
        "source": "conductor",
        "reference": reference_for(identifier),
        "summary": "Caller-supplied verification",
    }


def approve_all():
    feature = operate("list", "F1")["feature"]
    if feature["workflow"]["phase"] == "design":
        while (selected := operate("design-next", "F1")["task"]) is not None:
            parent = any(t["parent_id"] == selected["id"] for t in feature["tasks"].values())
            operate(
                "design-record",
                "F1",
                {
                    "summary": "Approved contract",
                    "verification": "Tests",
                    "approval": "User confirmed",
                    "disposition": "decomposed" if parent else "leaf",
                },
                selected["id"],
            )
        operate("design-finish", "F1")


def finish(identifier):
    approve_all()
    operate("start", "F1", task_id=identifier)
    record_attempt("F1", identifier)
    result = operate("complete", "F1", evidence(identifier), identifier)["task"]
    record_checkpoint("F1", identifier)
    return result


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    operate("init", "F1", FEATURE)
    approve_direction("F1")
    # A previously verified root lets these unit tests add independent corrective issues.
    finish("F1")
    operate("design-reopen", "F1", {"reason": "Plan corrective issues"})
    return ledger.open_current("F1").path


def cli(root, *args, payload=None, ok=True):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", *args],
        cwd=root,
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (0 if ok else 1), result.stderr
    return json.loads(result.stdout if ok else result.stderr)


@pytest.mark.parametrize(
    "batch",
    [
        [task("a"), task("a")],
        [task("a", ["absent"])],
        [task("a", ["a"])],
        [task("a", ["b"]), task("b", ["a"])],
        [task("a", ["b", "b"])],
        [{**task("a"), "status": "completed"}],
        [{**task("a"), "acceptance_criteria": []}],
        [None],
        [],
    ],
)
def test_invalid_batch_is_atomic(state, batch):
    before = state.read_bytes()
    with pytest.raises(ValueError):
        operate("add", "F1", batch)
    assert state.read_bytes() == before


def test_dependencies_and_completion_require_design_then_checkpoints(state):
    operate("add", "F1", [task("later", ["first"]), task("first")])
    assert operate("next", "F1")["task"] is None
    approve_all()
    with pytest.raises(ValueError):
        operate("start", "F1", task_id="later")
    finish("first")
    assert operate("next", "F1")["task"]["id"] == "later"
    finish("later")
    before = state.read_bytes()
    operate("complete", "F1", evidence("later"), "later")
    assert state.read_bytes() == before
    with pytest.raises(ValueError):
        operate("complete", "F1", {**evidence("later"), "reference": "other"}, "later")


def test_legacy_tasks_json_is_ignored_and_never_modified(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    legacy = tmp_path / ".scaffold/tasks.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"schema_version": 2, "features": {"OLD": {"id": "OLD"}}}))
    before = legacy.read_bytes()
    with pytest.raises(ValueError, match="legacy .scaffold/tasks.json"):
        operate("list", "OLD")
    operate("init", "F1", FEATURE)
    assert operate("list", "F1")["schema_version"] == 3
    assert legacy.read_bytes() == before


def test_a_ledger_without_the_issue_workflow_is_rejected_unmodified(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    forged = ledger.create("F1", "a" * 32)
    forged.append("init", {}, {"id": "F1", "tasks": {}, "workflow": {"instance_id": "a" * 32}})
    before = forged.path.read_bytes()
    with pytest.raises(ValueError, match="issue graph"):
        operate("start", "F1", task_id="F1")
    assert forged.path.read_bytes() == before


def test_tampered_ledger_blocks_every_operation_without_modification(state):
    data = state.read_bytes()
    state.write_bytes(data.replace(b'"objective"', b'"objectivx"', 1))
    before = state.read_bytes()
    for action in ("list", "next", "summary"):
        with pytest.raises(ValueError):
            operate(action, "F1")
    with pytest.raises(ValueError):
        operate("add", "F1", [task("a")])
    assert state.read_bytes() == before


def test_process_persistence_and_concurrent_adds(tmp_path):
    init = cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)
    assert init["schema_version"] == 3
    assert init["feature"]["workflow"]["phase"] == "design"
    assert list(init["feature"]["tasks"]) == ["F1"]

    def add(i):
        return cli(tmp_path, "add", "--feature", "F1", "--input", "-", payload=[task(str(i))])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add, range(8)))
    assert len(cli(tmp_path, "list", "--feature", "F1")["feature"]["tasks"]) == 9
    cli(tmp_path, "start", "--feature", "F1", "--task", "0", ok=False)


def test_interrupted_append_leaves_the_ledger_unchanged_and_recovers(state, monkeypatch):
    before = state.read_bytes()
    for failing in ("write", "fsync"):
        with monkeypatch.context() as patch:

            def interrupted(*args):
                raise OSError("Simulated append interruption")

            patch.setattr(ledger.os, failing, interrupted)
            with pytest.raises(OSError):
                operate("add", "F1", [task("lost")])
        assert state.read_bytes() == before
    operate("add", "F1", [task("a")])
    assert "a" in operate("list", "F1")["feature"]["tasks"]
    assert "lost" not in operate("list", "F1")["feature"]["tasks"]


def test_missing_baseline_branch_and_old_selector_cannot_create_an_alternate_flow(tmp_path):
    for payload in (
        {k: v for k, v in FEATURE.items() if k != "baseline"},
        {k: v for k, v in FEATURE.items() if k != "branch"},
        {**FEATURE, "workflow": "recursive-issues/v1"},
    ):
        cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=payload, ok=False)
        assert not (tmp_path / ".scaffold/features").exists()


@pytest.mark.parametrize("kind", [[], {}, None, 42, True, ["design"]])
def test_malformed_origin_returns_json_error_and_preserves_state(tmp_path, kind):
    cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)
    (path,) = (tmp_path / ".scaffold/features").glob("F1.*/ledger.jsonl")
    before = path.read_bytes()
    result = cli(
        tmp_path,
        "decompose",
        "--feature",
        "F1",
        "--task",
        "F1",
        "--input",
        "-",
        payload=[{**task("bad"), "origin": {"kind": kind, "reference": "receipt"}}],
        ok=False,
    )
    assert result["schema_version"] == 3
    assert "origin" in result["error"].lower()
    assert path.read_bytes() == before
