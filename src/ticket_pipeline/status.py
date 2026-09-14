#!/usr/bin/env python3

from ticket_pipeline.lib.stack_store import load_stack


def main() -> None:
    stack = load_stack()
    if not stack:
        print("No active ticket. Stack is empty.")
        return

    top = stack[0]
    print(f"Ticket: {top.ticket}")
    print(f"Criteria remaining: {len(stack)}")
    print()
    print("Current criterion:")
    print(f"- [{top.status} | {top.origin}] {top.criterion}")


if __name__ == "__main__":
    main()
