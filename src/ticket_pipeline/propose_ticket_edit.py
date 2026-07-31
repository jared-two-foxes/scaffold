#!/usr/bin/env python3
"""
propose-ticket-edit - Given a prior review-ticket.py run's flagged
concerns, propose a revised ticket that resolves exactly those concerns.

Deliberately standalone, same as review-ticket.py: not wired into
check-ticket.py/resolve-ticket.py/pipeline_lib.py. Reads the
.ticket-review-{ticket_id}.md file review-ticket.py saved (run that
first) rather than re-fetching from Linear, so the proposal is grounded
in exactly the ticket text and concerns that were actually reviewed.

Never touches Linear and never touches .ticket.md - that file stays the
other pipeline scripts' canonical state, untouched by this review/
propose loop until you deliberately feed it in. --ticket-file-out is
required (no implicit default path) precisely so the destination is
always explicit at the call site, not something you have to remember or
look up - point it at the same isolated file across a review/propose
loop, e.g.:

    review-ticket SA-42
    propose-ticket-edit SA-42 --ticket-file-out .ticket-proposed-SA-42.md
    review-ticket SA-42 --ticket-file-in .ticket-proposed-SA-42.md
    propose-ticket-edit SA-42 --ticket-file-out .ticket-proposed-SA-42.md
    ...repeat until review-ticket reports no concerns, then:
    check-ticket SA-42 --ticket-file-in .ticket-proposed-SA-42.md   # try it
    update-ticket SA-42 --ticket-file-in .ticket-proposed-SA-42.md --yes

Pass --no-write to only print the diff/proposal without writing anywhere
(skips needing --ticket-file-out at all).

Usage:
    propose-ticket-edit <ticket-id> [--model <model-id>]
                         (--ticket-file-out <path> | --no-write)
"""

import argparse
import difflib
import re
from pathlib import Path

from .lib import ai_client, pipeline_lib as lib, render, tools, verbosity
from . import review_ticket as review

log = verbosity.get_logger(__name__)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"

PROPOSE_PROMPT_FILE = lib.PROMPTS_DIR / "propose-ticket-edit.prompt.md"
TICKET_DEDUP_KEY = ".ticket.md"

VERDICT_RE = re.compile(r"^###\s*Verdict\s*\n+(\S+)", re.MULTILINE)
NONE_FOUND_RE = re.compile(r"^###\s*Concerns\s*\n+None found\.", re.MULTILINE)


def load_review(ticket_id: str) -> tuple[str, str]:
    """
    Returns (ticket_content, report) parsed back out of the file
    review-ticket.py saved. Dies with a clear pointer if it's missing -
    this script has no fallback path that re-derives concerns itself.
    """
    path = review.review_file_path(ticket_id)
    if not path.exists():
        lib.die(
            f"{path} not found. Run 'review-ticket {ticket_id}' first - "
            f"propose-ticket-edit only resolves concerns an actual review "
            f"already verified against the codebase, it doesn't re-derive them."
        )
    text = path.read_text(encoding="utf-8")
    if review.REVIEW_FILE_REPORT_MARKER not in text:
        lib.die(f"{path} doesn't look like a review-ticket.py output file (missing report marker).")
    ticket_part, _, report_part = text.partition(review.REVIEW_FILE_REPORT_MARKER)
    ticket_content = ticket_part.split(review.REVIEW_FILE_TICKET_MARKER, 1)[-1].strip()
    report = (review.REVIEW_FILE_REPORT_MARKER + report_part).strip()
    return ticket_content, report


def has_concerns(report: str) -> bool:
    verdict_match = VERDICT_RE.search(report)
    verdict = verdict_match.group(1).strip().lower() if verdict_match else None
    if verdict == "clear" and NONE_FOUND_RE.search(report):
        return False
    return True


def build_propose_prompt(ticket_content: str, report: str) -> str:
    instructions = lib.load_prompt_body(PROPOSE_PROMPT_FILE)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the original ticket - already complete and current, no "
        f"need to read_file it again:\n\n{ticket_content}\n\n"
        f"Here is the prior review's report listing the concerns to "
        f"resolve:\n\n{report}\n\n"
        f"Use read_file/list_dir/search_files only if you need to confirm "
        f"an exact file/symbol name while resolving a concern - the "
        f"concerns were already verified against the codebase by the "
        f"review, you don't need to re-verify ones you're not changing "
        f"the wording of. Produce your final response in the exact "
        f"format from Step 4 above - no chat header, no preamble or "
        f"trailing commentary."
    )


