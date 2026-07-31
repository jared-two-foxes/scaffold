#!/usr/bin/env python3
"""
reset-workflow - the git-native "abandon the current ticket" command.
Reverts the working tree to the ticket's base branch (main/master by
default), deletes the ticket/<id> branch, and clears every pipeline
state file (the same set reset-pipeline clears - .criteria-stack.json,
the scratch plan/ticket files, and the .pipeline-git-state.json
sidecar). The nuclear option: one command back to a clean base with no
in-progress ticket left behind.

The inverse of push-ticket's branch creation + next-step's per-criterion
commits: where push-ticket makes a ticket branch and next-step fills it
with one commit per criterion, this tears the whole thing down. Use it
when a ticket is being dropped entirely (wrong ticket, abandoned
approach, want to start over from base) - as opposed to reset-criterion
(roll back a *single* criterion but keep going) or reset-pipeline
(clear state files only, leave any git branch untouched).

Resolution of *which* ticket to abandon, in priority order:
  1. the top frame's ticket in .criteria-stack.json (the ticket currently
     in progress), if the stack is non-empty;
  2. the current branch, if it matches the configured branch_prefix
     (e.g. on `ticket/SA-1` -> SA-1);
  3. the single entry in .pipeline-git-state.json, if exactly one ticket
     has a recorded base_branch (common right after a finished ticket
     whose sidecar wasn't cleared - though a clean validate clears it, a
     crashed/abandoned one may leave it behind).

If none of those identifies a ticket, the git steps are skipped (nothing
to revert) and only the pipeline-state cleanup runs - making this a
superset of reset-pipeline when there's no ticket branch to tear down.

The base branch is the one push-ticket recorded for this ticket (in
.pipeline-git-state.json), falling back to the configured `base_branch`,
then to a detection guess only when unambiguous. Refuses rather than
guesses when it can't be determined and a branch switch is needed.

Safety: dry-run by default (same convention as reset-pipeline /
create-child-tickets / update-ticket) - prints exactly what would be
checked out, deleted, and removed, and exits without touching anything
until --yes is passed. Refuses on a dirty working tree (consistent with
push-ticket's guard) so uncommitted in-progress work isn't silently
carried onto the base branch - commit, stash, or `git restore .` first.
Branch deletion is on by default (the branch is being abandoned); pass
--keep-branch to leave it in place for inspection.

Requires `git_workflow = true` for the git steps; with it off this just
runs the pipeline-state cleanup (there's no ticket branch to revert).

Usage:
    reset-workflow [--yes] [--keep-stack] [--keep-branch] [--include-log]
                   [--config <path>] [--log-level <level>]
"""

import argparse
import sys
from pathlib import Path

from .lib import pipeline_lib as lib, render, verbosity
from . import reset_pipeline

log = verbosity.get_logger(__name__)


def _identify_ticket(cfg: "lib.GitConfig") -> str | None:
    """Resolve the ticket id to abandon - stack top, current branch, or
    the sole sidecar entry. None when nothing identifiable."""
    stack = lib.load_stack()
    if stack:
        return stack[0].ticket
    if lib.git_is_repo():
        branch = lib.git_current_branch()
        prefix = cfg.branch_prefix
        if prefix and branch.startswith(prefix):
            return branch[len(prefix):]
    state = lib.load_git_state()
    if len(state) == 1:
        return next(iter(state))
    return None


