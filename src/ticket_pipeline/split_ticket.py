#!/usr/bin/env python3
"""
split-ticket - Assess a Linear ticket for implementation complexity and, if
it's too large for a single pass, propose a set of focused child tickets.

Deliberately standalone: not called by check-ticket.py, resolve-ticket.py,
or pipeline_lib.py, and does not write .ticket.md or any other pipeline
state file. This is a human-facing proposal, not a pipeline step - it never
creates tickets in Linear itself and never feeds its output into plan/narrow.

Why this exists: the resolve-ticket.py loop assumes each ticket maps to one
coherent implementation pass. Tickets that span multiple unrelated concerns,
touch several distinct areas of the codebase, or carry 5+ acceptance criteria
routinely cause the planner to produce vague or over-broad plans and the
implementer to thrash. The fix is to split the ticket before it reaches the
pipeline - this script surfaces that signal and proposes the split.

Complexity is assessed in two stages:

  1. Mechanical pre-check (no AI): count acceptance criteria and detect
     conjunctive scope signals in the title/description ("and also", "as well
     as", multiple unrelated nouns). Only a trivially simple verdict (too few
     criteria, no conjunction signals) short-circuits and reports without
     spending a token. Tickets flagged as ambiguous OR obviously overloaded
     both proceed to the AI step below - even an "obviously overloaded"
     ticket still needs the codebase-cohesion mapping in step 2 to produce a
     trustworthy split proposal, so the mechanical check never skips straight
     to a split verdict.

  2. AI complexity review (read-only tools): the model reads the ticket,
     maps each acceptance criterion to the codebase area it would touch, and
     judges whether those areas are cohesive enough for a single pass. If not,
     it proposes a split - each child ticket gets a title, a description, and
     its own acceptance criteria carved from the parent.

Saves the ticket text plus the complexity report/split proposal to
.ticket-split-{ticket_id}.md so a human can read it, copy the child ticket
bodies, and create them in Linear manually (or pipe them into a future
create-child-tickets.py).

Re-entrant via --ticket-file-in: review a local file instead of fetching
from Linear (same flag convention as review-ticket.py / check-ticket.py).

--review-file-in optionally feeds a prior review-ticket.py report into the
prompt as grounding context - what it already confirmed exists in the
codebase - so the complexity assessment and child-ticket descriptions
don't have to re-derive facts a previous step already checked. Purely
additional context: it never changes which criteria end up in which
child, or whether the verdict is a split at all - review-ticket.py
already strips out any criterion it finds fully satisfied before this
ticket ever reaches split-ticket.py - review-ticket.py already strips out
any criterion it finds fully satisfied before this ticket is pushed, so
there's nothing here to react to on that front.

Usage:
    split-ticket <ticket-id> [--model <model-id>] [--ticket-file-in <path>]
                             [--force-ai] [--threshold <n>] [--review-file-in <path>]
"""

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

from .lib import ai_client, pipeline_lib as lib, render, tools, verbosity

log = verbosity.get_logger(__name__)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"

# Tickets with fewer than this many acceptance criteria are always considered
# simple - the AI step is skipped unless --force-ai is passed.
SIMPLE_THRESHOLD = 2

# Tickets with this many or more acceptance criteria are always sent for an
# AI review, even if no other complexity signals are present.
COMPLEX_THRESHOLD = 5

SPLIT_PROMPT_FILE = lib.PROMPTS_DIR / "split-ticket.prompt.md"
TICKET_DEDUP_KEY = ".ticket.md"

SPLIT_FILE_TICKET_MARKER = "## Assessed Ticket"
SPLIT_FILE_REPORT_MARKER = "## Complexity Assessment"

# Conjunctive phrases in the title/description that suggest multiple concerns
# are bundled into one ticket. Checked mechanically, no AI required.
SCOPE_CONJUNCTION_RE = re.compile(
    r"\b(and also|as well as|additionally|in addition|plus also|"
    r"on top of that|alongside|while also|at the same time)\b",
    re.IGNORECASE,
)

# Acceptance criterion bullet pattern - lines starting with -, *, [ ], [x]
# or numbered list items (1., 2., etc.)
AC_LINE_RE = re.compile(r"^\s*(?:[-*]|\[[ xX]\]|\d+\.)\s+\S")


# ---------------------------------------------------------------------------
# Mechanical pre-check (no AI)
# ---------------------------------------------------------------------------

