"""Filesystem and concurrent-writer regression tests for administrative clean.

Each test uses an isolated temporary repository. Subprocess tests exercise the
same lock inode across Python processes on Linux and Windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ticket_pipeline.lib import state_store
from ticket_pipeline.lib.task_reset import apply_reset, plan_reset
from ticket_pipeline.lib.task_store import operate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    directory = tmp_path / ".scaffold"
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", directory)
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", directory / ".tasks.lock")
    return tmp_path


def feature(identifier):
    return {
        "id": identifier,
        "title": identifier,
        "objective": "Test clean locking",
        "design_ref": "test-design",
        "acceptance_criteria": ["Persist state safely"],
        "baseline": "a" * 40,
        "branch": "feature/test-clean",
    }


def state_path(repo):
    return repo / ".scaffold" / "tasks.json"


def init(identifier):
    operate("init", identifier, feature(identifier))


def child(repo, *command):
    return subprocess.Popen(
        [sys.executable, *command],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_cross_process_clean_waits_for_tasks_lock_and_keeps_lock_inode(repo):
    init("F1")
    lock_path = repo / ".scaffold" / ".tasks.lock"
    inode = lock_path.stat().st_ino
    # Child announces its intent before entering the actual CLI.
    script = (
        "import subprocess,sys;"
        "print('READY',flush=True);"
        "sys.exit(subprocess.run("
        "[sys.executable,'-m','scaffold_cli.cli','clean','--feature','F1','--yes']"
        ").returncode)"
    )
    with state_store.state_lock():
        process = child(repo, "-c", script)
        try:
            assert process.stdout.readline().strip() == "READY"
            # The child cannot perform deletion while another process has the lock.
            # Reading stdout here would block, so check state before releasing.
            assert process.poll() is None
            assert "F1" in json.loads(state_path(repo).read_text())["features"]
        finally:
            # The context manager releases the lock even if a process starts late.
            pass
    out, err = process.communicate(timeout=25)
    assert process.returncode == 0, (out, err)
    assert "Removed 1 feature(s)" in out
    assert json.loads(state_path(repo).read_text())["features"] == {}
    assert lock_path.is_file()
    assert lock_path.stat().st_ino == inode


def test_external_task_writer_invalidates_preview_instead_of_being_lost(repo):
    init("F1")
    preview = plan_reset(all_features=True)
    task_file = state_path(repo)
    lock_path = repo / ".scaffold" / ".tasks.lock"
    lock_inode = lock_path.stat().st_ino
    script = (
        "import json,sys;"
        "from ticket_pipeline.lib.task_store import operate;"
        "print('READY',flush=True);"
        "operate('init','F2',json.loads(sys.argv[1]));"
        "print('DONE',flush=True)"
    )
    with state_store.state_lock():
        process = child(repo, "-c", script, json.dumps(feature("F2")))
        assert process.stdout.readline().strip() == "READY"
        assert process.poll() is None
        assert set(json.loads(task_file.read_text())["features"]) == {"F1"}
    output, error = process.communicate(timeout=25)
    assert process.returncode == 0, (output, error)
    assert "DONE" in output
    before = task_file.read_bytes()
    with pytest.raises(ValueError, match="changed since preview"):
        apply_reset(preview)
    assert task_file.read_bytes() == before
    assert set(json.loads(before)["features"]) == {"F1", "F2"}
    assert lock_path.stat().st_ino == lock_inode


def test_untrusted_predictable_temp_symlink_is_never_followed(repo):
    if os.name == "nt":
        pytest.skip("Windows symlinks may require privileges")
    init("F1")
    task_file = state_path(repo)
    before = task_file.read_bytes()
    outside = repo / "unrelated.txt"
    outside.write_bytes(b"OUTSIDE sentinel")
    # Earlier atomic_write_json used to open tasks.json.tmp for writing.
    trap = repo / ".scaffold" / "tasks.json.tmp"
    trap.symlink_to(outside)
    plan = plan_reset(feature_id="F1")

    apply_reset(plan)

    assert json.loads(task_file.read_text())["features"] == {}
    assert task_file.read_bytes() != before
    assert trap.is_symlink()
    assert outside.read_bytes() == b"OUTSIDE sentinel"
    assert not list(trap.parent.glob("tasks.json.*.tmp"))


def test_failed_atomic_replace_leaves_no_temp_and_keeps_prior_state(repo, monkeypatch):
    init("F1")
    task_file = state_path(repo)
    previous = task_file.read_bytes()
    lock_path = repo / ".scaffold" / ".tasks.lock"
    lock_inode = lock_path.stat().st_ino
    legacy = repo / ".scaffold" / ".criteria-stack.json"
    legacy.write_bytes(b"invalid, but must be preserved")

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(state_store.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected replace failure"):
            apply_reset(plan_reset(feature_id="F1"))

    assert task_file.read_bytes() == previous
    assert not list(task_file.parent.glob("tasks.json.*.tmp"))
    assert lock_path.is_file() and lock_path.stat().st_ino == lock_inode
    assert legacy.read_bytes() == b"invalid, but must be preserved"


def test_atomic_serialization_failure_never_creates_temp_files(repo):
    init("F1")
    state = state_path(repo)
    before = state.read_bytes()
    with pytest.raises(TypeError):
        with state_store.state_lock():
            state_store.atomic_write_json(state, {"unserializable": object()})
    assert state.read_bytes() == before
    assert not list(state.parent.glob("tasks.json.*.tmp"))


@pytest.mark.parametrize("entry_type", ["state-directory-file", "lock-directory", "state-json-directory"])
def test_unexpected_path_types_fail_before_cleaning(repo, entry_type):
    directory = repo / ".scaffold"
    if entry_type == "state-directory-file":
        directory.write_bytes(b"do not replace me")
        target = directory
    else:
        directory.mkdir()
        target = directory / (".tasks.lock" if entry_type == "lock-directory" else "tasks.json")
        target.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", "clean", "--all", "--yes"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert target.exists()
    assert (target.is_dir() if entry_type != "state-directory-file" else target.is_file())


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks may require privileges")
@pytest.mark.parametrize("entry", ["state-directory", "lock", "tasks-json"])
def test_symlinked_scaffold_paths_are_rejected(repo, entry):
    directory = repo / ".scaffold"
    outside = repo / "external"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_bytes(b"keep")
    if entry == "state-directory":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        if entry == "lock":
            (directory / ".tasks.lock").symlink_to(outside / "keep.txt")
        else:
            (directory / "tasks.json").symlink_to(outside / "keep.txt")

    result = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", "clean", "--all", "--yes"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert marker.read_bytes() == b"keep"
    assert not (outside / "tasks.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fifo paths are not portable")
def test_fifo_task_lock_is_rejected_without_hanging(repo):
    directory = repo / ".scaffold"
    directory.mkdir()
    os.mkfifo(directory / ".tasks.lock")
    result = subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", "clean", "--all", "--yes"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode != 0
    assert (directory / ".tasks.lock").exists()
