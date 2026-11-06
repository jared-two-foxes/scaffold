import sys

import pytest

from scaffold_cli import cli


def test_top_level_help_lists_only_supported_commands(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scaffold"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "fetch-ticket" in out
    assert "tasks" in out
    assert "clean" in out
    assert "status" not in out
    assert "stack" not in out


@pytest.mark.parametrize("command", ["status", "stack"])
def test_retired_commands_are_rejected(monkeypatch, capsys, command):
    monkeypatch.setattr(sys, "argv", ["scaffold", command])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert f"unknown command '{command}'" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "module"),
    [("fetch-ticket", "ticket_pipeline.lib.fetch_ticket"), ("tasks", "ticket_pipeline.tasks"), ("clean", "ticket_pipeline.clean")],
)
def test_cli_dispatches_to_supported_subcommands(monkeypatch, capsys, command, module):
    called = {}

    class DummyModule:
        @staticmethod
        def main():
            print("dispatched")

    def fake_import_module(name):
        called["module"] = name
        return DummyModule

    monkeypatch.setattr(cli.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(sys, "argv", ["scaffold", command])

    cli.main()

    assert called["module"] == module
    assert capsys.readouterr().out.strip() == "dispatched"