def extract_acceptance_criteria(ticket_text: str) -> list[str]:
    """
    Returns the list of acceptance criterion lines found in the ticket text.
    Looks for a heading that contains 'acceptance criteria' (case-insensitive)
    and collects bullet/numbered lines below it until the next heading or EOF.
    Falls back to collecting all bullet lines in the whole document if no such
    heading is found.
    """
    lines = ticket_text.splitlines()
    in_ac_section = False
    criteria: list[str] = []
    found_section = False

    for line in lines:
        if re.match(r"^#{1,3}\s+", line):
            if re.search(r"acceptance.criteria|criteria|ac\b", line, re.IGNORECASE):
                in_ac_section = True
                found_section = True
                continue
            elif in_ac_section:
                break  # next heading - stop collecting
        if in_ac_section and AC_LINE_RE.match(line):
            criteria.append(line.strip())

    if not found_section:
        # Fallback: collect all bullet lines anywhere in the document
        criteria = [l.strip() for l in lines if AC_LINE_RE.match(l)]

    return criteria


class MechanicalVerdict:
    """Result of the no-AI pre-check step."""
    SIMPLE = "simple"      # skip AI, ticket is fine as-is
    COMPLEX = "complex"    # skip AI, ticket is obviously too large
    AMBIGUOUS = "ambiguous"  # proceed to AI step


def mechanical_complexity_check(
    ticket_text: str,
    threshold: int,
) -> tuple[str, str]:
    """
    Returns (verdict, explanation) without calling the AI.

    verdict is one of MechanicalVerdict.{SIMPLE,COMPLEX,AMBIGUOUS}.
    explanation is a human-readable string for the report.
    """
    criteria = extract_acceptance_criteria(ticket_text)
    n = len(criteria)

    conjunction_hits = SCOPE_CONJUNCTION_RE.findall(ticket_text)

    signals: list[str] = []
    if n >= threshold:
        signals.append(f"{n} acceptance criteria (threshold: {threshold})")
    if conjunction_hits:
        unique = list(dict.fromkeys(h.lower() for h in conjunction_hits))
        signals.append(f"scope conjunction(s) in ticket text: {', '.join(repr(h) for h in unique)}")

    if n < SIMPLE_THRESHOLD and not signals:
        return (
            MechanicalVerdict.SIMPLE,
            f"{n} acceptance criteria, no scope conjunction signals - "
            f"ticket appears focused enough for a single implementation pass.",
        )

    if n >= threshold:
        verdict = MechanicalVerdict.COMPLEX if n >= COMPLEX_THRESHOLD else MechanicalVerdict.AMBIGUOUS
        return (
            verdict,
            f"Mechanical signals detected: {'; '.join(signals)}. "
            + (
                "Ticket is unambiguously large - sending to AI for split proposal."
                if verdict == MechanicalVerdict.COMPLEX
                else "One or more complexity signals - sending to AI for review."
            ),
        )

    return (
        MechanicalVerdict.AMBIGUOUS,
        f"{n} acceptance criteria; scope conjunction(s) detected ({', '.join(repr(h) for h in conjunction_hits[:3])}). "
        "Sending to AI for review.",
    )


# ---------------------------------------------------------------------------
# AI complexity + split step
# ---------------------------------------------------------------------------

def split_file_path(ticket_id: str) -> Path:
    return Path(f".ticket-split-{ticket_id}.md")


def save_split(ticket_id: str, ticket_content: str, report: str) -> Path:
    path = split_file_path(ticket_id)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path.write_text(
        f"<!-- generated by split-ticket.py on {timestamp} for {ticket_id} -->\n\n"
        f"{SPLIT_FILE_TICKET_MARKER}\n\n{ticket_content}\n\n"
        f"{report}\n",
        encoding="utf-8",
    )
    return path


def build_split_prompt(ticket_content: str, mechanical_explanation: str, review_context: str = "") -> str:
    instructions = lib.load_prompt_body(SPLIT_PROMPT_FILE)
    review_section = (
        f"\n\nA prior review-ticket.py pass already checked this ticket's "
        f"claims against the codebase - its report is below. Use it to keep "
        f"your Reasoning and child-ticket descriptions grounded in what's "
        f"already confirmed to exist, rather than re-deriving it yourself, "
        f"and as a cohesion signal if it names specific existing code "
        f"multiple criteria share. This does not change Step 1's rule: it's "
        f"still cohesion-only, not a redundancy check - review-ticket.py "
        f"already removed any criterion it found fully satisfied before "
        f"this ticket reached you, so nothing here should be treated as a "
        f"reason to drop or reword a criterion.\n\n{review_context}"
        if review_context else ""
    )
    return (
        f"{instructions}\n\n---\n\n"
        f"The following mechanical pre-check has already been run on this "
        f"ticket and flagged it for your review:\n\n"
        f"> {mechanical_explanation}\n\n"
        f"Here is the ticket - already complete and current, no need to "
        f"read_file it again:\n\n{ticket_content}"
        f"{review_section}\n\n"
        f"Use read_file/list_dir/search_files to map each acceptance "
        f"criterion to the codebase areas it would modify - use this to "
        f"judge cohesion, not to assess whether the work is done. "
        f"Produce your response in the exact format from Step 5 above. "
        f"Your final response (no further tool calls) must be exactly "
        f"that report - no chat header, no preamble or trailing commentary."
    )


