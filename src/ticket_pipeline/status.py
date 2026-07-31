#!/usr/bin/env python3
"""
status - show where the pipeline is and what to do next.

Reads .criteria-stack.json (and optionally .pipeline-log.jsonl for recent
failures) and prints a human-readable summary: which ticket is active,
how many criteria remain, what the current criterion needs, and which
scaffold command to run next. No AI calls, no test runs, no network —
purely file reads and git status. Safe to run at any time.
"""

import argparse
import json
import sys
from pathlib import Path

from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.lib import render

# Status constants — mirror next_step.py's definitions. Duplicated as
# literals rather than imported to avoid next_step's import-time side
# effects (it pulls in ai_client, tools, subprocess gates, etc.).
_VALIDATING = lib.VALIDATING_STATUS          # "validating"
_GREEN_UNCONFIRMED = "green-unconfirmed"      # GREEN_UNCONFIRMED_STATUS
_MANUAL_PENDING = "awaiting-manual-impl"      # MANUAL_PENDING_STATUS
_FEEDBACK_READY = lib.FEEDBACK_READY_STATUS


def _truncate(text: str, width: int = 100) -> str:
    """Collapse whitespace and truncate to width with ellipsis."""
    compact = " ".join(text.split())
    return compact if len(compact) <= width else compact[: width - 1] + "…"


def _strip_html_comment(criterion: str) -> str:
    """Remove the trailing <!-- ... --> tag from a criterion bullet."""
    idx = criterion.rfind("<!--")
    if idx != -1:
        return criterion[:idx].rstrip()
    return criterion


def _format_frame_brief(frame: "lib.CriterionFrame", index: int) -> str:
    """One-line summary for a non-top frame in the 'remaining' list."""
    vtag = "manual" if frame.verification == "manual" else "test"
    sttag = frame.status
    text = _truncate(_strip_html_comment(frame.criterion))
    return f"  {index}. [{vtag} | {sttag}] {text}"


def _print_section(title: str) -> None:
    render.print_line()
    render.print_line(title)


def _print_guidance(lines: list[str]) -> None:
    """Print action items with a → prefix."""
    for line in lines:
        render.print_line(f"  → {line}")


def _recent_failures(log_path: Path, ticket: str, limit: int = 3) -> list[dict]:
    """
    Scan .pipeline-log.jsonl (newest first) for failed entries for this
    ticket, returning up to `limit` dicts with block/criterion/error.
    """
    if not log_path.is_file():
        return []
    failures: list[dict] = []
    try:
        # Read all lines, reverse to get newest first
        lines = log_path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("status") != "failed":
                continue
            if entry.get("ticket") and entry["ticket"] != ticket:
                continue
            failures.append(entry)
            if len(failures) >= limit:
                break
    except OSError:
        pass
    return failures


def show_status(show_log: bool = False) -> None:
    stack = lib.load_stack()

    if not stack:
        render.print_line("No active ticket. Stack is empty.")
        render.print_line()
        render.print_line("  → Run 'scaffold push-ticket <id>' to start.")
        return

    # All frames should be for the same ticket (or the last frame is a
    # validating sentinel for a ticket whose real frames are gone).
    ticket = stack[0].ticket
    total = len(stack)

    render.print_line(f"Ticket: {ticket}")
    render.print_line(f"Criteria remaining: {total}")

    frame = stack[0]
    vtag = "manual" if frame.verification == "manual" else "test"

    _print_section("▶ Current criterion:")
    render.print_line(f"  [{vtag} | {frame.status}] {_strip_html_comment(frame.criterion)}")

    # Show plan context if available (truncated)
    if frame.plan_context:
        context = _truncate(frame.plan_context, 200)
        render.print_line(f"  Context: {context}")

    # Show test info if available
    if frame.test_files and frame.test_names:
        render.print_line("  Tests:")
        for f, n in zip(frame.test_files, frame.test_names):
            unconfirmed_tag = ""
            if n in (frame.unconfirmed_tests or []):
                unconfirmed_tag = " — UNCONFIRMED"
            render.print_line(f"    {f} :: {n}{unconfirmed_tag}")

    # Show origin if not the default
    if frame.origin != "ticket":
        render.print_line(f"  Origin: {frame.origin}")
    if frame.status == _FEEDBACK_READY and frame.feedback_target:
        render.print_line(f"  Feedback target: {frame.feedback_target}")
    if frame.feedback:
        render.print_line(f"  Feedback: {_truncate(frame.feedback, 200)}")

    # Dispatch on status to give actionable guidance
    _print_section("Next action:")
    _dispatch_guidance(frame, ticket)

    # Show remaining criteria as a brief list
    if total > 1:
        _print_section(f"Remaining ({total - 1} more):")
        for i, f in enumerate(stack[1:], start=2):
            render.print_line(_format_frame_brief(f, i))

    # Show recent failures if requested
    if show_log:
        failures = _recent_failures(lib.PIPELINE_LOG_FILE, ticket)
        if failures:
            _print_section("Recent failures (from .pipeline-log.jsonl):")
            for f in failures:
                block = f.get("block", "?")
                error = _truncate(f.get("error", ""), 150)
                render.print_line(f"  [{block}] {error}")

    render.print_line()


