import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ticket_pipeline.lib import stack_store
from ticket_pipeline.lib.task_store import operate

FEATURE = {
    "id": "F1",
    "title": "Feature",
    "objective": "Deliver feature",
    "design_ref": "docs/approved.md@abc123",
    "acceptance_criteria": ["Works"],
}


def task(identifier, dependencies=()):
    return {
        "id": identifier,
        "title": identifier,
        "objective": "Implement " + identifier,
        "acceptance_criteria": ["Tests pass"],
        "dependencies": list(dependencies),
        "context": "Existing module and design reference",
        "constraints": ["No AI calls"],
    }


def evidence(identifier):
    return {
        "feature_id": "F1",
        "task_id": identifier,
        "result": "PASSED",
        "source": "conductor",
        "reference": "artifacts/run-1.json",
        "summary": "Build, tests and independent review passed (caller supplied)",
    }


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(stack_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(stack_store, "STACK_LOCK_FILE", tmp_path / ".scaffold/tasks.lock")
    operate("init", "F1", FEATURE)
    return tmp_path / ".scaffold/tasks.json"


def finish(identifier):
    operate("start", "F1", task_id=identifier)
    return operate("complete", "F1", evidence(identifier), identifier)["task"]


def test_creation_dependencies_and_repeated_completion(state):
    operate("add", "F1", [task("later", ["first"]), task("first")])
    assert operate("next", "F1")["task"]["id"] == "first"
    with pytest.raises(ValueError):
        operate("start", "F1", task_id="later")
    completed = finish("first")
    before = state.read_bytes()
    assert operate("complete", "F1", evidence("first"), "first")["task"] == completed
    assert state.read_bytes() == before
    assert operate("next", "F1")["task"]["id"] == "later"
    with pytest.raises(ValueError):
        operate("complete", "F1", {**evidence("first"), "reference": "other"}, "first")
    finish("later")
    assert operate("next", "F1")["task"] is None
    assert len(operate("list", "F1")["feature"]["tasks"]) == 2


@pytest.mark.parametrize(
    "batch",
    [
        [task("a"), task("a")],
        [task("a", ["absent"])],
        [task("a", ["a"])],
        [task("a", ["b"]), task("b", ["a"])],
        [task("a", ["b"]), task("b", ["c"]), task("c", ["a"])],
        [task("a", ["b", "b"]), task("b")],
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


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {**evidence("a"), "result": "FAILED"},
        {**evidence("a"), "task_id": "b"},
        {**evidence("a"), "feature_id": "F2"},
        {**evidence("a"), "reference": ""},
    ],
)
def test_requires_successful_bound_evidence(state, bad):
    operate("add", "F1", [task("a")])
    operate("start", "F1", task_id="a")
    with pytest.raises(ValueError):
        operate("complete", "F1", bad, "a")
    assert operate("list", "F1")["feature"]["tasks"]["a"]["status"] == "in_progress"


def test_transitions_and_resume(state):
    operate("add", "F1", [task("a")])
    for action in ("complete", "fail", "retry"):
        with pytest.raises(ValueError):
            operate(action, "F1", evidence("a") if action == "complete" else "reason", "a")
    operate("block", "F1", "Needs decision", "a")
    assert operate("next", "F1")["task"] is None
    operate("retry", "F1", "Decision resolved", "a")
    operate("start", "F1", task_id="a")
    with pytest.raises(ValueError):
        operate("start", "F1", task_id="a")
    operate("fail", "F1", "Tests failed", "a")
    assert operate("next", "F1")["task"] is None
    operate("retry", "F1", "Corrective design", "a")
    result = finish("a")
    assert [e["to"] for e in result["history"]] == [
        "blocked",
        "pending",
        "in_progress",
        "failed",
        "pending",
        "in_progress",
        "completed",
    ]
    for action in ("start", "block", "fail", "retry", "decompose"):
        with pytest.raises(ValueError):
            operate(action, "F1", [task("child")] if action == "decompose" else "reason", "a")


def test_recursive_decomposition_preserves_history_and_orders_work(state):
    operate("add", "F1", [task("pre"), task("parent", ["pre"]), task("after", ["parent"])])
    operate("decompose", "F1", [task("c1"), task("c2", ["c1"])], "parent")
    assert operate("next", "F1")["task"]["id"] == "pre"
    with pytest.raises(ValueError):
        operate("start", "F1", task_id="c1")
    finish("pre")
    finish("c1")
    operate("start", "F1", task_id="c2")
    operate("fail", "F1", "Too large", "c2")
    operate("decompose", "F1", [task("grandchild")], "c2")
    with pytest.raises(ValueError):
        operate("start", "F1", task_id="parent")
    finish("grandchild")
    finish("c2")
    # Additional remaining work does not discard verified children.
    operate("decompose", "F1", [task("c3", ["c2"])], "parent")
    finish("c3")
    result = finish("parent")
    assert result["objective"] == "Implement parent"
    assert result["acceptance_criteria"] == ["Tests pass"]
    assert operate("next", "F1")["task"]["id"] == "after"
    tasks = operate("list", "F1")["feature"]["tasks"]
    assert tasks["c1"]["history"][-1]["evidence"] == evidence("c1")
    assert tasks["c2"]["history"][1]["reason"] == "Too large"


def test_decomposition_rejects_deadlocks_and_blocked_parent_pauses_children(state):
    operate("add", "F1", [task("p"), task("after", ["p"])])
    for dependency in ("p", "after"):
        with pytest.raises(ValueError):
            operate("decompose", "F1", [task("c", [dependency])], "p")
    operate("decompose", "F1", [task("c")], "p")
    operate("block", "F1", "Need design decision", "p")
    assert operate("next", "F1")["task"] is None
    operate("retry", "F1", "Resolved", "p")
    assert operate("next", "F1")["task"]["id"] == "c"


def test_interrupted_atomic_write_recovers_and_releases_lock(state, monkeypatch):
    before = state.read_bytes()
    with monkeypatch.context() as patch:

        def interrupted(*args):
            raise OSError("Simulated interruption before replacement")

        patch.setattr(stack_store.os, "replace", interrupted)
        with pytest.raises(OSError):
            operate("add", "F1", [task("lost")])
    assert state.read_bytes() == before
    assert state.with_suffix(".json.tmp").exists()
    operate("add", "F1", [task("a")])
    assert list(operate("list", "F1")["feature"]["tasks"]) == ["a"]


def cli(root, *args, payload=None, ok=True):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", "tasks", *args],
        cwd=root,
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (0 if ok else 1), result.stderr
    return json.loads(result.stdout if ok else result.stderr)


def test_separate_process_persistence_and_interrupted_session(tmp_path):
    cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)
    cli(tmp_path, "add", "--feature", "F1", "--input", "-", payload=[task("a"), task("b", ["a"])])
    cli(tmp_path, "start", "--feature", "F1", "--task", "a")
    result = cli(tmp_path, "list", "--feature", "F1")
    assert result["feature"]["tasks"]["a"]["status"] == "in_progress"
    cli(tmp_path, "retry", "--feature", "F1", "--task", "a", "--reason", "Session interrupted")
    cli(tmp_path, "start", "--feature", "F1", "--task", "a")
    cli(
        tmp_path,
        "complete",
        "--feature",
        "F1",
        "--task",
        "a",
        "--input",
        "-",
        payload=evidence("a"),
    )
    assert cli(tmp_path, "next", "--feature", "F1")["task"]["id"] == "b"
    cli(tmp_path, "start", "--feature", "F1", "--task", "missing", ok=False)


