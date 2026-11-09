"""Top-level clean CLI tests: confirmation and administrative reset boundaries."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from ticket_pipeline import clean
from ticket_pipeline.lib import ledger, state_store
from ticket_pipeline.lib.task_store import operate


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    directory = tmp_path / ".scaffold"
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", directory)
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", directory / ".tasks.lock")
    return tmp_path


def feature(identifier):
    return {
        "id": identifier,
        "title": identifier,
        "objective": "Keep state safe",
        "design_ref": "approved-context",
        "acceptance_criteria": ["Work is verified"],
        "baseline": "a" * 40,
        "branch": "feature/clean-cli",
    }


def initialize(*identifiers):
    for identifier in identifiers:
        operate("init", identifier, feature(identifier))


def ledger_files(workspace):
    return sorted((workspace / ".scaffold" / "features").glob("*/ledger.jsonl"))


def snapshot_ledgers(workspace):
    return {str(path): path.read_bytes() for path in ledger_files(workspace)}


def open_ids(workspace):
    return sorted(led.state["id"] for led in ledger.all_open())


def invoke(workspace, *args, input_text=None):
    """Call the real dispatch in a separate process, with no controlling TTY."""
    return subprocess.run(
        [sys.executable, "-m", "scaffold_cli.cli", "clean", *args],
        cwd=workspace,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class TerminalError(io.StringIO):
    def isatty(self):
        return True


def interactive(monkeypatch, *args, answer="yes\n"):
    terminal_input = TerminalInput(answer)
    terminal_error = TerminalError()
    monkeypatch.setattr(sys, "argv", ["scaffold clean", *args])
    monkeypatch.setattr(sys, "stdin", terminal_input)
    monkeypatch.setattr(sys, "stderr", terminal_error)
    return terminal_error


def test_clean_help_describes_both_reset_scopes(workspace):
    result = invoke(workspace, "--help")
    assert result.returncode == 0
    for text in ("--feature", "--all", "--dry-run", "--yes"):
        assert text in result.stdout
    assert not (workspace / ".scaffold").exists()


def test_dry_run_previews_feature_and_never_changes_anything(workspace):
    initialize("F1", "F2")
    before = snapshot_ledgers(workspace)

    result = invoke(workspace, "--feature", "F1", "--dry-run", input_text="yes\n")

    assert result.returncode == 0, result.stderr
    assert "feature F1" in result.stdout
    assert "Affected features: 1" in result.stdout
    assert "Affected tasks/issues: 1" in result.stdout
    assert "F1: 1 tasks/issues" in result.stdout
    assert str(workspace / ".scaffold" / "features") in result.stdout
    assert "Dry run" in result.stdout
    assert "confirm" not in result.stderr
    assert snapshot_ledgers(workspace) == before


def test_dry_run_all_is_never_destructive_even_with_yes(workspace):
    initialize("F1", "F2")
    before = snapshot_ledgers(workspace)

    result = invoke(workspace, "--all", "--dry-run", "--yes")

    assert result.returncode == 0, result.stderr
    assert "all features" in result.stdout
    assert "Affected features: 2" in result.stdout
    assert "Affected tasks/issues: 2" in result.stdout
    assert snapshot_ledgers(workspace) == before


def test_yes_scoped_clean_closes_only_that_feature_and_deletes_nothing(workspace):
    initialize("F1", "F2")
    before = snapshot_ledgers(workspace)
    git_marker = workspace / ".git" / "HEAD"
    git_marker.parent.mkdir()
    git_marker.write_bytes(b"ref: refs/heads/main\n")
    application = workspace / "app.rs"
    application.write_bytes(b"fn main() {}\n")
    historical = workspace / ".scaffold" / ".criteria-stack.json"
    historical.write_bytes(b"not-json, do not touch")
    legacy = workspace / ".scaffold" / "tasks.json"
    legacy.write_bytes(b"legacy state, never read or modified")
    lock_path = workspace / ".scaffold" / ".tasks.lock"
    lock_before = lock_path.read_bytes()

    result = invoke(workspace, "--feature", "F1", "--yes")

    assert result.returncode == 0, result.stderr
    assert "Closed 1 feature(s) and 1 task/issue" in result.stdout
    assert open_ids(workspace) == ["F2"]
    after = snapshot_ledgers(workspace)
    assert set(after) == set(before)  # every ledger still exists
    for path, content in before.items():  # closing only appends
        assert after[path].startswith(content)
        assert (after[path] == content) == ("F2" in path)
    assert lock_path.is_file() and lock_path.read_bytes() == lock_before
    assert git_marker.read_bytes() == b"ref: refs/heads/main\n"
    assert application.read_bytes() == b"fn main() {}\n"
    assert historical.read_bytes() == b"not-json, do not touch"
    assert legacy.read_bytes() == b"legacy state, never read or modified"


def test_yes_all_closes_everything_without_removing_lock_or_ledgers(workspace):
    initialize("F1", "F2")
    lock_path = workspace / ".scaffold" / ".tasks.lock"
    old_lock = workspace / ".scaffold" / ".criteria-stack.lock"
    old_lock.write_bytes(b"old lock")

    result = invoke(workspace, "--all", "--yes")

    assert result.returncode == 0, result.stderr
    assert "Closed 2 feature(s) and 2 task/issue" in result.stdout
    assert open_ids(workspace) == []
    assert len(ledger_files(workspace)) == 2
    assert lock_path.is_file()
    assert old_lock.read_bytes() == b"old lock"


def test_interactive_confirm_closes_only_the_previewed_feature(workspace, monkeypatch, capsys):
    initialize("F1", "F2")
    terminal_error = interactive(monkeypatch, "--feature", "F1")

    clean.main()

    assert "Type 'yes' to confirm" in terminal_error.getvalue()
    assert "Closed 1 feature(s)" in capsys.readouterr().out
    assert open_ids(workspace) == ["F2"]


@pytest.mark.parametrize("answer", ["no\n", "y\n", "\n", ""])
def test_interactive_decline_or_eof_preserves_state(workspace, monkeypatch, answer):
    initialize("F1")
    before = snapshot_ledgers(workspace)
    terminal_error = interactive(monkeypatch, "--all", answer=answer)

    with pytest.raises(SystemExit) as exc:
        clean.main()

    assert exc.value.code == 1
    assert "Reset cancelled" in terminal_error.getvalue()
    assert snapshot_ledgers(workspace) == before


def test_noninteractive_input_cannot_confirm_without_yes_flag(workspace):
    initialize("F1")
    before = snapshot_ledgers(workspace)

    result = invoke(workspace, "--all", input_text="yes\n")

    assert result.returncode == 1
    assert "Confirmation requires a terminal" in result.stderr
    assert "--yes" in result.stderr
    assert snapshot_ledgers(workspace) == before


def test_missing_feature_is_idempotent_and_noninteractive(workspace):
    initialize("F1")
    before = snapshot_ledgers(workspace)

    result = invoke(workspace, "--feature", "MISSING")

    assert result.returncode == 0, result.stderr
    assert "Nothing to close" in result.stdout
    assert snapshot_ledgers(workspace) == before


def test_empty_all_reset_is_noninteractive_noop(workspace):
    result = invoke(workspace, "--all")

    assert result.returncode == 0, result.stderr
    assert "Nothing to close" in result.stdout
    assert not (workspace / ".scaffold" / "features").exists()
    assert (workspace / ".scaffold" / ".tasks.lock").is_file()


@pytest.mark.parametrize(
    ("args", "expected_code"),
    [
        ((), 2),
        (("--dry-run",), 2),
        (("--feature",), 2),
        (("--all", "--feature", "F1"), 2),
        (("--all", "--task", "F1"), 2),
        (("--feature", "  ", "--yes"), 1),
        (("--feature", "", "--yes"), 1),
        (("--feature", "../escape", "--yes"), 1),
    ],
)
def test_invalid_targets_and_options_fail_without_mutation(workspace, args, expected_code):
    initialize("F1")
    before = snapshot_ledgers(workspace)

    result = invoke(workspace, *args)

    assert result.returncode == expected_code
    assert result.stderr
    assert snapshot_ledgers(workspace) == before


@pytest.mark.parametrize("damage", ["tamper", "garbage"])
def test_corrupt_ledger_is_reported_and_not_rewritten(workspace, damage):
    initialize("F1")
    (path,) = ledger_files(workspace)
    raw = path.read_bytes()
    if damage == "tamper":
        path.write_bytes(raw.replace(b'"objective"', b'"objectivx"', 1))
    else:
        path.write_bytes(b"{not json\n" + raw)
    damaged = path.read_bytes()

    result = invoke(workspace, "--all", "--yes")

    assert result.returncode == 1
    assert "scaffold clean:" in result.stderr
    assert path.read_bytes() == damaged


def test_confirmation_rejects_intervening_normal_task_change(workspace, monkeypatch):
    initialize("F1")
    before = snapshot_ledgers(workspace)
    other = feature("F2")

    def change_state_then_confirm():
        operate("init", "F2", other)
        return True

    monkeypatch.setattr(clean, "_confirm", change_state_then_confirm)
    monkeypatch.setattr(sys, "argv", ["scaffold clean", "--all"])

    with pytest.raises(SystemExit) as exc:
        clean.main()

    assert exc.value.code == 1
    assert open_ids(workspace) == ["F1", "F2"]
    (f1,) = [path for path in before if "F1." in path]
    assert (workspace / ".scaffold").exists() and before[f1] == next(
        p for p in ledger_files(workspace) if "F1." in str(p)
    ).read_bytes()


def test_failed_append_reports_error_without_closing_anything(workspace, monkeypatch):
    initialize("F1")
    before = snapshot_ledgers(workspace)

    def fail(*_args):
        raise OSError("injected interrupted append")

    monkeypatch.setattr(ledger.os, "fsync", fail)
    monkeypatch.setattr(sys, "argv", ["scaffold clean", "--all", "--yes"])

    with pytest.raises(SystemExit) as exc:
        clean.main()

    assert exc.value.code == 1
    assert snapshot_ledgers(workspace) == before
