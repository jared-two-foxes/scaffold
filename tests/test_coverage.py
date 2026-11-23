"""Requirement links on recursive tasks and design-coverage enforcement."""

import copy

import pytest
from flow import approve_direction
from test_tasks import FEATURE, finish, task

from ticket_pipeline.lib import coverage, ledger, state_store
from ticket_pipeline.lib.task_store import operate

TWO = {
    **FEATURE,
    "requirements": [{"id": "REQ-1", "text": "One"}, {"id": "REQ-2", "text": "Two"}],
}


def linked(identifier, links, deps=()):
    base = {k: v for k, v in task(identifier, deps).items() if k != "requirements_rationale"}
    return {
        **base,
        "requirements": [{"id": i, "roles": list(r)} for i, r in links],
    }


BOTH = ("contribute", "verify")


@pytest.fixture
def feature(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    operate("init", "F1", TWO)
    return ledger


def approve_designs():
    while (selected := operate("design-next", "F1")["task"]) is not None:
        everything = operate("list", "F1")["feature"]["tasks"].values()
        parent = any(t["parent_id"] == selected["id"] for t in everything)
        operate(
            "design-record",
            "F1",
            {
                "summary": "s",
                "verification": "v",
                "approval": "a",
                "disposition": "decomposed" if parent else "leaf",
            },
            selected["id"],
        )


def finish_design():
    approve_designs()
    return operate("design-finish", "F1")


def decompose(tasks, parent="F1"):
    return operate("decompose", "F1", tasks, parent)


# --- shape and identity ------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"requirements": []},
        {"requirements": "REQ-1"},
        {"requirements": [{"id": "REQ-1"}]},
        {"requirements": [{"id": "REQ-1", "roles": []}]},
        {"requirements": [{"id": "REQ-1", "roles": ["own"]}]},
        {"requirements": [{"id": "REQ-1", "roles": ["verify", "verify"]}]},
        {"requirements": [{"id": "REQ-1", "roles": ["verify"], "x": 1}]},
        {"requirements": [{"id": " ", "roles": ["verify"]}]},
        {"requirements": [{"id": "REQ-9", "roles": ["verify"]}]},
        {
            "requirements": [
                {"id": "REQ-1", "roles": ["verify"]},
                {"id": "REQ-1", "roles": ["contribute"]},
            ]
        },
        {"requirements_rationale": " "},
        {"requirements_rationale": "why", "requirements": [{"id": "REQ-1", "roles": ["verify"]}]},
        {},
    ],
)
def test_invalid_links_are_rejected_atomically(feature, bad):
    before = ledger.open_current("F1").path.read_bytes()
    base = {k: v for k, v in task("a").items() if k != "requirements_rationale"}
    with pytest.raises(ValueError):
        decompose([{**base, **bad}])
    assert ledger.open_current("F1").path.read_bytes() == before


def test_links_are_stored_in_canonical_order_and_mapping_is_one_to_many_and_many_to_one(feature):
    decompose(
        [
            linked("a", [("REQ-2", ("verify", "contribute")), ("REQ-1", ("contribute",))]),
            linked("b", [("REQ-1", ("contribute",))]),
            linked("check", [("REQ-1", ("verify",)), ("REQ-2", ("verify",))], deps=["a", "b"]),
        ]
    )
    tasks = operate("list", "F1")["feature"]["tasks"]
    assert tasks["a"]["requirements"] == [
        {"id": "REQ-1", "roles": ["contribute"]},
        {"id": "REQ-2", "roles": ["contribute", "verify"]},
    ]
    finish_design()
    plan = operate("list", "F1")["feature"]["workflow"]["plan"]["requirements"]
    assert plan["REQ-1"] == {"contributors": ["a", "b"], "verifiers": ["check"]}
    assert plan["REQ-2"] == {"contributors": ["a"], "verifiers": ["a", "check"]}


# --- nested roll-up ---------------------------------------------------------


def test_children_may_not_widen_their_parents_scope(feature):
    decompose([linked("p", [("REQ-1", BOTH)])])
    with pytest.raises(ValueError, match="outside its parent p: REQ-2"):
        decompose([linked("c", [("REQ-2", BOTH)])], "p")
    decompose([linked("c", [("REQ-1", BOTH)])], "p")


def test_supporting_parent_cannot_have_requirement_children(feature):
    decompose([task("p")])
    with pytest.raises(ValueError, match="supporting issue"):
        decompose([linked("c", [("REQ-1", BOTH)])], "p")
    decompose([task("c")], "p")  # a supporting child of a supporting parent is fine


def test_nested_many_to_one_and_one_to_many_cover_everything(feature):
    decompose([linked("p", [("REQ-1", BOTH), ("REQ-2", BOTH)]), task("support")])
    decompose(
        [
            linked("x", [("REQ-1", BOTH)]),
            linked("y", [("REQ-1", ("contribute",)), ("REQ-2", BOTH)]),
        ],
        "p",
    )
    finish_design()
    plan = operate("list", "F1")["feature"]["workflow"]["plan"]["requirements"]
    # The parent's own links are roll-up: only leaves contribute, but parent verification counts.
    assert plan["REQ-1"]["contributors"] == ["x", "y"]
    assert plan["REQ-1"]["verifiers"] == ["p", "x"]
    assert plan["REQ-2"]["contributors"] == ["y"]


