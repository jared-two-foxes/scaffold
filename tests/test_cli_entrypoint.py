import sys

import pytest


def test_cli_dispatches_to_ticket_pipeline_subcommands(monkeypatch, capsys):
    from scaffold_cli import cli

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