def run_propose_step(ticket_content: str, report: str, model: str) -> str:
    try:
        result = lib.run_ai_step_with_retry(
            lambda: ai_client.run_with_tools(
                build_propose_prompt(ticket_content, report),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(allow_write=False, preloaded_paths={TICKET_DEDUP_KEY}),
                "propose-ticket-edit",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "propose-ticket-edit",
        )
    except (ai_client.AIError, tools.PipelineAbort) as e:
        lib.die(str(e))
    if "## Proposed Ticket Revision" not in result.text:
        lib.render_step_output(result.text, level=0)
        lib.die("Editor did not produce a valid proposal (see output above).")
    return result.text


def extract_revised_ticket(proposal_text: str) -> str | None:
    """
    Pulls just the revised ticket body back out of the proposal (between
    the '## Proposed Ticket Revision' heading and the '## Changes Made'
    heading that always follows it per the prompt's output format) so we
    can diff it against the original. Returns None for the
    no-remaining-work case, where there's prose instead of ticket text.

    Deliberately anchors on '## Changes Made' specifically rather than
    the next '## ' of any kind - the revised ticket body itself commonly
    contains its own '## Acceptance Criteria' (or similar) heading, which
    would otherwise truncate the match after just the title/description.
    """
    match = re.search(
        r"^## Proposed Ticket Revision\s*\n(.*?)\n## Changes Made\b",
        proposal_text,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        return None
    body = match.group(1).strip()
    if body.lower().startswith("no revision proposed"):
        return None
    return body


def print_diff(original: str, revised: str) -> None:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        revised.splitlines(keepends=True),
        fromfile="original ticket",
        tofile="proposed ticket",
    )
    diff_text = "".join(diff)
    render.print_line(diff_text if diff_text else "(no textual differences)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Propose a ticket revision that resolves a prior review-ticket.py run's flagged concerns.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("ticket_id", help="Linear ticket ID, e.g. NEB-42 - must match a prior review-ticket run")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"opencode zen model ID to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--ticket-file-out",
        type=Path,
        default=None,
        help="Where to write the proposed revision. Required unless --no-write "
             "is passed - there is no implicit default path, so the destination "
             "is always explicit at the call site rather than something you "
             "have to remember. Pass .ticket.md if you want it written straight "
             "to the canonical pipeline state file instead of an isolated one.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Only print the diff/proposal - don't write the revision anywhere "
             "(makes --ticket-file-out optional).",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info).",
    )
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)
    if args.ticket_file_out is None and not args.no_write:
        parser.error("--ticket-file-out is required (or pass --no-write to skip writing).")

    ticket_content, report = load_review(args.ticket_id)
    if not has_concerns(report):
        render.print_line(f"-- {review.review_file_path(args.ticket_id)} reports no concerns - nothing to propose.")
        return

    proposal_text = run_propose_step(ticket_content, report, args.model)

    render.print_line()
    render.print_line(f"-- Proposed edit for {args.ticket_id}:")
    render.print_line()
    render.print_line(proposal_text)

    revised_ticket = extract_revised_ticket(proposal_text)
    if revised_ticket is not None:
        render.print_line()
        render.print_line("-- Diff against the original ticket:")
        render.print_line()
        print_diff(ticket_content, revised_ticket)

    render.print_line()
    if revised_ticket is None:
        render.print_line("-- Nothing written - this was the no-remaining-work case, not a revision.")
    elif args.no_write:
        render.print_line("-- --no-write passed: not writing the revision anywhere.")
    else:
        out_path = args.ticket_file_out
        tools.write_file_block(str(out_path))(revised_ticket)
        render.print_line(
            f"-- Wrote proposed revision to {out_path}. Nothing was sent to Linear "
            f"and {lib.TICKET_FILE} was not touched. Next:\n"
            f"   review-ticket {args.ticket_id} --ticket-file-in {out_path}   # check whether concerns are resolved\n"
            f"   update-ticket {args.ticket_id} --ticket-file-in {out_path} --yes   # push it to Linear once you're happy"
        )
    render.print_line(f"-- Token usage: {ai_client.usage}")


if __name__ == "__main__":
    main()
