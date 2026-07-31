#!/usr/bin/env python3
"""
reset-pipeline - clear .criteria-stack.json and every scratch/
intermediate file the criteria-stack pipeline leaves behind in the
current project root, so a fresh `push_ticket <id>` starts from a
genuinely clean slate.

Purely mechanical: no AI call, no Linear call - just deleting files that
already exist on disk. Same dry-run-by-default convention as
create-child-tickets.py/update-ticket.py (the other scripts here with a
real, hard-to-reverse effect): prints what would be removed and exits
without touching anything until --yes is passed.

What gets removed (default):
  - .criteria-stack.json     - the pipeline's sole cross-invocation
                                state (see pipeline_lib.py's module
                                docstring) - unless --keep-stack.
  - .declined-criteria.json  - append-only ledger of criteria rejected
                                by the grounding check. Removing it on
                                reset means previously declined
                                criteria will be re-checked on the next
                                push - desirable for a clean slate,
                                especially after a grounding-check bug
                                fix that wrongly declined criteria.
  - .ticket.md, .tdd-plan.md, .updated-plan.md, .gap-plan.md
                              - transient scratch push_ticket.py/
                                next_step.py regenerate fresh on every
                                run; never read back by a *later*,
                                separate invocation.
  - .ticket-review-*.md, .ticket-proposed-*.md
                              - review-ticket/propose-
                                ticket-edit's per-ticket
                                working files (their *default* output
                                paths only - a custom --ticket-file-out
                                elsewhere isn't matched by this glob and
                                is left alone, same as any other file
                                this pipeline doesn't own).
  - .ticket-split-*.md, .ticket-children-*.json
                              - split-ticket.py's report and create-
                                child-tickets.py's created-children
                                manifest.

What's NEVER removed, with or without --yes:
  - .dev-pipeline.toml       - project-local toolchain command
                                overrides; this is your configuration,
                                not pipeline output.
  - .pipeline-log.jsonl      - the diagnostic event log. Every other
                                scratch-cleanup call site in this
                                pipeline (push_ticket.py's own
                                remove_scratch_files calls) already
                                excludes this file for the same reason:
                                it's a running history across every
                                ticket ever processed, not per-ticket
                                scratch tied to whatever's currently in
                                progress. Pass --include-log to remove
                                it anyway.

Usage:
    reset-pipeline [--yes] [--keep-stack] [--include-log]
                   [--log-level <level>]
"""

import argparse
from pathlib import Path

from .lib import pipeline_lib as lib, render, verbosity

log = verbosity.get_logger(__name__)

FIXED_SCRATCH_FILES = (
    lib.TICKET_FILE,
    lib.PLAN_FILE,
    lib.UPDATED_PLAN_FILE,
    lib.GAP_PLAN_FILE,
    lib.GIT_STATE_FILE,
    lib.DECLINED_CRITERIA_FILE,
)

# Per-ticket working files with a predictable *default* name - each
# script's own --ticket-file-out/--split-file-in flag can point
# elsewhere, and a file at a custom path is left alone here, same as any
# file this pipeline doesn't itself own.
SCRATCH_GLOBS = (
    ".ticket-review-*.md",
    ".ticket-proposed-*.md",
    ".ticket-explored-*.md",
    ".ticket-split-*.md",
    ".ticket-children-*.json",
)


def find_targets(keep_stack: bool, include_log: bool) -> list[Path]:
    """
    Every existing path this run would remove, in a stable, readable
    order (fixed files first, then each glob's matches sorted by name) -
    never includes PIPELINE_CONFIG_FILE, since nothing in this function
    ever considers it a candidate in the first place.
    """
    targets = [p for p in FIXED_SCRATCH_FILES if p.exists()]
    if include_log and lib.PIPELINE_LOG_FILE.exists():
        targets.append(lib.PIPELINE_LOG_FILE)
    if not keep_stack and lib.CRITERIA_STACK_FILE.exists():
        targets.append(lib.CRITERIA_STACK_FILE)
    for pattern in SCRATCH_GLOBS:
        targets.extend(sorted(Path(".").glob(pattern)))
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear .criteria-stack.json and every scratch/intermediate "
                     "file the criteria-stack pipeline leaves behind, for a "
                     "genuinely clean slate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually delete the files. Without this, prints what would be "
             "removed and exits without deleting anything.",
    )
    parser.add_argument(
        "--keep-stack", action="store_true",
        help="Don't remove .criteria-stack.json - clear only the scratch/"
             "intermediate files around it, leaving any in-progress work on "
             "the stack untouched.",
    )
    parser.add_argument(
        "--include-log", action="store_true",
        help="Also remove .pipeline-log.jsonl (excluded by default - it's a "
             "running diagnostic history across every ticket ever processed, "
             "not scratch tied to whatever's currently in progress).",
    )
    parser.add_argument(
        "--log-level", default="info", choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info).",
    )
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)

    targets = find_targets(args.keep_stack, args.include_log)

    if not targets:
        render.print_line("-- Nothing to clean up. Already a clean slate.")
        return

    render.print_line(f"-- {len(targets)} file(s) to remove:")
    for path in targets:
        render.print_line(f"   {path}")

    if not args.yes:
        render.print_line()
        render.print_line(
            f"-- Dry run - nothing was deleted. Re-run with --yes to remove "
            f"these {len(targets)} file(s)."
        )
        return

    for path in targets:
        path.unlink()
        log.info("-- Removed %s", path)

    render.print_line()
    render.print_line(f"-- Removed {len(targets)} file(s). Clean slate.")


if __name__ == "__main__":
    main()
