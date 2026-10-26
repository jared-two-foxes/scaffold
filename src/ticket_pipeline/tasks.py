"""JSON CLI contract for persistent feature tasks."""

import argparse
import json
import sys
from pathlib import Path

from ticket_pipeline.lib.task_store import operate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in (
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
        "validation-start",
        "validation-record",
        "validation-invalidate",
    ):
        command = commands.add_parser(action)
        command.add_argument("--feature", required=True)
        if action in {"start", "complete", "block", "fail", "decompose", "retry"}:
            command.add_argument("--task", required=True)
        if action in {"init", "add", "complete", "decompose", "validation-start", "validation-record", "validation-invalidate"}:
            command.add_argument("--input", required=True, help="JSON file, or - for stdin")
        if action in {"block", "fail", "retry"}:
            command.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        payload = getattr(args, "reason", None)
        if hasattr(args, "input"):
            text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text("utf-8")
            payload = json.loads(text)
        result = operate(args.action, args.feature, payload, getattr(args, "task", None))
    except (ValueError, OSError) as exc:
        print(json.dumps({"schema_version": 1, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
