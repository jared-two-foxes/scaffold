#!/usr/bin/env python3
"""
scaffold - single entry point dispatching to ticket-pipeline's per-stage
CLI tools, so the individual script names (push_ticket, review-ticket,
next_step, ...) don't have to be remembered or typed directly.

Each subcommand's own argparse/main() is untouched by this module - it
only resolves a subcommand name to a module, imports that one module
lazily (never the others, to avoid tripping any of their import-time
side effects), and forwards the remaining argv into its main() exactly
as if that script had been run directly. `scaffold <name> --help`
therefore shows that subcommand's real flags, not a dispatcher-level
summary.
"""

import importlib
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class _Command:
    module: str
    help: str
    has_argparse: bool = True


_GROUPS: list[tuple[str, dict[str, _Command]]] = [
    ("Ticket review (manual)", {
        "review-ticket": _Command(
            "review_ticket",
            "Check a ticket's claims against the actual codebase (read-only report)",
        ),
        "propose-ticket-edit": _Command(
            "propose_ticket_edit",
            "Rewrite a ticket to resolve review-ticket's flagged concerns",
        ),
    }),
    ("Seed & run the criteria loop", {
        "status": _Command(
            "status",
            "Show where the pipeline is and what to do next (no AI, no tests)",
        ),
        "push-ticket": _Command(
            "push_ticket",
            "Fetch a ticket, plan+narrow it, and seed .criteria-stack.json",
        ),
        "next-step": _Command(
            "next_step",
            "Advance the criteria stack by one phase (re-run to keep moving)",
        ),
        "give-feedback": _Command(
            "give_feedback",
            "Queue feedback on the top frame for an automated retry on next-step",
        ),
    }),
    ("Ticket restructuring (manual)", {
        "split-ticket": _Command(
            "split_ticket",
            "Assess a ticket for complexity and propose child tickets if too large",
        ),
        "create-child-tickets": _Command(
            "create_child_tickets",
            "Turn split-ticket's proposed children into real Linear sub-issues",
        ),
        "update-ticket": _Command(
            "update_ticket",
            "Push a locally revised ticket file back to the live Linear ticket",
        ),
    }),
    ("Utilities", {
        "list-models": _Command(
            "list_models",
            "List the models available from a configured ai_client provider",
        ),
        "reset-pipeline": _Command(
            "reset_pipeline",
            "Clear .criteria-stack.json and pipeline scratch files (dry-run by default)",
        ),
        "reset-criterion": _Command(
            "reset_criterion",
            "Roll the top criterion back: git reset --hard to its pre-WRITE_TEST commit, return the frame to pending (git_workflow only)",
        ),
        "reset-workflow": _Command(
            "reset_workflow",
            "Abandon the current ticket: revert to base branch, delete ticket/<id>, clear all pipeline state (dry-run by default)",
        ),
    }),
    ("Advanced / internal (situational, not everyday pipeline steps)", {
        "copilot-login": _Command(
            "copilot_login",
            "One-time device-flow OAuth login for the GitHub Copilot provider",
            has_argparse=False,
        ),
        "fetch-ticket": _Command(
            "lib.fetch_ticket",
            "Fetch and render a single Linear ticket by id",
            has_argparse=False,
        ),
        "bench": _Command(
            "bench",
            "Run a pipeline_lib block N times per model and report pass-rate/cost",
        ),
        "bench-block": _Command(
            "bench_block",
            "Run one pipeline_lib block once against fixed fixtures (used by bench)",
        ),
    }),
]

_COMMANDS: dict[str, _Command] = {
    name: cmd for _, group in _GROUPS for name, cmd in group.items()
}


def _print_top_level_help() -> None:
    print("usage: scaffold <command> [args...]\n")
    print("Linear-ticket-driven TDD pipeline.\n")
    for group_name, group in _GROUPS:
        print(f"{group_name}:")
        for name, cmd in group.items():
            print(f"  {name:<22} {cmd.help}")
        print()
    print("Run 'scaffold <command> --help' for a command's own options.")


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

    if not cmd.has_argparse and rest[:1] in (["-h"], ["--help"]):
        print(f"scaffold {name}: {cmd.help}")
        print("(no detailed --help available for this command)")
        sys.exit(0)

    module = importlib.import_module(f"ticket_pipeline.{cmd.module}")
    sys.argv = [f"scaffold {name}"] + rest
    module.main()


if __name__ == "__main__":
    main()
