# ruff: noqa: F811
import json

import pytest
from test_issue_workflow import approve, call, finish, state  # noqa: F401, F811
from test_tasks import task

from ticket_pipeline.lib import ledger


def test_issue_scope_contains_contracts_and_inherited_dependencies_without_histories(state):
    call("decompose", "F1", [task("pre"), task("parent", ["pre"]), task("unrelated")])
    approve("F1", True)
    approve("pre")
    call("decompose", "parent", [task("leaf")])
    approve("parent", True)
    approve("leaf")
    before = state.read_bytes()
    context = call("issue", "leaf")["context"]
    assert context["issue"]["id"] == "leaf"
    assert [t["id"] for t in context["ancestors"]] == ["parent", "F1"]
    assert [t["id"] for t in context["prerequisites"]] == ["pre"]
    assert context["ancestors"][0]["design"]["summary"] == "Approved contract parent"
    assert '"history"' not in json.dumps(context)
    assert "Implement unrelated" not in json.dumps(context)
    assert state.read_bytes() == before
    with pytest.raises(ValueError, match="Unknown"):
        call("issue", "missing")


def test_scoped_selection_matches_full_selection_and_keeps_null_gates(state):
    assert call("design-select")["context"]["issue"]["id"] == call("design-next")["task"]["id"]
    assert call("select")["context"] is None
    approve("F1")
    call("design-finish")
    assert call("select")["context"]["issue"]["id"] == call("next")["task"]["id"]
    finish("F1", checkpoint=False)
    assert call("select")["context"] is None


def inflate(mutate):
    """Append a chain-valid entry that grows task history, like a long-running feature."""
    import copy

    led = ledger.open_current("F1")
    grown = copy.deepcopy(led.state)
    mutate(grown)
    led.append("test-inflate", {}, grown)


def test_summary_size_is_independent_of_history_and_scope_stamp_detects_state_changes(state):
    approve("F1")
    first = call("summary")["summary"]
    before = state.read_bytes()
    call("summary")
    assert state.read_bytes() == before
    inflate(lambda feature: feature["tasks"]["F1"]["history"].append({"diagnostic": "x" * 200000}))
    second = call("summary")["summary"]
    assert first["state_digest"] != second["state_digest"]
    assert len(json.dumps(first)) == len(json.dumps(second))
    assert "x" * 100 not in json.dumps(call("issue", "F1"))


def test_scoped_cli_issue_and_selection_are_json_and_read_only(tmp_path):
    from test_tasks import FEATURE, cli

    cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)
    (state_path,) = (tmp_path / ".scaffold/features").glob("F1.*/ledger.jsonl")
    before = state_path.read_bytes()
    summary = cli(tmp_path, "summary", "--feature", "F1")["summary"]
    assert summary["phase"] == "design"
    assert cli(tmp_path, "design-select", "--feature", "F1")["context"]["issue"]["id"] == "F1"
    assert (
        cli(tmp_path, "issue", "--feature", "F1", "--task", "F1")["context"]["feature"] == summary
    )
    assert state_path.read_bytes() == before


def test_unrelated_history_does_not_expand_issue_context(state):
    call("decompose", "F1", [task("leaf"), task("sibling")])
    approve("F1", True)
    before = call("issue", "leaf")["context"]
    inflate(lambda feature: feature["tasks"]["sibling"]["history"].append({"output": "z" * 500000}))
    after = call("issue", "leaf")["context"]
    assert len(json.dumps(before)) == len(json.dumps(after))
    assert len(json.dumps(after)) < len(state.read_bytes()) / 50


# --- requirement and traceability projections (SA-661) -----------------------------------------


@pytest.fixture
def two(tmp_path, monkeypatch):
    from test_coverage import TWO

    from ticket_pipeline.lib import state_store
    from ticket_pipeline.lib.task_store import operate

    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    operate("init", "F1", TWO)
    return operate


