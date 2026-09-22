#!/usr/bin/env python3

import importlib
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class _Command:
    module: str
    help: str


_COMMANDS: dict[str, _Command] = {
    "fetch-ticket": _Command("lib.fetch_ticket", "Fetch and print a Linear ticket"),
    "status": _Command("status", "Show ticket and criteria stack status"),
    "stack": _Command("stack", "Manage criteria stack operations"),
}


def _print_top_level_help() -> None:
    print("usage: scaffold <command> [args...]\n")
    print("Ticket access and criteria-stack management only.\n")
    for name, cmd in _COMMANDS.items():
        print(f"  {name:<14} {cmd.help}")
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
    sys.argv = [f"scaffold {name}"] + rest
    module.main()


if __name__ == "__main__":
    main()