def test_parent_links_do_not_substitute_for_leaf_coverage(feature):
    decompose([linked("p", [("REQ-1", BOTH), ("REQ-2", BOTH)])])
    decompose([linked("leaf", [("REQ-1", BOTH)])], "p")
    approve_designs()
    with pytest.raises(ValueError) as error:
        operate("design-finish", "F1")
    message = str(error.value)
    assert "REQ-2: missing contributor" in message
    assert "REQ-1: missing" not in message


def test_root_cannot_mask_missing_implementation_but_a_childless_root_owns_everything(feature):
    # Alone, the root is the single executable issue and owns every requirement.
    finish_design()
    assert operate("list", "F1")["feature"]["workflow"]["plan"]["requirements"]["REQ-2"] == {
        "contributors": ["F1"],
        "verifiers": ["F1"],
    }


def test_root_with_only_supporting_children_has_no_coverage(feature):
    decompose([task("support")])
    approve_designs()
    with pytest.raises(ValueError) as error:
        operate("design-finish", "F1")
    for needle in (
        "REQ-1: missing contributor",
        "REQ-1: missing verifier",
        "REQ-2: missing contributor",
        "REQ-2: missing verifier",
    ):
        assert needle in str(error.value)
    assert operate("list", "F1")["feature"]["workflow"]["phase"] == "design"


# --- verification order ------------------------------------------------------


def test_missing_verifier_is_reported_with_its_requirement(feature):
    decompose(
        [
            linked("a", [("REQ-1", ("contribute", "verify"))]),
            linked("b", [("REQ-2", ("contribute",))]),
        ]
    )
    approve_designs()
    with pytest.raises(ValueError, match="REQ-2: missing verifier"):
        operate("design-finish", "F1")


def test_verifier_must_be_ordered_after_the_contributors_it_verifies(feature):
    decompose(
        [
            linked("impl", [("REQ-1", ("contribute",)), ("REQ-2", BOTH)]),
            linked("check", [("REQ-1", ("verify",))]),  # no dependency on impl
        ]
    )
    approve_designs()
    with pytest.raises(ValueError) as error:
        operate("design-finish", "F1")
    assert "REQ-1: invalid verification order, contributor impl" in str(error.value)
    assert "verifier check is not the same as or ordered after any contributor" in str(error.value)


def test_verifier_ordered_after_a_contributor_through_nested_dependencies(feature):
    decompose([task("pre"), linked("grp", [("REQ-1", BOTH), ("REQ-2", BOTH)], deps=["pre"])])
    decompose([linked("impl", [("REQ-1", ("contribute",)), ("REQ-2", ("contribute",))])], "grp")
    # The parent group verifies after its descendant (explicit parent integration verification).
    finish_design()
    plan = operate("list", "F1")["feature"]["workflow"]["plan"]["requirements"]
    assert plan["REQ-1"] == {"contributors": ["impl"], "verifiers": ["grp"]}


def test_parent_inherits_ordering_from_its_children_and_sibling_dependencies(feature):
    decompose(
        [
            linked("a", [("REQ-1", ("contribute",)), ("REQ-2", ("contribute",))]),
            linked("g", [("REQ-1", ("verify",)), ("REQ-2", ("verify",))], deps=["a"]),
        ]
    )
    decompose([task("g1")], "g")
    finish_design()
    assert operate("list", "F1")["feature"]["workflow"]["plan"]["requirements"]["REQ-1"] == {
        "contributors": ["a"],
        "verifiers": ["g"],
    }


def test_independent_self_verifying_contributors_are_valid(feature):
    decompose(
        [
            linked("a", [("REQ-1", BOTH), ("REQ-2", BOTH)]),
            linked("b", [("REQ-1", BOTH)]),
        ]
    )
    finish_design()


def test_a_cycle_is_still_rejected_before_coverage(feature):
    with pytest.raises(ValueError, match="Circular"):
        decompose(
            [
                linked("a", [("REQ-1", BOTH)], deps=["b"]),
                linked("b", [("REQ-2", BOTH)], deps=["a"]),
            ]
        )


# --- reopening, staleness and immutability -------------------------------------


def covered():
    decompose([linked("a", [("REQ-1", BOTH), ("REQ-2", BOTH)])])
    finish_design()
    approve_direction("F1")


def test_reopening_discards_the_plan_and_finish_recomputes_it(feature):
    covered()
    plan = operate("list", "F1")["feature"]["workflow"]["plan"]
    operate("design-reopen", "F1", {"reason": "More work"})
    assert "plan" not in operate("list", "F1")["feature"]["workflow"]
    operate("add", "F1", [task("extra")])
    finish_design()
    assert operate("list", "F1")["feature"]["workflow"]["plan"]["digest"] != plan["digest"]