def test_summary_counts_mapped_and_unmapped_requirements_compactly(two):
    from test_coverage import BOTH, linked

    summary = two("summary", "F1")["summary"]
    assert summary["requirements"] == {
        "total": 2, "mapped": 2, "unmapped": 0, "unmapped_ids": []
    }  # a childless root owns everything
    two("decompose", "F1", [linked("a", [("REQ-1", BOTH)])], "F1")
    summary = two("summary", "F1")["summary"]
    assert summary["requirements"] == {
        "total": 2, "mapped": 1, "unmapped": 1, "unmapped_ids": ["REQ-2"]
    }
    assert summary["coverage"]["plan_status"] == "unrecorded"
    assert "requirements" not in summary["coverage"]


def test_requirements_query_reports_roles_missing_and_evidence_and_filters(two, tmp_path):
    from test_coverage import BOTH, linked

    two("decompose", "F1", [linked("a", [("REQ-1", BOTH)]), linked("v", [("REQ-1", ("verify",))], ["a"])], "F1")
    before = (tmp_path / ".scaffold").rglob("ledger.jsonl")
    snapshot = {p: p.read_bytes() for p in before}
    view = two("requirements", "F1")
    assert view["schema"] == "scaffold.requirements/v1"
    first, second = view["requirements"]
    assert (first["id"], first["mapped"], first["missing"]) == ("REQ-1", True, [])
    assert [h["id"] for h in first["contributors"]] == ["a"]
    assert [h["id"] for h in first["verifiers"]] == ["a", "v"]
    assert first["contributors"][0]["evidence"] is None
    assert (second["id"], second["missing"]) == ("REQ-2", ["contribute", "verify"])
    assert second["problems"] and all(p.startswith("REQ-2:") for p in second["problems"])
    only = two("requirements", "F1", {"ids": ["REQ-2"]})
    assert [r["id"] for r in only["requirements"]] == ["REQ-2"]
    unmapped = two("requirements", "F1", {"unmapped": True})
    assert [r["id"] for r in unmapped["requirements"]] == ["REQ-2"]
    with pytest.raises(ValueError, match="Unknown requirement ids"):
        two("requirements", "F1", {"ids": ["NOPE"]})
    with pytest.raises(ValueError, match="accepts"):
        two("requirements", "F1", {"bogus": 1})
    assert {p: p.read_bytes() for p in snapshot} == snapshot
    # reflects graph changes on the next read
    two("add", "F1", [linked("b", [("REQ-2", BOTH)])])
    assert two("requirements", "F1", {"unmapped": True})["requirements"] == []


def test_issue_scope_has_scoped_requirements_inherited_constraints_and_binding(two):
    from test_coverage import BOTH, linked

    two("decompose", "F1", [linked("p", [("REQ-1", BOTH), ("REQ-2", BOTH)]), linked("o", [("REQ-2", BOTH)])], "F1")
    two("decompose", "F1", [linked("leaf", [("REQ-1", BOTH)])], "p")
    root = two("issue", "F1", task_id="F1")["context"]
    assert [r["id"] for r in root["requirements"]] == ["REQ-1", "REQ-2"]
    assert root["requirements"][0]["text"] == "One"
    leaf = two("issue", "F1", task_id="leaf")["context"]
    assert leaf["requirements"] == [
        {"id": "REQ-1", "text": "One", "roles": ["contribute", "verify"],
         "contributors": ["leaf"], "verifiers": ["leaf", "p"]}
    ]
    assert "Two" not in json.dumps(leaf["requirements"])
    assert [c["from"] for c in leaf["inherited_constraints"]] == ["p"]
    assert leaf["binding"]["plan_status"] == "unrecorded"
    assert leaf["binding"]["phase"] == "design" and leaf["binding"]["design_recorded"] is False
    assert len(leaf["binding"]["direction_scope"]) == 64
    assert "ancestors" in leaf and "o" not in [t["id"] for t in leaf["ancestors"]]


def test_requirements_cli_is_json_with_filters(tmp_path):
    from test_tasks import FEATURE, cli

    cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)
    out = cli(tmp_path, "requirements", "--feature", "F1", "--requirement", "REQ-1")
    assert [r["id"] for r in out["requirements"]] == ["REQ-1"]
    assert cli(tmp_path, "requirements", "--feature", "F1", "--unmapped")["requirements"] == []
    assert cli(tmp_path, "summary", "--feature", "F1")["summary"]["requirements"]["total"] == 1