def _dispatch_guidance(frame: "lib.CriterionFrame", ticket: str) -> None:
    """Print the 'what to do next' guidance for the top frame's state."""
    status = frame.status
    verification = frame.verification

    # --- Validating (ticket validation in progress) ---
    if status == _VALIDATING:
        _print_guidance([
            "Run 'scaffold next-step' to continue ticket validation.",
            "(Re-fetch + re-narrow + lint + full test suite + code review)",
        ])
        return

    if status == _FEEDBACK_READY:
        target = frame.feedback_target or "unknown"
        if target == lib.FEEDBACK_TARGET_TESTER:
            _print_guidance([
                "Run 'scaffold next-step' to roll back the previous test-writing attempt",
                "and re-run the Tester with your queued feedback.",
            ])
            return
        if target == lib.FEEDBACK_TARGET_IMPLEMENTOR:
            _print_guidance([
                "Run 'scaffold next-step' to re-run the Implementor with your queued feedback.",
            ])
            return
        _print_guidance([
            "This criterion has human-only feedback queued.",
            "Fix it by hand, then continue with the normal next command for this criterion.",
        ])
        return

    # --- Green-unconfirmed ---
    if status == _GREEN_UNCONFIRMED:
        _print_guidance([
            "Tests are green but were not confirmed legitimate",
            f"(origin: {frame.origin}).",
            "Inspect the unconfirmed test(s) above, then either:",
            "  scaffold next-step --accept-green   (confirm and advance)",
            "  scaffold next-step                  (re-check; will pause if still green)",
        ])
        return

    # --- Manual criteria ---
    if verification == "manual" and status in ("pending", _MANUAL_PENDING):
        paths = lib.extract_referenced_paths(
            f"{frame.criterion}\n{frame.plan_context}"
        )
        if paths:
            files_str = ", ".join(paths)
            _print_guidance([
                f"Make the change to: {files_str}",
                "Then run: scaffold next-step",
                "(If the file(s) are still unchanged, that rerun lets the pipeline attempt the change automatically.)",
            ])
        else:
            _print_guidance([
                "Make the change described in the criterion, or re-run scaffold next-step to let the pipeline try it.",
                "Afterward, run: scaffold next-step --accept-manual",
                "(No specific file could be identified for mechanical checking.)",
            ])
        return

    # --- Test criteria: pending (WRITE_TEST not yet run) ---
    if status == "pending":
        _print_guidance([
            "Run: scaffold next-step",
            "(Writes a failing test for this criterion, then AI-implements automatically when needed.)",
            "Or skip test generation and hand directly to the Implementor:",
            "  scaffold next-step --skip-test",
            "Or write the test by hand, then run:",
            "  scaffold next-step --manual-test --manual-test-ref <file>::<qualified_test_name>",
            "  (replace placeholders with the real test reference)",
        ])
        return

    # --- Test criteria: test-written (awaiting implementation or re-check) ---
    if status == "test-written":
        if not frame.test_files or not frame.test_names:
            _print_guidance([
                "Run: scaffold next-step",
                "(Test metadata is missing — will retry WRITE_TEST.)",
            ])
            return
        _print_guidance([
            "Run: scaffold next-step    (if still red, AI implements; if green, it advances)",
            "Or require manual implementation:",
            "  scaffold next-step --skip-implementation",
        ])
        return

    # --- Done (shouldn't normally be seen, but handle gracefully) ---
    if status == "done":
        _print_guidance([
            "Criterion is done. Run: scaffold next-step  (pops and advances)",
        ])
        return

    # --- Unknown status ---
    _print_guidance([
        f"Unrecognized status '{status}'. Run: scaffold next-step",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show pipeline status and what to do next.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Reads .criteria-stack.json and prints the current criterion, "
            "what action to take, and remaining criteria. No AI calls, no "
            "test runs — safe to run at any time for a quick check."
        ),
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help="Also show recent failures from .pipeline-log.jsonl for the active ticket.",
    )
    args = parser.parse_args()
    show_status(show_log=args.log)


if __name__ == "__main__":
    main()