def test_decomposing_a_leaf_that_was_the_only_contributor_requires_new_leaf_coverage(feature):
    decompose([linked("a", [("REQ-1", BOTH), ("REQ-2", BOTH)])])
    approve_designs()
    decompose([task("only-support")], "a")
    approve_designs()
    with pytest.raises(ValueError, match="REQ-1: missing contributor"):
        operate("design-finish", "F1")
    decompose([linked("real", [("REQ-1", BOTH), ("REQ-2", BOTH)])], "a")
    finish_design()


def test_a_stale_plan_cannot_start_work(feature):
    covered()
    stale = ledger.open_current("F1")
    state = copy.deepcopy(stale.state)
    state["tasks"]["a"]["dependencies"] = []
    state["tasks"]["a"]["requirements"] = [{"id": "REQ-1", "roles": ["contribute", "verify"]}]
    stale.append("test-forgery", {}, state)  # a graph change that bypassed design-finish
    with pytest.raises(ValueError, match="stale"):
        operate("start", "F1", task_id="a")


def test_a_feature_without_a_recorded_plan_cannot_start(feature):
    covered()
    current = ledger.open_current("F1")
    state = copy.deepcopy(current.state)
    del state["workflow"]["plan"]
    current.append("test-forgery", {}, state)
    with pytest.raises(ValueError, match="No design coverage plan"):
        operate("start", "F1", task_id="a")


def test_valid_plan_proceeds_through_the_existing_barriers(feature):
    decompose([linked("a", [("REQ-1", BOTH), ("REQ-2", BOTH)]), task("support")])
    finish_design()
    with pytest.raises(ValueError, match="Approve the feature direction"):
        operate("start", "F1", task_id="a")
    approve_direction("F1")
    finish("a")
    assert operate("list", "F1")["feature"]["tasks"]["a"]["status"] == "completed"


def test_completed_work_keeps_its_links_and_cannot_be_redecomposed(feature):
    decompose([linked("a", [("REQ-1", BOTH), ("REQ-2", BOTH)])])
    finish_design()
    approve_direction("F1")
    finish("a")
    before = copy.deepcopy(operate("list", "F1")["feature"]["tasks"]["a"])
    operate("design-reopen", "F1", {"reason": "Corrective work"})
    with pytest.raises(ValueError, match="completed"):
        decompose([linked("child", [("REQ-1", BOTH)])], "a")
    operate("add", "F1", [task("fix", ["a"])])
    assert operate("list", "F1")["feature"]["tasks"]["a"] == before
    assert coverage.links(before) == {"REQ-1": set(BOTH), "REQ-2": set(BOTH)}


def test_the_design_record_snapshots_the_links_and_history_keeps_them(feature):
    decompose([linked("a", [("REQ-1", BOTH), ("REQ-2", BOTH)])])
    approve_designs()
    record = operate("list", "F1")["feature"]["tasks"]["a"]["designs"][-1]
    assert record["requirements"] == [
        {"id": "REQ-1", "roles": ["contribute", "verify"]},
        {"id": "REQ-2", "roles": ["contribute", "verify"]},
    ]
    operate("design-reopen", "F1", {"reason": "Rework"}, "a")
    task_state = operate("list", "F1")["feature"]["tasks"]["a"]
    assert task_state["design_history"][-1]["requirements"] == record["requirements"]


def test_summary_and_issue_scope_expose_coverage_and_links(feature):
    decompose([linked("a", [("REQ-1", BOTH)]), task("s")])
    summary = operate("summary", "F1")["summary"]
    assert summary["coverage"]["complete"] is False
    assert "REQ-2: missing contributor (no executable issue implements it)" in summary[
        "coverage"
    ]["problems"]
    scope = operate("issue", "F1", task_id="a")["context"]
    assert scope["issue"]["requirements"] == [{"id": "REQ-1", "roles": ["contribute", "verify"]}]
    assert operate("issue", "F1", task_id="s")["context"]["issue"]["requirements_rationale"]


def test_childless_root_does_not_hide_other_issues_unordered_verification(feature):
    # The root stays childless while top-level issues are added beside it.
    operate("add", "F1", [linked("impl", [("REQ-1", ("contribute",))])])
    operate("add", "F1", [linked("check", [("REQ-1", ("verify",))])])  # not after impl
    approve_designs()
    with pytest.raises(ValueError) as error:
        operate("design-finish", "F1")
    assert "REQ-1: invalid verification order, contributor impl" in str(error.value)
    plan = coverage.compute(operate("list", "F1")["feature"])["requirements"]["REQ-1"]
    assert plan == {"contributors": ["F1", "impl"], "verifiers": ["F1", "check"]}


def test_issues_created_before_links_existed_can_still_be_reopened(feature):
    covered()
    current = ledger.open_current("F1")
    state = copy.deepcopy(current.state)
    legacy = state["tasks"]["a"]
    del legacy["requirements"]  # as if created before links existed
    current.append("test-forgery", {}, state)
    operate("design-reopen", "F1", {"reason": "Recover"})
    operate("add", "F1", [task("other")])
    approve_designs()
    with pytest.raises(ValueError, match="REQ-1: missing contributor"):
        operate("design-finish", "F1")
