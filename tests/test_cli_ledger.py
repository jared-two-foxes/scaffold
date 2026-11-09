"""The new ledger actions through the real command line, as Pi invokes them."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_tasks import FEATURE, task

from ticket_pipeline.lib import ledger

SRC = str(Path(__file__).resolve().parents[1] / "src")
A, B, C = "d" * 40, "b" * 40, "c" * 40


def run(root, *args, payload=None, ok=True):
    result = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", *args],
        cwd=root,
        env={**os.environ, "PYTHONPATH": SRC},
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == ok, result.stderr
    return json.loads(result.stdout if ok else result.stderr)


def feature_args(*extra):
    return ("--feature", "F1", *extra)


def test_every_ledger_action_is_reachable_through_the_cli(tmp_path):
    run(tmp_path, "init", *feature_args("--input", "-"), payload=FEATURE)
    run(tmp_path, "decompose", *feature_args("--task", "F1", "--input", "-"), payload=[task("a")])
    for design_task, disposition in (("F1", "decomposed"), ("a", "leaf")):
        run(
            tmp_path,
            "design-record",
            *feature_args("--task", design_task, "--input", "-"),
            payload={
                "summary": "s",
                "verification": "v",
                "approval": "user",
                "disposition": disposition,
            },
        )
    run(tmp_path, "design-finish", *feature_args())
    listed = run(tmp_path, "list", *feature_args())["feature"]
    assert listed["head"] == FEATURE["baseline"] and listed["history"] == []

    run(tmp_path, "start", *feature_args("--task", "a"), ok=False)  # not approved yet
    approval = run(
        tmp_path,
        "direction-approve",
        *feature_args("--input", "-"),
        payload={"scope_digest": listed["direction_scope"], "capabilities": ["cli"]},
    )
    assert approval["feature"]["direction"]["scope_digest"] == listed["direction_scope"]
    run(tmp_path, "start", *feature_args("--task", "a"))

    receipt = run(tmp_path, "artifact-put", *feature_args("--input", "-"), payload={"r": 1})
    assert receipt["reference"].startswith("sha256:")
    back = run(
        tmp_path,
        "artifact-get",
        *feature_args("--input", "-"),
        payload={"reference": receipt["reference"]},
    )
    assert back["artifact"] == {"r": 1}
    run(
        tmp_path,
        "attempt-record",
        *feature_args("--task", "a", "--input", "-"),
        payload={
            "attempt": 1,
            "status": "PASSED",
            "receipt": receipt["reference"],
            "base": FEATURE["baseline"],
            "tree": B,
        },
    )
    run(
        tmp_path,
        "complete",
        *feature_args("--task", "a", "--input", "-"),
        payload={
            "feature_id": "F1",
            "task_id": "a",
            "result": "PASSED",
            "source": "conductor",
            "reference": receipt["reference"],
            "summary": "ok",
        },
    )
    checkpoint = run(
        tmp_path,
        "checkpoint-record",
        *feature_args("--task", "a", "--input", "-"),
        payload={
            "commit": A,
            "parent": FEATURE["baseline"],
            "tree": B,
            "reference": receipt["reference"],
        },
    )
    assert checkpoint["feature"]["head"] == A

    approved = run(tmp_path, "artifact-put", *feature_args("--input", "-"), payload={"ok": "user"})
    foreign = {
        "from": A,
        "to": C,
        "source": "user-commits",
        "commits": [{"commit": C, "parents": [A], "subject": "user"}],
        "approval": approved["reference"],
    }
    after = run(tmp_path, "history-foreign", *feature_args("--input", "-"), payload=foreign)
    assert after["feature"]["head"] == C
    segments = after["feature"]["history"]
    assert [s["kind"] for s in segments] == ["checkpoint", "foreign"]

    rewritten = json.loads(json.dumps(segments))
    rewritten[0].update(commit="1" * 40, tree="2" * 40)
    rewritten[1].update(**{"from": "1" * 40, "to": "3" * 40})
    rewritten[1]["commits"][0].update(commit="3" * 40, parents=["1" * 40])
    proof = run(tmp_path, "artifact-put", *feature_args("--input", "-"), payload={"v": 1})
    result = run(
        tmp_path,
        "history-rewrite",
        *feature_args("--input", "-"),
        payload={
            "old_head": C,
            "new_head": "3" * 40,
            "history": rewritten,
            "verification": proof["reference"],
        },
    )
    assert result["feature"]["head"] == "3" * 40
    assert result["feature"]["epochs"][0]["head"] == C


def test_features_lists_open_instances_and_omits_closed_ones(tmp_path):
    assert run(tmp_path, "features")["features"] == []
    assert not (tmp_path / ".scaffold").exists()  # listing creates nothing in a clean repo
    run(tmp_path, "init", *feature_args("--input", "-"), payload=FEATURE)
    other = {**FEATURE, "id": "F2"}
    run(tmp_path, "init", "--feature", "F2", "--input", "-", payload=other)
    listed = run(tmp_path, "features")["features"]
    assert [f["id"] for f in listed] == ["F1", "F2"]
    assert {f["phase"] for f in listed} == {"design"} and listed[0]["tasks"] == 1
    assert all(len(f["instance_id"]) == 32 for f in listed)
    clean = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", "clean", "--feature", "F1", "--yes"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0, clean.stderr
    assert [f["id"] for f in run(tmp_path, "features")["features"]] == ["F2"]


def test_cli_errors_for_the_new_actions_are_json_and_leave_the_ledger_unchanged(tmp_path):
    run(tmp_path, "init", *feature_args("--input", "-"), payload=FEATURE)
    (path,) = (tmp_path / ".scaffold" / "features").glob("F1.*/ledger.jsonl")
    before = path.read_bytes()
    bad = run(
        tmp_path,
        "direction-approve",
        *feature_args("--input", "-"),
        payload={"scope_digest": "0" * 64},
        ok=False,
    )
    assert bad["schema_version"] == 3 and "does not match" in bad["error"]
    missing = run(
        tmp_path,
        "artifact-get",
        *feature_args("--input", "-"),
        payload={"reference": "sha256:" + "0" * 64},
        ok=False,
    )
    assert "missing" in missing["error"]
    assert path.read_bytes() == before
    assert ledger.Ledger(path.parent).entries[-1]["type"] == "init"


@pytest.mark.parametrize("action", ["attempt-record"])
def test_attempt_record_requires_a_task_on_the_command_line(tmp_path, action):
    run(tmp_path, "init", *feature_args("--input", "-"), payload=FEATURE)
    result = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", action, *feature_args("--input", "-")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": SRC},
        input="{}",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and "--task" in result.stderr
