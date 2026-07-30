#!/usr/bin/env python3
"""
push_ticket - fetch a Linear ticket, run plan+narrow, and seed
.criteria-stack.json with one frame per remaining acceptance criterion.
The starting gesture of the criteria-stack pipeline: run this once per
ticket, then run `next_step` repeatedly to advance it.

The criteria stack processes criteria one at a time regardless of how
many are on a ticket — each criterion gets its own frame, its own test,
its own implementation step. Large tickets are handled by the stack
itself, not by splitting them into separate Linear sub-issues. If a
human decides a ticket is genuinely too large and wants to break it up
in Linear, `split-ticket` and `create-child-tickets` remain as
standalone commands for that purpose — but push_ticket no longer runs
the split check automatically.

Guard-first, not cleanup-first: the re-entrancy check (stack already has
this ticket) and the clobber check (stack has a *different* ticket, and
neither --force nor --prepend was passed) both run before any file on
disk is touched. Only once the guard has passed does this script clear
its own scratch state (.ticket.md/.tdd-plan.md/.gap-plan.md) and write
the new stack.

Pushing a *different* ticket while one is already in progress has two
distinct resolutions, not one - --force and --prepend are mutually
exclusive and mean very different things:

  --force    Abandon the in-progress stack. Replaces it entirely with
             the new ticket's frames. Use this when the in-progress
             ticket was pushed by mistake, or you're deliberately
             dropping it.

  --prepend  Insert the new ticket's frames *ahead* of the in-progress
             stack, as a prerequisite. The in-progress stack is kept
             intact underneath, not discarded. Once the new ticket's
             own frames are all popped, TICKET_VALIDATE runs for it
             and, once approved, the original ticket's criteria resume
             automatically on the next `next_step` call. Use this when
             working the current ticket's top criterion reveals that
             some other, not-yet-built piece needs to exist first.

.criteria-stack.json is the only file this pipeline trusts across
invocations. .ticket.md/.tdd-plan.md/.gap-plan.md are transient scratch,
regenerated here and again by next_step's TICKET_VALIDATE phase - not
read back by any later, separate run. Safe to clear on every push
because each frame carries its own plan_context already extracted at
push time.

--validate-only skips fetch/plan/narrow and criteria-building entirely -
it just pushes a "validating" sentinel frame so the next `next_step`
call runs the full validation gate directly. Use this to trigger a
validation pass on demand.

--from-gap-plan reuses an existing .gap-plan.md instead of re-running
plan+narrow. Use this after an earlier run already produced the gap
plan and you just want to seed the stack from it.

--explore runs an interactive per-criterion context-scaffolding session
after plan+narrow (or after --from-gap-plan), before the stack is written.
For each criterion that survived the gap check, an interactive agent
explores the codebase and asks you targeted questions about things the
code can't answer (which approach to follow, which patterns apply, which
integration points matter), then appends its findings directly to that
frame's plan_context. Every downstream step (test-writer, recheck) sees
the enriched context automatically. Without --explore the stack is written
immediately after plan+narrow, same as today.

Usage:
    push_ticket <ticket-id> [--model <model-id>] [--ticket-file-in <path>]
                [--from-gap-plan | --validate-only] [--force | --prepend]
                [--explore] [--log-level <level>]
"""

import argparse
import sys
from pathlib import Path

from .lib import ai_client, pipeline_lib as lib, render, verbosity

log = verbosity.get_logger(__name__)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"


