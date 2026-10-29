# ruff: noqa: F811
import json

import pytest
from test_issue_workflow import approve, call, finish, state  # noqa: F401, F811
from test_tasks import task


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


def test_summary_size_is_independent_of_history_and_scope_stamp_detects_state_changes(state):
    approve("F1")
    first = call("summary")["summary"]
    before = state.read_bytes()
    call("summary")
    assert state.read_bytes() == before
    raw = json.loads(before)
    raw["features"]["F1"]["tasks"]["F1"]["history"].append({"diagnostic": "x" * 200000})
    state.write_text(json.dumps(raw))
    second = call("summary")["summary"]
    assert first["state_digest"] != second["state_digest"]
    assert len(json.dumps(first)) == len(json.dumps(second))
    assert "x" * 100 not in json.dumps(call("issue", "F1"))


def test_scoped_cli_issue_and_selection_are_json_and_read_only(tmp_path):
    from test_tasks import FEATURE, cli

    cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)
    state_path = tmp_path / ".scaffold/tasks.json"
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
    raw = json.loads(state.read_bytes())
    raw["features"]["F1"]["tasks"]["sibling"]["history"].append({"output": "z" * 500000})
    state.write_text(json.dumps(raw))
    after = call("issue", "leaf")["context"]
    assert len(json.dumps(before)) == len(json.dumps(after))
    assert len(json.dumps(after)) < len(state.read_bytes()) / 50
