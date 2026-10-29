"""Persisted top-down design, bottom-up implementation, and discovery transitions."""

import pytest
from test_tasks import FEATURE, evidence, task

from ticket_pipeline.lib import stack_store
from ticket_pipeline.lib.task_store import operate


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(stack_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(stack_store, "STACK_LOCK_FILE", tmp_path / ".scaffold/tasks.lock")
    operate(
        "init",
        "F1",
        {
            **FEATURE,
            "baseline": "a" * 40,
            "branch": "feature/test",
        },
    )
    return tmp_path / ".scaffold/tasks.json"


def call(action, identifier=None, payload=None):
    return operate(action, "F1", payload, identifier)


def approve(identifier, parent=False):
    return call(
        "design-record",
        identifier,
        {
            "summary": "Approved contract " + identifier,
            "verification": "Check original criteria",
            "approval": "User approved this design",
            "disposition": "decomposed" if parent else "leaf",
        },
    )


def finish(identifier, checkpoint=True):
    call("start", identifier)
    call("complete", identifier, evidence(identifier))
    if checkpoint:
        call(
            "checkpoint-record",
            identifier,
            {"commit": "a" * 40, "reference": evidence(identifier)["reference"]},
        )


def test_downward_design_then_upward_execution_and_parent_verification(state):
    assert call("design-next")["task"]["id"] == "F1"
    assert call("next")["task"] is None
    call("decompose", "F1", [task("api"), task("storage", ["api"])])
    with pytest.raises(ValueError, match="parent"):
        approve("api")
    approve("F1", True)
    call("decompose", "api", [task("types")])
    approve("api", True)
    assert call("design-next")["task"]["id"] == "types"
    approve("storage")
    with pytest.raises(ValueError, match="Unapproved"):
        call("design-finish")
    with pytest.raises(ValueError, match="design"):
        call("start", "types")
    approve("types")
    assert call("design-next")["task"] is None
    call("design-finish")
    assert call("next")["task"]["id"] == "types"
    finish("types", checkpoint=False)
    assert call("next")["task"] is None
    with pytest.raises(ValueError, match="checkpoints"):
        call("start", "api")
    call(
        "checkpoint-record",
        "types",
        {"commit": "b" * 40, "reference": evidence("types")["reference"]},
    )
    finish("api")
    finish("storage")
    assert call("next")["task"]["id"] == "F1"
    finish("F1")
    assert call("next")["task"] is None


def test_discovery_preserves_definitions_and_requires_explicit_resume(state):
    approve("F1")
    call("design-finish")
    call("start", "F1")
    original = call("list")["feature"]["tasks"]["F1"]
    with pytest.raises(ValueError, match="Return to design"):
        call("decompose", "F1", [task("fix")])
    with pytest.raises(ValueError, match="suspension"):
        call("design-reopen", "F1", {"reason": "Review found prerequisite"})
    reference = "pi-evidence:sha256:" + "a" * 64
    call("design-reopen", "F1", {"reason": "Review found prerequisite", "reference": reference})
    call("decompose", "F1", [{**task("fix"), "origin": {"kind": "review", "reference": reference}}])
    approve("F1", True)
    approve("fix")
    call("design-finish")
    finish("fix")
    assert call("next")["task"] is None
    with pytest.raises(ValueError, match="handoff"):
        call("start", "F1")
    call(
        "issue-resume", "F1", {"reference": reference, "handoff": "pi-evidence:sha256:" + "b" * 64}
    )
    assert call("next")["task"]["id"] == "F1"
    finish("F1")
    current = call("list")["feature"]["tasks"]["F1"]
    assert current["objective"] == original["objective"]
    assert current["acceptance_criteria"] == original["acceptance_criteria"]
    assert current["design_history"] == original["designs"]
    assert any(h.get("suspension") == reference for h in current["history"])


def test_completed_review_adds_dependent_issue_without_rewriting_completed_node(state):
    approve("F1")
    call("design-finish")
    finish("F1")
    root = call("list")["feature"]["tasks"]["F1"]
    call("design-reopen", payload={"reason": "Final review found integration defect"})
    call(
        "add",
        payload=[
            {
                **task("review-1", ["F1"]),
                "origin": {"kind": "review", "reference": "retained-review"},
            }
        ],
    )
    with pytest.raises(ValueError, match="Unapproved"):
        call("design-finish")
    approve("review-1")
    call("design-finish")
    finish("review-1")
    assert call("list")["feature"]["tasks"]["F1"] == root


def test_rejected_changes_are_atomic_and_design_approval_is_durable(state):
    approve("F1")
    before = state.read_bytes()
    with pytest.raises(ValueError, match="Reopen"):
        call(
            "design-record",
            "F1",
            {"summary": "changed", "verification": "v", "approval": "a", "disposition": "leaf"},
        )
    assert state.read_bytes() == before
    call("design-finish")
    before = state.read_bytes()
    with pytest.raises(ValueError, match="Return to design"):
        call("add", payload=[task("unplanned")])
    assert state.read_bytes() == before
    assert (
        call("list")["feature"]["tasks"]["F1"]["designs"][0]["approval"]
        == "User approved this design"
    )


def test_dependency_designs_are_invalidated_and_checkpoint_replays_are_noops(state):
    call("decompose", "F1", [task("a"), task("b", ["a"])])
    approve("F1", True)
    approve("a")
    approve("b")
    call("design-finish")
    call("design-reopen", "a", {"reason": "Contract correction"})
    tasks = call("list")["feature"]["tasks"]
    assert "designs" not in tasks["a"] and "designs" not in tasks["b"]
    assert tasks["b"]["design_history"]
    approve("a")
    approve("b")
    call("design-finish")
    finish("a")
    before = state.read_bytes()
    call("checkpoint-record", "a", {"commit": "a" * 40, "reference": evidence("a")["reference"]})
    assert state.read_bytes() == before
    import json

    assert json.loads(state.read_bytes())["schema_version"] == 2


def test_issue_cli_persists_design_and_phase_across_processes(tmp_path):
    from test_tasks import cli

    feature = {
        **FEATURE,
        "baseline": "a" * 40,
        "branch": "feature/issues",
    }
    created = cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=feature)
    assert created["schema_version"] == 2
    assert cli(tmp_path, "design-next", "--feature", "F1")["task"]["id"] == "F1"
    cli(tmp_path, "start", "--feature", "F1", "--task", "F1", ok=False)
    cli(
        tmp_path,
        "design-record",
        "--feature",
        "F1",
        "--task",
        "F1",
        "--input",
        "-",
        payload={
            "summary": "Unicode approved contract: Māori",
            "verification": "Tests",
            "approval": "User approved",
            "disposition": "leaf",
        },
    )
    cli(tmp_path, "design-finish", "--feature", "F1")
    resumed = cli(tmp_path, "list", "--feature", "F1")["feature"]
    assert resumed["workflow"]["phase"] == "implement"
    assert resumed["tasks"]["F1"]["designs"][0]["approval"] == "User approved"
