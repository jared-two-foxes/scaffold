#!/usr/bin/env python3
"""Scaffold CLI: lifecycle operations at the top level, plus administrative clean."""

import importlib
import sys
from dataclasses import dataclass

from ticket_pipeline.tasks import TASK_ACTIONS


@dataclass(frozen=True)
class _Command:
    module: str
    help: str


_COMMANDS: dict[str, _Command] = {
    **{action: _Command("tasks", "Feature task lifecycle operation") for action in TASK_ACTIONS},
    "clean": _Command("clean", "Reset persisted feature/task state (requires confirmation)"),
}


def _print_top_level_help() -> None:
    print("usage: scaffold <command> [options]\n")
    print("Persistent feature task lifecycle and administrative reset.\n")
    for name, cmd in _COMMANDS.items():
        print(f"  {name:<22} {cmd.help}")
    print("\nRun 'scaffold <command> --help' for command options.")


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        _print_top_level_help()
        sys.exit(0)

    name, rest = argv[0], argv[1:]
    cmd = _COMMANDS.get(name)
    if cmd is None:
        print(f"scaffold: unknown command '{name}'\n", file=sys.stderr)
        _print_top_level_help()
        sys.exit(2)

    module = importlib.import_module(f"ticket_pipeline.{cmd.module}")
    # task lifecycle parser still owns the operation-specific --feature,
    # --task and JSON input schema; no second parser or compatibility route.
    sys.argv = ["scaffold", name, *rest] if cmd.module == "tasks" else ["scaffold clean", *rest]
    module.main()


if __name__ == "__main__":
    main()
