#!/usr/bin/env python3

import argparse
import json

from ticket_pipeline.lib.stack_store import CriterionFrame, clear_stack, load_stack, pop_frame, push_frame


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the criteria stack.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List criteria currently on the stack")

    push = sub.add_parser("push", help="Push a criterion frame onto the stack")
    push.add_argument("--ticket", required=True, help="Ticket id (for example SA-123)")
    push.add_argument("--criterion", required=True, help="Criterion text")
    push.add_argument("--status", default="pending", help="Frame status")
    push.add_argument("--origin", default="ticket", help="Frame origin")
    push.add_argument("--plan-context", default="", help="Optional context")

    sub.add_parser("pop", help="Pop the top criterion frame")
    sub.add_parser("clear", help="Clear all criteria from the stack")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "list":
        print(json.dumps([frame.__dict__ for frame in load_stack()], indent=2))
        return

    if args.command == "push":
        push_frame(
            CriterionFrame(
                ticket=args.ticket,
                criterion=args.criterion,
                status=args.status,
                origin=args.origin,
                plan_context=args.plan_context,
            )
        )
        print("Pushed 1 frame.")
        return

    if args.command == "pop":
        frame = pop_frame()
        if frame is None:
            print("Stack is empty.")
            return
        print(json.dumps(frame.__dict__, indent=2))
        return

    if args.command == "clear":
        clear_stack()
        print("Stack cleared.")


if __name__ == "__main__":
    main()