def test_versions_and_feature_isolation(state):
    operate("init", "F2", {**FEATURE, "id": "F2"})
    operate("add", "F1", [task("a")])
    operate("add", "F2", [task("a")])
    finish("a")
    assert operate("next", "F2")["task"]["status"] == "pending"
    with pytest.raises(ValueError):
        operate("init", "F1", FEATURE)
    data = json.loads(state.read_text())
    data["schema_version"] = 2
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="schema_version"):
        operate("list", "F1")


def test_concurrent_mutations_do_not_lose_tasks(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)

    def add(i):
        return cli(tmp_path, "add", "--feature", "F1", "--input", "-", payload=[task(str(i))])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add, range(8)))
    assert len(cli(tmp_path, "list", "--feature", "F1")["feature"]["tasks"]) == 8


def test_process_death_during_write_releases_os_lock(tmp_path):
    cli(tmp_path, "init", "--feature", "F1", "--input", "-", payload=FEATURE)
    path = tmp_path / ".scaffold/tasks.json"
    before = path.read_bytes()
    code = """
import os
from ticket_pipeline.lib import stack_store
from ticket_pipeline.lib.task_store import operate
stack_store.os.replace = lambda *args: os._exit(17)
operate('add', 'F1', [{'id': 'lost', 'title': 'Lost', 'objective': 'Lost',
                     'acceptance_criteria': ['Lost']}])
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, timeout=15)
    assert result.returncode == 17
    assert path.read_bytes() == before
    cli(tmp_path, "add", "--feature", "F1", "--input", "-", payload=[task("a")])
    assert list(cli(tmp_path, "list", "--feature", "F1")["feature"]["tasks"]) == ["a"]


def test_file_input_json_errors_and_stack_coexistence(tmp_path):
    definition = tmp_path / "feature.json"
    definition.write_text(json.dumps(FEATURE))
    cli(tmp_path, "init", "--feature", "F1", "--input", str(definition))
    definition.write_text("{broken")
    assert "error" in cli(tmp_path, "add", "--feature", "F1", "--input", str(definition), ok=False)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scaffold_cli.cli",
            "stack",
            "push",
            "--ticket",
            "SA-1",
            "--criterion",
            "Old stack",
        ],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", "stack", "clear"], cwd=tmp_path, check=True
    )
    assert cli(tmp_path, "list", "--feature", "F1")["feature"]["id"] == "F1"