def prepare_git_branch(ticket_id: str, cfg: "lib.GitConfig", force: bool) -> None:
    """
    Layer 1 of the git-native workflow: make sure pipeline state files
    are gitignored, refuse on a dirty working tree, then create (or
    resume) the ticket/<id> branch from current HEAD and record the base
    branch for later merge/PR. No-op when cfg.git_workflow is off.

    Idempotent across re-runs: if the branch already exists (a prior
    push for this ticket crashed after creating it, or the stack was
    reset while the branch survived), it's checked out rather than
    recreated - so a fresh push-ticket for the same ticket lands back
    on its branch instead of erroring. A *different* ticket's branch
    being current is left alone here; the existing-stack guard above
    already decided whether this push is allowed at all.
    """
    if not cfg.git_workflow:
        return
    if not lib.git_is_repo():
        lib.die(
            "git_workflow = true in .dev-pipeline.toml but the current "
            "directory is not a git repository. Disable git_workflow or run "
            "from inside the repo."
        )
    lib.ensure_gitignore_entries()
    if lib.git_user_is_dirty():
        lib.die(
            "Clean working tree required for git_workflow. Commit or stash "
            f"your changes first:\n{lib.git_status_porcelain()}"
        )
    branch = lib.ticket_branch_name(cfg, ticket_id)
    base_branch = cfg.base_branch or lib.git_current_branch()
    if lib.git_branch_exists(branch):
        if not force and lib.git_current_branch() != branch:
            log.info("-- git_workflow: ticket branch %s already exists; checking it out.", branch)
        lib.git_checkout(branch)
    else:
        lib.git_create_branch(branch)
        render.print_line(f"-- git_workflow: created branch {branch} from {base_branch}.")
    lib.record_git_base_branch(ticket_id, base_branch)


def print_declined_criteria(newly_declined: list[tuple["lib.CriterionFrame", list[str]]]) -> None:
    """
    Same shape as next_step.py's helper of the same name - prints one
    loud block per criterion a mechanical grounding check just rejected
    (lib.filter_grounded_frames), which has already recorded each one to
    lib.DECLINED_CRITERIA_FILE as a side effect. Duplicated rather than
    imported: push_ticket.py and next_step.py are separate CLI entry
    points that each own their own main()/local helpers, sharing only
    through pipeline_lib, same as every other small piece of console
    formatting in this pipeline.
    """
    if not newly_declined:
        return
    render.print_line()
    noun = "criterion" if len(newly_declined) == 1 else "criteria"
    render.print_line(f"-- {len(newly_declined)} {noun} failed mechanical grounding - NOT pushed:")
    for frame, reasons in newly_declined:
        render.print_line(f"   {frame.criterion}")
        for reason in reasons:
            render.print_line(f"     - {reason}")
    render.print_line(
        f"-- Not resolved automatically. Fix the ticket wording, or if this is a false "
        f"positive, review and clear the entry from {lib.DECLINED_CRITERIA_FILE}."
    )


