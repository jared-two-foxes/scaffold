#!/usr/bin/env python3
"""
create-child-tickets - turn split-ticket.py's proposed child tickets into
real Linear sub-issues of the parent, and record them locally so each
can be pushed onto the criteria stack in order via push_ticket --prepend.

Purely mechanical: split-ticket.py's own prompt enforces a fully
structured output (title/description/acceptance-criteria per child), so
parsing it and calling Linear's issueCreate mutation needs no AI call at
all. Deliberately standalone, same as split-ticket.py itself - reads
.ticket-split-{ticket-id}.md (or --split-file-in), never re-derives a
split proposal itself.

This is the other write path against Linear in this set of scripts,
alongside update-ticket.py, and follows the same convention: defaults to
a dry run (prints what would be created), nothing is sent to Linear
until --yes is passed. Sub-issues are linked to the split (parent)
ticket via Linear's parentId, not created as standalone tickets - the
parent keeps its original identity as the tracking issue; the split
"Depends on" ordering (if any) is recorded in each child's own
description for a human to see, not as a structured Linear relation.

Dies if the split report's verdict is "no-split" - nothing to create.

Writes .ticket-children-{ticket-id}.json: an ordered list of
{"id": <identifier>, "title": <title>} for each created child, in the
same order split-ticket.py proposed them - so each child can be pushed
via push_ticket --prepend without re-parsing the split report itself.

Usage:
    create-child-tickets <ticket-id> [--split-file-in <path>] [--yes]
                          [--log-level <level>]
"""

import argparse
import json
import re
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .lib import fetch_ticket as ticket_source, pipeline_lib as lib, render, verbosity

log = verbosity.get_logger(__name__)

VERDICT_RE = re.compile(r"^###\s*Verdict\s*\n+(\S+)", re.MULTILINE)
CHILD_HEADER_RE = re.compile(r"^####\s*Child\s+\d+:\s*(.+?)\s*$", re.MULTILINE)
DESCRIPTION_RE = re.compile(r"\*\*Description:\*\*\s*(.+?)(?:\n\*\*|\Z)", re.DOTALL)
CRITERIA_RE = re.compile(r"\*\*Acceptance Criteria:\*\*\s*\n((?:^\s*-.*(?:\n|\Z))+)", re.MULTILINE)
DEPENDS_RE = re.compile(r"\*\*Depends on:\*\*\s*(.+?)\s*$", re.MULTILINE)


def split_file_path(ticket_id: str) -> Path:
    return Path(f".ticket-split-{ticket_id}.md")


def children_file_path(ticket_id: str) -> Path:
    return Path(f".ticket-children-{ticket_id}.json")


@dataclass
class ChildTicket:
    title: str
    description: str
    criteria: list[str]
    depends_on: str | None


def extract_proposed_children_section(report_text: str) -> str:
    match = re.search(r"###\s*Proposed Child Tickets\s*\n(.*)", report_text, re.DOTALL)
    return match.group(1) if match else ""


def parse_child_tickets(report_text: str) -> list[ChildTicket]:
    """
    Splits the report's '### Proposed Child Tickets' section on each
    '#### Child N: <title>' boundary and pulls Description/Acceptance
    Criteria/Depends-on out of the block between one boundary and the
    next - matches split-ticket.prompt.md's Step 5 output format
    exactly, since that's the only thing that ever produces this file.
    """
    section = extract_proposed_children_section(report_text)
    headers = list(CHILD_HEADER_RE.finditer(section))
    children = []
    for i, header_match in enumerate(headers):
        title = header_match.group(1).strip()
        start = header_match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(section)
        block = section[start:end]

        desc_match = DESCRIPTION_RE.search(block)
        description = desc_match.group(1).strip() if desc_match else ""

        criteria: list[str] = []
        criteria_match = CRITERIA_RE.search(block)
        if criteria_match:
            for line in criteria_match.group(1).splitlines():
                line = line.strip()
                if line.startswith("-"):
                    criteria.append(line[1:].strip())

        depends_match = DEPENDS_RE.search(block)
        depends_on = depends_match.group(1).strip() if depends_match else None

        children.append(ChildTicket(title, description, criteria, depends_on))
    return children


CHECKBOX_PREFIX_RE = re.compile(r"^\[[ xX]\]\s*")


def build_child_body(child: ChildTicket) -> str:
    """
    Criteria carried over from the parent already have their own "[ ]"
    checkbox marker most of the time - split-ticket.py copies parent
    criteria verbatim, and this codebase's tickets are conventionally
    written that way. Only add one if it's actually missing, or every
    child ticket ends up with a doubled "- [ ] [ ] ..." bullet.
    """
    lines = [child.description, "", "## Acceptance Criteria"]
    for criterion in child.criteria:
        checkboxed = criterion if CHECKBOX_PREFIX_RE.match(criterion) else f"[ ] {criterion}"
        lines.append(f"- {checkboxed}")
    if child.depends_on:
        lines += ["", f"**Depends on:** {child.depends_on}"]
    return "\n".join(lines) + "\n"