def _resolve_base_branch(cfg: "lib.GitConfig", ticket_id: str | None) -> str | None:
    """The branch to land back on. Sidecar (per-ticket, recorded by
    push-ticket) first, then config, then None (caller refuses)."""
    if ticket_id is not None:
        recorded = lib.lookup_git_base_branch(ticket_id)
        if recorded:
            return recorded
    if cfg.base_branch:
        return cfg.base_branch
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Abandon the current ticket: revert to its base branch, "
                     "delete the ticket branch, and clear all pipeline state.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually perform the reset. Without this, prints what would "
             "happen and exits without touching anything.",
    )
    parser.add_argument(
        "--keep-stack", action="store_true",
        help="Don't remove .criteria-stack.json - clear only the other "
             "pipeline state (and still revert/delete the git branch).",
    )
    parser.add_argument(
        "--keep-branch", action="store_true",
        help="Don't delete the ticket/<id> branch - leave it in place for "
             "inspection. The working tree still returns to the base branch.",
    )
    parser.add_argument(
        "--include-log", action="store_true",
        help="Also remove .pipeline-log.jsonl (excluded by default - it's a "
             "running diagnostic history across every ticket ever processed).",
    )
    parser.add_argument(
        "--config",
        default=str(lib.PIPELINE_CONFIG_FILE),
        help=f"Path to the pipeline config (default: {lib.PIPELINE_CONFIG_FILE}).",
    )
    parser.add_argument(
        "--log-level", default="info", choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info).",
    )
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)

    config_path = Path(args.config)
    cfg = lib.load_git_config(config_path)

    ticket_id = _identify_ticket(cfg) if cfg.git_workflow else None
    base_branch = _resolve_base_branch(cfg, ticket_id) if cfg.git_workflow else None
    ticket_branch = (
        lib.ticket_branch_name(cfg, ticket_id)
        if (cfg.git_workflow and ticket_id is not None) else None
    )

    # ── Plan: what would happen ──────────────────────────────────────────
    plan_lines: list[str] = []

    do_git = cfg.git_workflow and lib.git_is_repo()
    if cfg.git_workflow and not lib.git_is_repo():
        plan_lines.append("-- git_workflow is on but not a git repo - "
                           "skipping git steps, clearing pipeline state only.")

    if do_git:
        if ticket_branch is None or not lib.git_branch_exists(ticket_branch):
            plan_lines.append("-- No ticket branch to revert - clearing pipeline state only.")
            do_git = False
        elif base_branch is None:
            plan_lines.append(
                f"-- Could not determine a base branch for {ticket_branch} "
                f"(no sidecar entry, no base_branch in config). Refusing to "
                f"guess; clear .pipeline-git-state.json or set base_branch. "
                f"Pipeline state will still be cleared."
            )
            do_git = False
        else:
            current = lib.git_current_branch()
            if current == ticket_branch:
                plan_lines.append(f"-- checkout {base_branch} (leaving {ticket_branch})")
            elif current == base_branch:
                plan_lines.append(f"-- already on base {base_branch}")
            else:
                plan_lines.append(f"-- checkout {base_branch} (currently on {current})")
            if not args.keep_branch:
                plan_lines.append(f"-- delete branch {ticket_branch}")

    targets = reset_pipeline.find_targets(args.keep_stack, args.include_log)
    if targets:
        plan_lines.append(f"-- remove {len(targets)} pipeline state file(s):")
        for path in targets:
            plan_lines.append(f"     {path}")

    if not plan_lines:
        render.print_line("-- Nothing to reset. Already clean.")
        return

    render.print_line("-- reset-workflow plan:")
    for line in plan_lines:
        render.print_line(line)

    if not args.yes:
        render.print_line()
        render.print_line("-- Dry run - nothing was changed. Re-run with --yes to execute.")
        return

    # ── Execute ──────────────────────────────────────────────────────────
    if do_git:
        # Make sure pipeline state files are gitignored before the dirty
        # check, so an existing .criteria-stack.json (untracked, written by
        # the pipeline itself) doesn't read as "uncommitted user work" and
        # block the reset. push-ticket does the same; this makes reset-workflow
        # self-sufficient when run without a prior push in the same repo.
        lib.ensure_gitignore_entries()
        if lib.git_user_is_dirty():
            lib.die(
                "Clean working tree required - uncommitted changes would be "
                "carried onto the base branch. Commit, stash, or `git restore .` "
                "first, then re-run:\n" + lib.git_status_porcelain()
            )
        try:
            current = lib.git_current_branch()
            if current != base_branch:
                lib.git_checkout(base_branch)
            if not args.keep_branch and lib.git_branch_exists(ticket_branch):
                # -D (force): the branch is being abandoned, so it's
                # expected to be unmerged - -d would refuse exactly that.
                r = lib._git("branch", "-D", ticket_branch)
                if r.returncode != 0:
                    log.warning("-- git branch -D %s failed (non-fatal): %s",
                                ticket_branch, r.stderr.strip())
            if ticket_id is not None:
                lib.clear_git_base_branch(ticket_id)
        except lib.GitError as e:
            lib.die(f"git step failed: {e}")

    for path in targets:
        if path.exists():
            path.unlink()
            log.info("-- Removed %s", path)

    render.print_line()
    if do_git and base_branch:
        render.print_line(f"-- Reverted to {base_branch}.")
    render.print_line(f"-- Cleared {len(targets)} pipeline state file(s). Clean slate.")


if __name__ == "__main__":
    main()