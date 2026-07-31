#!/usr/bin/env python3
"""
update-ticket - Push a locally revised ticket file (--ticket-file-in,
default .ticket.md - e.g. propose-ticket-edit.py's --ticket-file-out)
back to the live Linear ticket it came from.

Deliberately standalone and separate from every other script in this
set: this is the one place that performs a real Linear API mutation -
visible to everyone on the ticket, not locally reversible the way an
edit to a file on disk is. It is never called implicitly by
review-ticket.py, propose-ticket-edit.py, check-ticket.py, or
resolve-ticket.py.

Defaults to a dry run: fetches the live ticket, diffs it against the
local file, and prints exactly what would change without calling the
mutation. Pass --yes to actually apply it.

Parses the local file in the same '# ID — Title' / '## Description'
format fetch_ticket.py's render() produces (and propose-ticket-edit.py
preserves verbatim except where a flagged concern changed it) - title
comes from the first line, description from everything after the
'## Description' heading. The metadata table (state/priority/assignee/
etc.) is intentionally ignored: those are Linear-managed fields that
were never meant to be edited locally, only displayed.

Usage:
    update-ticket <ticket-id> [--ticket-file-in .ticket.md] [--yes]
"""

import argparse
import difflib
import re
from pathlib import Path

from .lib import fetch_ticket as ticket_source, pipeline_lib as lib, render, verbosity

log = verbosity.get_logger(__name__)

TITLE_RE = re.compile(r"^#\s*\S+\s*[—-]\s*(.+?)\s*$")
DESCRIPTION_RE = re.compile(r"^## Description\s*\n+(.*)\Z", re.DOTALL | re.MULTILINE)


def parse_local_ticket(text: str) -> tuple[str | None, str | None]:
    lines = text.splitlines()
    title = None
    if lines:
        match = TITLE_RE.match(lines[0])
        if match:
            title = match.group(1).strip()
    desc_match = DESCRIPTION_RE.search(text)
    description = desc_match.group(1).strip() if desc_match else None
    return title, description


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push a locally revised ticket file back to the live Linear ticket.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("ticket_id", help="Linear ticket ID, e.g. NEB-42")
    parser.add_argument(
        "--ticket-file-in", type=Path, default=lib.TICKET_FILE,
        help=f"Local ticket file to push (default: {lib.TICKET_FILE}).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually apply the update. Without this, only prints a dry-run diff.",
    )
    parser.add_argument(
        "--log-level", default="info", choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info).",
    )
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)

    if not args.ticket_file_in.exists():
        lib.die(f"{args.ticket_file_in} not found - nothing to push. Run propose-ticket-edit.py first.")
    local_text = args.ticket_file_in.read_text(encoding="utf-8")
    new_title, new_description = parse_local_ticket(local_text)
    if new_title is None and new_description is None:
        lib.die(
            f"Could not parse a title or description out of {args.ticket_file_in} - "
            f"expected the '# ID — Title' / '## Description' format fetch_ticket.py renders."
        )

    render.print_line(f"-- Fetching current {args.ticket_id} from Linear to compare ...")
    data = ticket_source.fetch_ticket(args.ticket_id)
    if "errors" in data:
        lib.die(f"Ticket fetch failed: {data['errors']}")
    issue = data["data"]["issue"]
    if not issue:
        lib.die(f"Ticket {args.ticket_id} not found.")

    live_description = (issue.get("description") or "").strip()
    title_changed = bool(new_title) and new_title != issue["title"]
    description_changed = bool(new_description) and new_description != live_description

    render.print_line()
    render.print_line(f"-- Proposed update to {args.ticket_id} ({issue['url']}):")
    if title_changed:
        render.print_line(f"   title: {issue['title']!r} -> {new_title!r}")
    else:
        render.print_line("   title: unchanged")
    if description_changed:
        render.print_line("   description: changed -")
        diff = difflib.unified_diff(
            (live_description + "\n").splitlines(keepends=True),
            (new_description + "\n").splitlines(keepends=True),
            fromfile="live Linear description",
            tofile=f"local {args.ticket_file_in}",
        )
        render.print_line("".join(diff))
    else:
        render.print_line("   description: unchanged")

    if not title_changed and not description_changed:
        render.print_line()
        render.print_line("-- No differences from the live ticket - nothing to push.")
        return

    if not args.yes:
        render.print_line()
        render.print_line("-- Dry run only - nothing was sent to Linear. Re-run with --yes to apply.")
        return

    render.print_line()
    render.print_line(f"-- Applying update to {args.ticket_id} ...")
    result = ticket_source.update_ticket(
        issue["id"],
        title=new_title if title_changed else None,
        description=new_description if description_changed else None,
    )
    if "errors" in result:
        lib.die(f"Update failed: {result['errors']}")
    if not result.get("data", {}).get("issueUpdate", {}).get("success"):
        lib.die(f"Update did not report success: {result}")
    render.print_line(f"-- {args.ticket_id} updated successfully.")


if __name__ == "__main__":
    main()