def resolve_ticket_frames(
    ticket_id: str,
    model: str,
    step_models: dict[str, str],
    ticket_file_in: Path | None,
) -> list[lib.CriterionFrame]:
    """
    Fetches the ticket, runs plan+narrow, and builds one CriterionFrame
    per remaining acceptance criterion. Returns an empty list if all
    criteria are already satisfied.
    """
    if ticket_file_in is not None:
        if not ticket_file_in.is_file():
            lib.die(f"--ticket-file-in {ticket_file_in} not found.")
        ticket_content = ticket_file_in.read_text(encoding="utf-8")
    else:
        ticket_content = lib.fetch_ticket_text(ticket_id)

    lib.remove_scratch_files((lib.TICKET_FILE, lib.PLAN_FILE, lib.GAP_PLAN_FILE))
    lib.TICKET_FILE.write_text(ticket_content, encoding="utf-8")
    lib.walk(lib.build_planning_blocks(ticket_id, model, step_models=step_models, ticket_file_in=lib.TICKET_FILE))
    gap_plan_content = lib.GAP_PLAN_FILE.read_text(encoding="utf-8")

    criteria = lib.extract_acceptance_criteria(gap_plan_content)
    if not criteria:
        render.print_line(f"-- {ticket_id}: no gap found. All acceptance criteria already satisfied.")
        return []

    candidate_frames = [
        lib.CriterionFrame(
            ticket=ticket_id,
            criterion=criterion,
            plan_context=lib.extract_plan_context_for_criterion(criterion, gap_plan_content),
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification=lib.extract_verification_mode(criterion),
            existing_test_refs=lib.extract_existing_test_refs(criterion),
        )
        for criterion in criteria
    ]
    frames, newly_declined, skipped_count = lib.filter_grounded_frames(candidate_frames)
    print_declined_criteria(newly_declined)
    if skipped_count:
        render.print_line(
            f"-- {ticket_id}: skipped {skipped_count} criteria already in "
            f"{lib.DECLINED_CRITERIA_FILE} (previously declined)."
        )
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a ticket, plan+narrow it, and seed the criteria "
                     "stack with one frame per remaining acceptance criterion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("ticket_id", help="Linear ticket ID, e.g. NEB-42")
    parser.add_argument(
        "--model",
        default=None,
        help=f"opencode zen model ID to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--ticket-file-in",
        type=Path,
        default=None,
        help="Read the ticket from this local file instead of fetching from "
             "Linear - e.g. a not-yet-pushed revision from propose-ticket-edit.py. "
             "Ignored with --from-gap-plan (no fetch happens either way).",
    )
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument(
        "--from-gap-plan",
        action="store_true",
        help="Read criteria from the existing .gap-plan.md instead of "
             "fetching the ticket and re-running plan+narrow. The ticket ID "
             "is still required (for the frame tag). Errors if .gap-plan.md "
             "does not exist or isn't a valid gap plan.",
    )
    seed_group.add_argument(
        "--validate-only",
        action="store_true",
        help="Skip fetch/plan/narrow and criteria-building entirely - just "
             "push a 'validating' sentinel for this ticket, so the next "
             "'next_step' call runs the full ticket-validation gate directly. "
             "Use this to trigger a validation pass on demand.",
    )
    guard_group = parser.add_mutually_exclusive_group()
    guard_group.add_argument(
        "--force",
        action="store_true",
        help="Abandon and replace an in-progress stack for a different "
             "ticket. Without --force or --prepend, pushing a different "
             "ticket while one is already in progress prints a warning "
             "and exits non-zero.",
    )
    guard_group.add_argument(
        "--prepend",
        action="store_true",
        help="Insert this ticket's frames ahead of an in-progress stack "
             "for a different ticket, as a prerequisite - the in-progress "
             "stack is kept, not discarded, and resumes automatically once "
             "this ticket's own frames are popped and validated. Use this "
             "when the current top criterion turns out to depend on some "
             "other system that doesn't exist yet.",
    )
    parser.add_argument(
        "--explore",
        action="store_true",
        help="After plan+narrow (or --from-gap-plan), run an interactive "
             "per-criterion context-scaffolding session before writing the "
             "stack. For each criterion the agent explores the codebase and "
             "asks targeted questions about implementation approach, patterns, "
             "and integration points, appending its findings to that frame's "
             "plan_context so every downstream step sees the enriched context. "
             "Incompatible with --validate-only (no criteria are built in that "
             "mode). Makes push_ticket interactive - omit for scripted/headless "
             "use.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info). 'debug' shows per-tool-call "
             "activity and command output even on success; 'trace' adds raw "
             "request/response payloads; 'warning'/'error'/'critical' show "
             "progressively less.",
    )
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)
    if args.explore and args.validate_only:
        lib.die("--explore and --validate-only are incompatible: --validate-only pushes no criteria frames.")
    model, step_models = lib.resolve_step_models(lib.PIPELINE_CONFIG_FILE, args.model)
    git_cfg = lib.load_git_config(lib.PIPELINE_CONFIG_FILE)
    ticket_id = args.ticket_id

    # ── Guard: runs before any file is touched ──────────────────────────
    existing = lib.load_stack()
    if existing:
        if existing[0].ticket == ticket_id:
            render.print_line(
                f"-- Stack already has {len(existing)} frame(s) for {ticket_id}. "
                f"Run 'next_step' to continue."
            )
            return
        if args.prepend:
            if any(f.ticket == ticket_id for f in existing):
                lib.die(
                    f"{ticket_id} already has frame(s) elsewhere in the stack "
                    f"(not at the top) - --prepend would split them apart and "
                    f"break ticket-boundary detection. Run 'next_step' until "
                    f"they're worked through, or use --force to discard the "
                    f"whole stack and start over."
                )
            log.info(
                "-- --prepend: inserting %s ahead of %d frame(s) in progress for %s.",
                ticket_id, len(existing), existing[0].ticket,
            )
        elif not args.force:
            render.print_line(
                f"-- Stack has {len(existing)} frame(s) in progress for "
                f"{existing[0].ticket}, not {ticket_id}. Pass --force to "
                f"overwrite it (discards the in-progress stack; already-"
                f"written test/implementation files are untouched) or "
                f"--prepend to insert {ticket_id} ahead of it as a "
                f"prerequisite (keeps the in-progress stack intact)."
            )
            sys.exit(1)
        else:
            log.info(
                "-- --force: overwriting in-progress stack for %s with %s.",
                existing[0].ticket, ticket_id,
            )

    # ── git-native workflow: branch + dirty-tree guard (Layer 1) ────────
    prepare_git_branch(ticket_id, git_cfg, args.force)

    if args.validate_only:
        lib.remove_scratch_files((lib.TICKET_FILE, lib.PLAN_FILE, lib.GAP_PLAN_FILE))
        lib.ensure_validating_sentinel(ticket_id)
        render.print_line()
        render.print_line(f"-- Pushed a validation-pending marker for {ticket_id}.")
        render.print_line("-- Run 'next_step' to run the full ticket validation gate now.")
        return

    if args.from_gap_plan:
        lib.remove_scratch_files((lib.TICKET_FILE, lib.PLAN_FILE))
        if not lib.GAP_PLAN_FILE.is_file():
            lib.die(f"--from-gap-plan given but {lib.GAP_PLAN_FILE} does not exist.")
        gap_plan_content = lib.GAP_PLAN_FILE.read_text(encoding="utf-8")
        if "## Acceptance Criteria" not in gap_plan_content:
            lib.die(f"--from-gap-plan given but {lib.GAP_PLAN_FILE} is not a valid gap plan (see output above).")
        render.print_line(f"-- Using existing {lib.GAP_PLAN_FILE} (--from-gap-plan). No fetch, no plan+narrow.")

        criteria = lib.extract_acceptance_criteria(gap_plan_content)
        if not criteria:
            render.print_line()
            render.print_line("-- No gap found. All acceptance criteria already satisfied. Nothing pushed.")
            render.print_line(f"-- Token usage: {ai_client.usage}")
            return
        candidate_frames = [
            lib.CriterionFrame(
                ticket=ticket_id,
                criterion=criterion,
                plan_context=lib.extract_plan_context_for_criterion(criterion, gap_plan_content),
                test_files=None,
                test_names=None,
                status="pending",
                origin="ticket",
                verification=lib.extract_verification_mode(criterion),
                existing_test_refs=lib.extract_existing_test_refs(criterion),
            )
            for criterion in criteria
        ]
        frames, newly_declined, skipped_count = lib.filter_grounded_frames(candidate_frames)
        print_declined_criteria(newly_declined)
        if skipped_count:
            render.print_line(
                f"-- Skipped {skipped_count} criteria already in {lib.DECLINED_CRITERIA_FILE} "
                f"(previously declined)."
            )
        if not frames:
            render.print_line(
                f"-- 0 of {len(criteria)} pushed - all were previously declined or failed "
                f"mechanical grounding this run. See {lib.DECLINED_CRITERIA_FILE}."
            )
            render.print_line(f"-- Token usage: {ai_client.usage}")
            return
    else:
        frames = resolve_ticket_frames(
            ticket_id, model, step_models, args.ticket_file_in,
        )
        if not frames:
            render.print_line("-- Nothing pushed.")
            render.print_line(f"-- Token usage: {ai_client.usage}")
            return

    # ── Optional interactive context scaffolding (--explore) ────────────
    if args.explore and frames:
        render.print_line()
        render.print_line(
            "-- --explore: starting per-criterion context scaffolding. "
            "The agent will explore the codebase and may ask you targeted "
            "questions below. Answer each at the '> ' prompt; press Enter "
            "with no answer to let it proceed on its own judgement."
        )
        lib.run_explore_for_frames(frames, model)

    # ── Combined write ──────────────────────────────────────────────────
    if args.prepend and existing:
        lib.push_frames(frames)
        render.print_line()
        render.print_line(
            f"-- Prepended {len(frames)} frame(s) for {ticket_id} ahead of "
            f"{len(existing)} frame(s) still in progress for {existing[0].ticket}."
        )
        for frame in frames:
            render.print_line(f"   {frame.criterion}")
        render.print_line(
            f"-- Run 'next_step' to work through {ticket_id} first; "
            f"{existing[0].ticket} resumes automatically once it's validated."
        )
    else:
        lib.save_stack(frames)
        render.print_line()
        render.print_line(f"-- Pushed {len(frames)} frame(s) for {ticket_id} onto the stack.")
        for frame in frames:
            render.print_line(f"   {frame.criterion}")
        render.print_line("-- Run 'next_step' to begin.")
    render.print_line(f"-- Token usage: {ai_client.usage}")


if __name__ == "__main__":
    main()