import sys

import pytest

from scaffold_cli import cli
from ticket_pipeline.tasks import TASK_ACTIONS


def test_top_level_help_lists_all_lifecycle_operations_and_clean(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scaffold"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in (*TASK_ACTIONS, "clean"):
        assert command in out
    assert "fetch-ticket" not in out
    assert "tasks " not in out
    assert "status" not in out
    assert "stack" not in out


@pytest.mark.parametrize("command", ["tasks", "fetch-ticket", "status", "stack"])
def test_removed_commands_are_rejected(monkeypatch, capsys, command):
    monkeypatch.setattr(sys, "argv", ["scaffold", command])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert f"unknown command '{command}'" in capsys.readouterr().err


@pytest.mark.parametrize("command", [*TASK_ACTIONS, "clean"])
def test_cli_dispatches_to_supported_subcommands(monkeypatch, capsys, command):
    called = {}

    class DummyModule:
        @staticmethod
        def main():
            called["argv"] = sys.argv[:]
            print("dispatched")

    def fake_import_module(name):
        called["module"] = name
        return DummyModule

    monkeypatch.setattr(cli.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(sys, "argv", ["scaffold", command, "--help"])

    cli.main()

    assert called["module"] == (
        "ticket_pipeline.clean" if command == "clean" else "ticket_pipeline.tasks"
    )
    expected = ["scaffold clean", "--help"] if command == "clean" else [
        "scaffold", command, "--help"
    ]
    assert called["argv"] == expected
    assert capsys.readouterr().out.strip() == "dispatched"


def test_top_level_task_help_uses_action_schema(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scaffold", "summary", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--feature" in out
    assert "summary" in out