@dataclass
class ChildCreationResult:
    created: list[dict]  # ordered [{"id": identifier, "title": title}, ...]
    failure: str | None  # None if every child was created successfully


def create_children(ticket_id: str, children: list[ChildTicket]) -> ChildCreationResult:
    """
    Creates each proposed child as a real Linear sub-issue of ticket_id,
    in order, stopping at the first failure. Does not write the manifest
    file or print a summary - callers own both (this script's own main()
    below, and push_ticket.py, want different surrounding messaging
    around the same core creation loop).
    """
    render.print_line(f"-- Fetching {ticket_id} to resolve its internal id/team ...")
    try:
        parent_data = ticket_source.fetch_ticket(ticket_id)
    except urllib.error.HTTPError as e:
        lib.die(f"Fetching {ticket_id} failed: HTTP {e.code}: {e.read().decode()}")
    if "errors" in parent_data:
        lib.die(f"Ticket fetch failed: {parent_data['errors']}")
    parent_issue = parent_data.get("data", {}).get("issue")
    if not parent_issue:
        lib.die(f"{ticket_id} not found in Linear.")
    parent_internal_id = parent_issue["id"]
    team_id = parent_issue["team"]["id"]

    created: list[dict] = []
    failure: str | None = None
    for i, child in enumerate(children, 1):
        render.print_line(f"-- Creating child {i}/{len(children)}: {child.title} ...")
        try:
            result = ticket_source.create_ticket(
                team_id, child.title, build_child_body(child), parent_id=parent_internal_id,
            )
        except urllib.error.HTTPError as e:
            failure = f"HTTP {e.code}: {e.read().decode()}"
            break
        if "errors" in result:
            failure = str(result["errors"])
            break
        if not result.get("data", {}).get("issueCreate", {}).get("success"):
            failure = f"Linear did not report success: {result}"
            break
        issue = result["data"]["issueCreate"]["issue"]
        render.print_line(f"   -> {issue['identifier']}: {issue['url']}")
        created.append({"id": issue["identifier"], "title": child.title})

    return ChildCreationResult(created=created, failure=failure)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Linear sub-issues from split-ticket.py's proposed child tickets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("ticket_id", help="The split (parent) ticket's Linear ID, e.g. NEB-42")
    parser.add_argument(
        "--split-file-in", type=Path, default=None,
        help="split-ticket.py report to read (default: .ticket-split-{ticket-id}.md).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually create the child tickets in Linear. Without this, prints "
             "what would be created and exits without calling Linear at all.",
    )
    parser.add_argument(
        "--log-level", default="info", choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info).",
    )
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)

    split_file = args.split_file_in or split_file_path(args.ticket_id)
    if not split_file.exists():
        lib.die(f"{split_file} not found. Run 'split-ticket {args.ticket_id}' first.")
    report_text = split_file.read_text(encoding="utf-8")

    verdict_match = VERDICT_RE.search(report_text)
    if not verdict_match:
        lib.die(f"Could not find a '### Verdict' line in {split_file}.")
    verdict = verdict_match.group(1).strip().lower()
    if verdict == "no-split":
        render.print_line(f"-- {split_file} verdict is 'no-split' - nothing to create.")
        return

    children = parse_child_tickets(report_text)
    if not children:
        lib.die(f"Verdict is '{verdict}' but no '#### Child N:' blocks were found in {split_file}.")

    render.print_line(f"-- {len(children)} proposed child ticket(s) for {args.ticket_id} (verdict: {verdict}):")
    for i, child in enumerate(children, 1):
        depends_note = f" - depends on: {child.depends_on}" if child.depends_on else ""
        render.print_line(f"   {i}. {child.title} ({len(child.criteria)} criteria){depends_note}")

    if not args.yes:
        render.print_line()
        render.print_line(
            f"-- Dry run - nothing was sent to Linear. Re-run with --yes to create these "
            f"{len(children)} ticket(s) as sub-issues of {args.ticket_id}."
        )
        return

    result = create_children(args.ticket_id, children)

    manifest_path = children_file_path(args.ticket_id)
    manifest_path.write_text(json.dumps(result.created, indent=2) + "\n", encoding="utf-8")

    if result.failure is not None:
        render.print_line(f"\n-- Saved {len(result.created)}/{len(children)} successfully-created child(ren) to {manifest_path}.")
        lib.die(
            f"Creating '{children[len(result.created)].title}' failed: {result.failure}. "
            f"The {len(result.created)} child(ren) already created above were not rolled back - "
            f"fix the issue, then either re-run with a --split-file-in trimmed to the "
            f"remaining children, or clean up manually in Linear before retrying."
        )

    render.print_line(f"\n-- Created {len(result.created)} child ticket(s), saved to {manifest_path}.")


if __name__ == "__main__":
    main()
