import sys

import pytest

from scaffold_cli import cli


def test_top_level_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scaffold"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "fetch-ticket" in out
    assert "status" in out
    assert "stack" in out


def test_cli_dispatches_to_subcommand(monkeypatch, capsys):
    called = {}

    class DummyModule:
        @staticmethod
        def main():
            print("dispatched")

    def fake_import_module(name):
        called["module"] = name
        return DummyModule

    monkeypatch.setattr(cli.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(sys, "argv", ["scaffold", "status"])

    cli.main()

    assert called["module"] == "ticket_pipeline.status"
    assert capsys.readouterr().out.strip() == "dispatched"
