import json
import sys

from scaffold_cli import cli
from ticket_pipeline.lib import stack_store
from ticket_pipeline.lib.stack_store import CriterionFrame


def _set_paths(tmp_path, monkeypatch):
    scaffold_dir = tmp_path / ".scaffold"
    monkeypatch.setattr(stack_store, "SCAFFOLD_DIR", scaffold_dir)
    monkeypatch.setattr(
        stack_store, "STACK_FILE", scaffold_dir / ".criteria-stack.json"
    )
    monkeypatch.setattr(
        stack_store, "STACK_LOCK_FILE", scaffold_dir / ".criteria-stack.lock"
    )


def _run_stack(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["scaffold", "stack", *args])
    cli.main()


def test_stack_push_and_list(monkeypatch, capsys, tmp_path):
    _set_paths(tmp_path, monkeypatch)
    _run_stack(monkeypatch, "push", "--ticket", "SA-1", "--criterion", "- [ ] First")
    capsys.readouterr()  # swallow "Pushed 1 frame."

    _run_stack(monkeypatch, "list")
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["ticket"] == "SA-1"
    assert data[0]["criterion"] == "- [ ] First"


def test_stack_pop(monkeypatch, capsys, tmp_path):
    _set_paths(tmp_path, monkeypatch)
    _run_stack(monkeypatch, "push", "--ticket", "SA-1", "--criterion", "- [ ] First")
    capsys.readouterr()

    _run_stack(monkeypatch, "pop")
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["criterion"] == "- [ ] First"

    _run_stack(monkeypatch, "pop")
    assert "Stack is empty" in capsys.readouterr().out


def test_stack_clear(monkeypatch, capsys, tmp_path):
    _set_paths(tmp_path, monkeypatch)
    _run_stack(monkeypatch, "push", "--ticket", "SA-1", "--criterion", "- [ ] First")
    capsys.readouterr()

    _run_stack(monkeypatch, "clear")
    assert "Stack cleared" in capsys.readouterr().out

    _run_stack(monkeypatch, "list")
    assert json.loads(capsys.readouterr().out) == []
