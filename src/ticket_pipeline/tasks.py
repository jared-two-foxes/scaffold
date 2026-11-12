"""JSON CLI contract for persistent feature tasks."""

import argparse
import json
import sys
from pathlib import Path

from ticket_pipeline.lib.task_store import list_features, operate


TASK_ACTIONS = (
    "summary",
    "issue",
    "select",
    "design-select",
    "init",
    "add",
    "list",
    "next",
    "start",
    "complete",
    "block",
    "fail",
    "decompose",
    "retry",
    "design-next",
    "design-record",
    "design-finish",
    "design-reopen",
    "checkpoint-record",
    "issue-resume",
    "delivery-record",
    "validation-start",
    "validation-record",
    "validation-invalidate",
    "direction-approve",
    "design-revise",
    "attempt-record",
    "history-foreign",
    "history-rewrite",
    "failure-record",
    "artifact-put",
    "artifact-get",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in TASK_ACTIONS:
        command = commands.add_parser(action)
        command.add_argument("--feature", required=True)
        if action in {
            "issue",
            "start",
            "complete",
            "block",
            "fail",
            "decompose",
            "retry",
            "design-record",
            "checkpoint-record",
            "issue-resume",
            "delivery-record",
            "attempt-record",
        }:
            command.add_argument("--task", required=True)
        if action in {"design-reopen", "failure-record"}:
            command.add_argument("--task")
        if action in {
            "init",
            "add",
            "complete",
            "decompose",
            "validation-start",
            "validation-record",
            "validation-invalidate",
            "design-record",
            "design-reopen",
            "checkpoint-record",
            "issue-resume",
            "delivery-record",
            "direction-approve",
            "design-revise",
            "attempt-record",
            "history-foreign",
            "history-rewrite",
            "failure-record",
            "artifact-put",
            "artifact-get",
        }:
            command.add_argument("--input", required=True, help="JSON file, or - for stdin")
        if action in {"block", "fail", "retry"}:
            command.add_argument("--reason", required=True)
    commands.add_parser("features", help="List open feature instances (id, phase, validation)")
    args = parser.parse_args()
    if args.action == "features":
        try:
            print(json.dumps(list_features(), indent=2))
        except (ValueError, OSError) as exc:
            print(json.dumps({"schema_version": 3, "error": str(exc)}), file=sys.stderr)
            raise SystemExit(1) from exc
        return
    try:
        payload = getattr(args, "reason", None)
        if hasattr(args, "input"):
            text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text("utf-8")
            payload = json.loads(text)
        result = operate(args.action, args.feature, payload, getattr(args, "task", None))
    except (ValueError, OSError) as exc:
        print(json.dumps({"schema_version": 3, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