def run_split_step(ticket_content: str, mechanical_explanation: str, model: str, review_context: str = "") -> str:
    try:
        result = lib.run_ai_step_with_retry(
            lambda: ai_client.run_with_tools(
                build_split_prompt(ticket_content, mechanical_explanation, review_context),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(allow_write=False, preloaded_paths={TICKET_DEDUP_KEY}),
                "split-ticket",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "split-ticket",
        )
    except (ai_client.AIError, tools.PipelineAbort) as e:
        lib.die(str(e))
    if SPLIT_FILE_REPORT_MARKER not in result.text:
        lib.render_step_output(result.text, level=0)
        lib.die("Complexity reviewer did not produce a valid report (see output above).")
    return result.text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assess a Linear ticket for complexity and propose a child-ticket split if needed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("ticket_id", help="Linear ticket ID, e.g. NEB-42")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"opencode zen model ID to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--ticket-file-in",
        type=Path,
        default=None,
        help="Review this local file instead of fetching from Linear.",
    )
    parser.add_argument(
        "--force-ai",
        action="store_true",
        help="Skip the mechanical pre-check and always run the AI step.",
    )
    parser.add_argument(
        "--review-file-in",
        type=Path,
        default=None,
        help="Optional: a review-ticket.py report (e.g. .ticket-review-{ticket-id}.md) "
             "to ground the complexity assessment in what's already confirmed to exist "
             "in the codebase. Purely additional context - has no effect on which "
             "criteria get carried into which child ticket.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=COMPLEX_THRESHOLD,
        help=f"Acceptance-criteria count at which the ticket is considered complex "
             f"(default: {COMPLEX_THRESHOLD}). Has no effect if --force-ai is set.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info).",
    )
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)

    # 1. Fetch ticket
    if args.ticket_file_in is not None:
        if not args.ticket_file_in.exists():
            lib.die(f"{args.ticket_file_in} not found.")
        render.print_line(
            f"-- Reviewing local file {args.ticket_file_in} instead of fetching {args.ticket_id} from Linear."
        )
        ticket_content = args.ticket_file_in.read_text(encoding="utf-8")
    else:
        ticket_content = lib.fetch_ticket_text(args.ticket_id)

    review_context = ""
    if args.review_file_in is not None:
        if not args.review_file_in.exists():
            lib.die(f"{args.review_file_in} not found.")
        review_context = args.review_file_in.read_text(encoding="utf-8")

    # 2. Mechanical pre-check
    if args.force_ai:
        mechanical_verdict = MechanicalVerdict.AMBIGUOUS
        mechanical_explanation = "Mechanical pre-check skipped (--force-ai)."
    else:
        mechanical_verdict, mechanical_explanation = mechanical_complexity_check(
            ticket_content, args.threshold
        )

    render.print_line()
    render.print_line(f"-- Mechanical pre-check: {mechanical_explanation}")

    if mechanical_verdict == MechanicalVerdict.SIMPLE:
        # Short-circuit: no AI call needed, write a minimal report and exit.
        report = (
            f"{SPLIT_FILE_REPORT_MARKER}\n\n"
            f"### Verdict\nno-split\n\n"
            f"### Reasoning\n{mechanical_explanation}\n\n"
            f"### Proposed Child Tickets\nNone.\n"
        )
        saved_path = save_split(args.ticket_id, ticket_content, report)
        render.print_line()
        render.print_line(f"-- {args.ticket_id} appears focused - no split needed.")
        render.print_line(f"-- Saved to {saved_path}.")
        render.print_line(f"-- Token usage: {ai_client.usage}")
        return

    # 3. AI step
    render.print_line(f"-- Running AI complexity review for {args.ticket_id} ...")
    report = run_split_step(ticket_content, mechanical_explanation, args.model, review_context)
    saved_path = save_split(args.ticket_id, ticket_content, report)

    render.print_line()
    render.print_line(f"-- Complexity assessment for {args.ticket_id}:")
    render.print_line()
    render.print_line(report)
    render.print_line()
    render.print_line(
        f"-- Saved to {saved_path}.\n"
        f"-- If a split is proposed, create the child tickets in Linear manually\n"
        f"   (or extend create-child-tickets.py to do it), then close or re-scope\n"
        f"   the parent ticket."
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")


if __name__ == "__main__":
    main()
