#!/usr/bin/env python3
"""Cross-strategy step orchestrator for the criteria stack pipeline.

Run `push_ticket <id>` once to seed the stack, then run `next_step`
repeatedly until the stack is empty. This module owns only shared
pipeline orchestration and validation gates; strategy modules own each
criterion lifecycle.

Dispatch order per call:
    - empty stack -> done
    - status == "validating" -> resume ticket validation
    - status == "feedback-ready" -> feedback retry path
    - status == "done" -> pop frame (and maybe validate ticket)
    - --manual-test/--skip-test -> strategy capability override
    - otherwise -> strategy.advance(stack, frame, ctx)

Per-strategy phase machines live in:
    - strategies/tdd.py
    - strategies/direct.py
    - strategies/manual.py
    - strategies/refactor.py

Shared cross-strategy responsibilities in this file:
    - step(): single-pass dispatch
    - do_pop(): pop and ticket-boundary handling
    - do_ticket_validate(): re-narrow, lint, suite, smoke, review
    - _run_feedback_retry(): apply queued tester/implementor feedback
    - do_push_review_findings(): push review-derived criteria
    - print_declined_criteria(): declined grounding output
    - _record_base_commit_if_needed(): git workflow helper

Usage:
        next_step [--model <model-id>] [--config <path>] [--continuous]
                            [--manual-test [--manual-test-ref <file::qualified_test_name> ...]]
                            [--skip-test] [--skip-implementation]
                            [--strategy <name>] [--log-level <level>]
"""

import argparse
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

from .lib import ai_client, render, tools, verbosity
from .lib import pipeline_lib as lib
from .lib.retry import resolve_retry_policy
from .strategies.registry import resolve_strategy

log = verbosity.get_logger(__name__)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"


FEEDBACK_READY_STATUS = lib.FEEDBACK_READY_STATUS


def _record_base_commit_if_needed(
    stack: list, frame: "lib.CriterionFrame", git_cfg: "lib.GitConfig | None"
) -> None:
    if git_cfg is None or not git_cfg.git_workflow or frame.base_commit is not None:
        return
    try:
        frame.base_commit = lib.git_current_head()
        lib.save_stack(stack)
    except lib.GitError as e:
        log.warning("-- git_workflow: could not record base_commit (non-fatal): %s", e)


def _run_feedback_retry(
    stack: list,
    frame: "lib.CriterionFrame",
    ctx: "lib.StepContext",
) -> None:
    """
    Apply queued user feedback on the top frame as a first-class retry path.
    The feedback is consumed exactly once here; a later correction requires a
    fresh `give-feedback` call.
    """
    target = frame.feedback_target
    feedback = frame.feedback
    if not target or not feedback:
        frame.status = "pending"
        lib.save_stack(stack)
        return

    if frame.feedback_attempts >= lib.FEEDBACK_MAX_RETRIES:
        lib.die_with_log(
            "feedback",
            f"Feedback retry limit reached ({lib.FEEDBACK_MAX_RETRIES}) for the top criterion. "
            "Fix it by hand or reset the criterion before asking for another automated retry.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )

    previous_changed_files = lib.git_changed_files()
    frame.feedback = None
    frame.feedback_target = None
    frame.feedback_attempts += 1

    if target == lib.FEEDBACK_TARGET_TESTER:
        if not ctx.git_cfg or not ctx.git_cfg.git_workflow or frame.base_commit is None:
            lib.die_with_log(
                "feedback",
                "Tester feedback requires git_workflow = true and a recorded base_commit so the "
                "previous test-writing attempt can be rolled back safely.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        try:
            lib.git_reset_hard(frame.base_commit)
        except lib.GitError as e:
            lib.die_with_log(
                "feedback",
                f"git reset --hard {frame.base_commit} failed before the tester retry: {e}",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        frame.status = "pending"
        frame.test_files = None
        frame.test_names = None
        frame.unconfirmed_tests = []
        frame.base_commit = None
        frame.commit_sha = None
        lib.save_stack(stack)
        lib.log_feedback_event(
            "apply-tester",
            feedback,
            criterion=frame.criterion,
            ticket=frame.ticket,
            target=target,
        )
        from .strategies.tdd import do_write_test as do_write_test_tdd

        do_write_test_tdd(
            stack,
            frame,
            ctx,
            feedback=feedback,
            previous_changed_files=previous_changed_files,
        )
        return

    if target == lib.FEEDBACK_TARGET_IMPLEMENTOR:
        lib.save_stack(stack)
        lib.log_feedback_event(
            "apply-implementor",
            feedback,
            criterion=frame.criterion,
            ticket=frame.ticket,
            target=target,
        )
        strategy = resolve_strategy(frame)
        frame.status = strategy.IMPL_AWAITING_STATUS
        lib.save_stack(stack)
        feedback_ctx = replace(ctx, accept_green=False, accept_manual=False)
        strategy.implement(
            frame,
            feedback_ctx,
            feedback=feedback,
            previous_changed_files=previous_changed_files,
        )
        strategy.recheck(stack, frame, feedback_ctx)
        return

    lib.die_with_log(
        "feedback",
        "Human-targeted feedback is not an automated retry path for "
        f"verification={frame.verification!r}.",
        criterion=frame.criterion,
        ticket=frame.ticket,
    )


def do_pop(
    frame: "lib.CriterionFrame",
    ctx: "lib.StepContext",
) -> None:
    just_popped_ticket = frame.ticket
    just_popped_criterion = frame.criterion
    # Layer 2: commit this criterion's worth of work *before* popping the
    # frame, while its criterion text is still in hand for the commit
    # message. A None (empty diff) is a logged skip, never a blocker -
    # the criterion is already verified green, that's the gate, not the
    # commit. A real git error is also non-fatal here: the POP still
    # happens so the stack advances; the uncommitted changes just ride
    # along into the next criterion's commit.
    if ctx.git_cfg is not None and ctx.git_cfg.git_workflow:
        try:
            sha = lib.commit_criterion(
                ctx.git_cfg, just_popped_ticket, just_popped_criterion
            )
            if sha is not None:
                frame.commit_sha = sha
                lib.log_event(
                    "git-workflow",
                    "criterion-committed",
                    ticket=just_popped_ticket,
                    criterion=just_popped_criterion,
                )
        except lib.GitError as e:
            log.warning("-- git_workflow: commit-on-POP failed (non-fatal): %s", e)
    lib.pop_frame()
    new_stack = lib.load_stack()

    # If we're returning to the validating sentinel for the same ticket,
    # keep the snapshot alive even if the sentinel was created without one.
    if (
        new_stack
        and new_stack[0].ticket == just_popped_ticket
        and new_stack[0].status == lib.VALIDATING_STATUS
        and new_stack[0].ticket_snapshot is None
        and frame.ticket_snapshot is not None
    ):
        new_stack[0].ticket_snapshot = frame.ticket_snapshot
        lib.save_stack(new_stack)

    render.print_line()
    render.print_line(f"-- Criterion done: {just_popped_criterion}")

    if not new_stack or new_stack[0].ticket != just_popped_ticket:
        do_ticket_validate(
            just_popped_ticket, ctx, ticket_snapshot=frame.ticket_snapshot
        )
        return

    if not ctx.continuous:
        render.print_line(f"-- Next: {new_stack[0].criterion}")
        render.print_line(f"-- Token usage: {ai_client.usage}")
        sys.exit(0)
    # continuous: fall through, letting the caller's loop re-dispatch
    # straight into the new top frame's WRITE_TEST phase.


def do_ticket_validate(
    ticket_id: str,
    ctx: "lib.StepContext",
    ticket_snapshot: str | None = None,
) -> None:
    """
    Full ticket-validation gate, run once a ticket's per-criterion
    frames are all popped: re-narrow (safety net for criteria the
    per-criterion gates missed), lint, full test suite, smoke test,
    code review. A CHANGES REQUESTED review or a non-empty safety-net
    re-narrow pushes new frames instead of failing outright - next_step
    is meant to be re-run, not treated as a one-shot gate.

    ticket_snapshot: the rendered ticket markdown captured at push_ticket
    seed time, propagated here via the last popped criterion frame (or
    the sentinel frame on a resumed retry). When present it is used
    directly, avoiding a round-trip to Linear. When None (--validate-only,
    --from-gap-plan, or an older stack file without the field), the ticket
    is fetched live from Linear as before.

    The very first thing this does is ensure a "validating" sentinel
    frame for ticket_id is on the stack (see lib.ensure_validating_sentinel -
    shared with push_ticket.py's --validate-only, which pushes the same
    sentinel directly without going through a pop first) - every step
    below this point is fallible (network fetch, AI calls, lint, the
    full test suite, smoke test), and the sentinel is what makes a
    failure at any of them resumable: it's only ever removed on an
    APPROVED verdict, so a re-run of `next_step` after a lint/test-suite/
    smoke failure (or a review the model failed to parse) finds the
    sentinel again and retries validation from scratch, instead of the
    ticket's "still needs validating" fact having vanished the moment
    its last real criterion was popped.
    """
    plan_model = ctx.step_models.get("plan", ctx.model)
    narrow_model = ctx.step_models.get("narrow", ctx.model)
    review_model = ctx.step_models.get("review", ctx.model)
    if ticket_snapshot is None:
        stack = lib.load_stack()
        top = stack[0] if stack else None
        if (
            top is not None
            and top.ticket == ticket_id
            and top.status == lib.VALIDATING_STATUS
            and top.ticket_snapshot is not None
        ):
            ticket_snapshot = top.ticket_snapshot
    lib.ensure_validating_sentinel(ticket_id, ticket_snapshot=ticket_snapshot)

    render.print_line()
    render.print_line(
        f"-- All criteria for {ticket_id} done. Running full ticket validation ..."
    )

    if ticket_snapshot is not None:
        ticket_content = ticket_snapshot
        tools.write_file_block(str(lib.TICKET_FILE))(ticket_content)
    else:
        ticket_content = lib.fetch_ticket_text(ticket_id)
        tools.write_file_block(str(lib.TICKET_FILE))(ticket_content)

    existing_plan = lib._resolve_plan_file()
    if existing_plan is not None:
        plan_text = existing_plan.read_text(encoding="utf-8")
    else:
        plan_text = lib.run_plan_step(ticket_content, plan_model, ticket_id=ticket_id)

    gap_plan_content = lib.run_narrow_step(
        ticket_content, plan_text, narrow_model, ticket_id=ticket_id
    )
    remaining = lib.extract_acceptance_criteria(gap_plan_content)
    if remaining:
        log.warning(
            "-- Safety-net re-narrow found %d criteria the per-criterion gates "
            "missed. This should not normally happen. Pushing them as new "
            "criteria instead of failing.",
            len(remaining),
        )
        candidate_frames = [
            lib.CriterionFrame(
                ticket=ticket_id,
                criterion=criterion,
                plan_context=lib.extract_plan_context_for_criterion(
                    criterion, gap_plan_content
                ),
                test_files=None,
                test_names=None,
                status="pending",
                origin="validate-missed",
                ticket_snapshot=ticket_content,
                # validate-missed criteria come from a re-narrow over
                # existing gap-plan text; use the parsed tags when
                # present, and fall back to "test"/"tdd" for older plans
                # that predate explicit tagging.
                verification=lib.extract_verification_mode(criterion) or "test",
                strategy=lib.extract_strategy(criterion) or "tdd",
                existing_test_refs=lib.extract_existing_test_refs(criterion),
            )
            for criterion in remaining
        ]
        missed_frames, newly_declined, skipped_count = lib.filter_grounded_frames(
            candidate_frames
        )
        if missed_frames:
            lib.push_frames(missed_frames)
        render.print_line()
        render.print_line(
            f"-- Ticket validation's re-narrow found {len(remaining)} criteria the "
            f"per-criterion gates missed."
        )
        if missed_frames:
            render.print_line(
                f"-- Pushed {len(missed_frames)} as new criteria. Run 'next_step' to "
                f"begin addressing them."
            )
            for missed in missed_frames:
                render.print_line(f"   {missed.criterion}")
        print_declined_criteria(newly_declined)
        if skipped_count:
            render.print_line(
                f"-- Skipped {skipped_count} criteria already in {lib.DECLINED_CRITERIA_FILE} "
                f"(previously declined)."
            )
        if not missed_frames:
            render.print_line(
                f"-- 0 of {len(remaining)} pushed - all were previously declined or failed "
                f"mechanical grounding this run. Ticket validation cannot proceed until this "
                f"is resolved by a human - see {lib.DECLINED_CRITERIA_FILE}."
            )
        render.print_line(f"-- Token usage: {ai_client.usage}")
        sys.exit(0)
        return

    lib.run_lint_gate(ctx.commands)

    result = lib.run_command(ctx.commands["test_cmd"], "full test suite gate")
    if result.returncode != 0:
        lib.die_with_log(
            "test-suite",
            f"Full test suite fails after all criteria implemented (exit "
            f"{result.returncode}). A criterion's scoped test passing doesn't "
            f"guarantee an earlier criterion's test still does - see output above.",
            ticket=ticket_id,
        )

    smoke_cmd = lib.load_smoke_cmd(ctx.config_path)
    lib.run_smoke_gate(smoke_cmd)

    changed_files = lib.git_changed_files()
    if not changed_files:
        lib.die_with_log(
            "review",
            "No changed files found (git diff/untracked are both empty). Nothing to review.",
            ticket=ticket_id,
        )

    # plan_text (the full original plan), not gap_plan_content: by this
    # point remaining is guaranteed empty (a non-empty one would have
    # exited above), so gap_plan_content's Acceptance Criteria section
    # always reads "nothing left to do" here - useless, misleading scope
    # for the reviewer. plan_text is the actual full ticket scope the
    # implementation was supposed to satisfy, same as the legacy
    # validate-and-review.py's review gate always used.
    verdict, review_text = lib.run_review_gate(
        changed_files, plan_text, review_model, ticket_id=ticket_id
    )

    if verdict == "APPROVED":
        # Validation is genuinely done now - remove the sentinel
        # (lib.ensure_validating_sentinel's counterpart) so a later
        # next_step call doesn't find a stale "still needs validating"
        # marker for a ticket that's already fully approved.
        lib.pop_frame()
        render.print_line()
        render.print_line("-- Summary:")
        render.print_line(f"   Ticket: {ticket_id}")
        render.print_line("   Acceptance criteria: all satisfied")
        render.print_line("   Lint: clean")
        render.print_line("   Test suite: passed")
        render.print_line(
            f"   Smoke test: {'passed' if smoke_cmd else 'skipped (not configured)'}"
        )
        render.print_line(
            f"   Files reviewed ({len(changed_files)}): {', '.join(changed_files)}"
        )
        render.print_line("   Code review: APPROVED")
        render.print_line()
        render.print_line(f"-- {ticket_id} fully validated. Success.")
        # Layer 3: merge the ticket branch back to its base (Tier 1) or
        # push + open a PR (Tier 2). Runs after the sentinel is popped so
        # a failure here can't leave a stale "still needs validating"
        # marker. Non-fatal: the verdict is already APPROVED, so a merge
        # conflict or push failure is surfaced as a warning, not a die.
        if ctx.git_cfg is not None:
            lib.post_validate_git(
                ctx.git_cfg,
                ticket_id,
                title=f"{ticket_id}: validated",
                body=f"Ticket {ticket_id} passed the pipeline's full validation gate "
                f"(lint, test suite, smoke, code review).",
            )
        render.print_line(f"-- Token usage: {ai_client.usage}")
        sys.exit(0)
        return

    do_push_review_findings(ticket_id, review_text, ticket_content=ticket_content)


def print_declined_criteria(
    newly_declined: list[tuple["lib.CriterionFrame", list[str]]],
) -> None:
    """
    Prints one loud block per criterion a mechanical grounding check just
    rejected (lib.filter_grounded_frames) - never silent, even though the
    run itself never blocks on it. Each entry has already been recorded
    to lib.DECLINED_CRITERIA_FILE by filter_grounded_frames as a side
    effect; this only makes the rejection visible in this run's output.
    """
    if not newly_declined:
        return
    render.print_line()
    noun = "criterion" if len(newly_declined) == 1 else "criteria"
    render.print_line(
        f"-- {len(newly_declined)} {noun} failed mechanical grounding - NOT pushed:"
    )
    for frame, reasons in newly_declined:
        render.print_line(f"   {frame.criterion}")
        for reason in reasons:
            render.print_line(f"     - {reason}")
    render.print_line(
        f"-- Not resolved automatically. Fix the ticket/gap-plan wording, or if this is a "
        f"false positive, review and clear the entry from {lib.DECLINED_CRITERIA_FILE}."
    )


def do_push_review_findings(
    ticket_id: str,
    review_text: str,
    ticket_content: str | None = None,
) -> None:
    findings = lib.extract_review_findings(review_text)
    if not findings:
        lib.die_with_log(
            "review",
            "Review verdict was CHANGES REQUESTED but no parseable findings were "
            "found in its output (see output above). Refusing to push zero frames "
            "for a failed review.",
            ticket=ticket_id,
        )
    candidate_frames = [
        lib.CriterionFrame(
            ticket=ticket_id,
            criterion=f"- [ ] {finding}",
            plan_context=finding,
            test_files=None,
            test_names=None,
            status="pending",
            origin="review",
            ticket_snapshot=ticket_content,
            # review findings come from reviewer prose and never carry
            # strategy tags; default to "tdd" for backward compatibility.
            strategy=lib.extract_strategy(f"- [ ] {finding}") or "tdd",
        )
        for finding in findings
    ]
    new_frames, newly_declined, skipped_count = lib.filter_grounded_frames(
        candidate_frames
    )
    if new_frames:
        lib.push_frames(new_frames)
    render.print_line()
    render.print_line(f"-- Review found {len(findings)} issue(s).")
    if new_frames:
        render.print_line(
            f"-- Pushed {len(new_frames)} as new criteria. Run 'next_step' to begin "
            f"addressing them."
        )
        for new_frame in new_frames:
            render.print_line(f"   {new_frame.criterion}")
    print_declined_criteria(newly_declined)
    if skipped_count:
        render.print_line(
            f"-- Skipped {skipped_count} finding(s) already in {lib.DECLINED_CRITERIA_FILE} "
            f"(previously declined)."
        )
    if not new_frames:
        render.print_line(
            f"-- 0 of {len(findings)} pushed - all were previously declined or failed "
            f"mechanical grounding this run. See {lib.DECLINED_CRITERIA_FILE}."
        )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def step(
    model: str,
    commands: dict,
    continuous: bool,
    config_path: Path,
    step_models: dict[str, str] | None = None,
    accept_green: bool = False,
    accept_manual: bool = False,
    accept_no_test: bool = False,
    manual_test: bool = False,
    manual_test_refs: list[str] | None = None,
    skip_test: bool = False,
    skip_implementation: bool = False,
    allow_compile: bool = True,
    reset_on_retry: bool = True,
    max_attempts: int = 3,
    retry_policy=None,
    git_cfg: "lib.GitConfig | None" = None,
    strategy_override: str | None = None,
) -> None:
    """
    One pass of phase detection + dispatch. Returns normally only when
    the caller's loop should immediately re-detect and dispatch again
    (a green test cascading into POP, or --continuous advancing into
    the next criterion) - every other path exits the process directly.
    """
    step_models = step_models or {}
    stack = lib.load_stack()
    if not stack:
        render.print_line("-- No work remaining. Stack is empty.")
        sys.exit(0)

    frame = stack[0]
    if strategy_override:
        frame.strategy = strategy_override
        lib.save_stack(stack)
    log.info(
        "-- next_step: ticket=%s status=%s criterion=%s",
        frame.ticket,
        frame.status,
        frame.criterion,
    )

    ctx = lib.StepContext(
        model=model,
        step_models=step_models,
        commands=commands,
        config_path=config_path,
        continuous=continuous,
        max_attempts=max_attempts,
        retry_policy=retry_policy,
        accept_green=accept_green,
        accept_manual=accept_manual,
        accept_no_test=accept_no_test,
        skip_implementation=skip_implementation,
        allow_compile=allow_compile,
        reset_on_retry=reset_on_retry,
        git_cfg=git_cfg,
    )

    if manual_test:
        if frame.status != "pending":
            lib.die_with_log(
                "manual-test",
                "Manual test mode only applies when the top frame is pending.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        strategy_module = resolve_strategy(frame)
        if not hasattr(strategy_module, "manual_test_authoring"):
            lib.die_with_log(
                "manual-test",
                f"Manual test mode is not valid for strategy={frame.strategy!r}.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        strategy_module.manual_test_authoring(stack, frame, ctx, manual_test_refs)
        return

    if skip_test:
        if frame.status != "pending":
            lib.die_with_log(
                "skip-test",
                "Skip-test mode only applies when the top frame is pending.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        strategy_module = resolve_strategy(frame)
        if not hasattr(strategy_module, "skip_test_implementation"):
            lib.die_with_log(
                "skip-test",
                f"Skip-test mode is not valid for strategy={frame.strategy!r}.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        if skip_implementation:
            lib.die_with_log(
                "skip-implementation",
                "--skip-implementation cannot be combined with --skip-test.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        strategy_module.skip_test_implementation(stack, frame, ctx)
        return

    if frame.status == lib.VALIDATING_STATUS:
        # A prior TICKET_VALIDATE attempt for this ticket died partway
        # through (lint/test-suite/smoke/unparseable review) and left
        # this sentinel behind - re-enter validation directly, no pop
        # needed (there's nothing left to pop; the real criteria are
        # long gone). Pass the sentinel's snapshot so a resumed retry
        # still uses the original ticket text rather than re-fetching.
        do_ticket_validate(
            frame.ticket,
            ctx,
            ticket_snapshot=frame.ticket_snapshot,
        )
        return

    if frame.status == FEEDBACK_READY_STATUS:
        _run_feedback_retry(
            stack,
            frame,
            ctx,
        )
        return

    if frame.status == "done":
        do_pop(frame, ctx)
        return

    strategy_module = resolve_strategy(frame)
    strategy_module.advance(stack, frame, ctx)
    return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Advance the criteria stack by exactly one phase, pausing "
        "only when genuinely human-only input is required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"opencode zen model ID to use (default: {DEFAULT_MODEL}). "
        f"Overrides [step_models] in .dev-pipeline.toml for all steps",
    )
    parser.add_argument(
        "--config",
        default=str(lib.PIPELINE_CONFIG_FILE),
        help=f"Path to the build/test command config (default: {lib.PIPELINE_CONFIG_FILE}).",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Advance through every automatable transition without pausing, "
        "stopping only when human input is genuinely required "
        "(confirmation/acceptance pauses, or the stack going empty).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Total implementation attempts per criterion, initial write + "
        "refines sharing one budget (default: 3).",
    )
    parser.add_argument(
        "--retry-policy",
        default=None,
        choices=["fixed-budget", "endless"],
        help="Retry policy for refine loops. Default: fixed-budget with --max-attempts.",
    )
    parser.add_argument(
        "--accept-green",
        action="store_true",
        help="Accept the top frame as satisfied if it's currently paused "
        "in the 'green-unconfirmed' state (a validate-missed/review "
        "criterion whose test(s) passed immediately, without any "
        "changes - see the pause message for exactly which one(s) and "
        "why that's untrusted by default). Has no effect if the top "
        "frame isn't in that state. Use this only after confirming the behaviour really "
        "is already present - not as a way to silence the warning.",
    )
    parser.add_argument(
        "--accept-manual",
        action="store_true",
        help="Accept the top frame as satisfied if it's currently paused in the "
        "'awaiting-manual-impl' state (a verification=\"manual\" criterion - "
        "documentation, config, etc.). Overrides the mechanical floor check "
        "(did a referenced file actually change) whether or not one could be "
        "identified in the first place - use this after confirming the "
        "change is actually made, whether the automatic check missed it or "
        "there was nothing for it to check at all. Has no effect if the top "
        "frame isn't in that state, or if the automatic check already "
        "confirmed it (nothing to override).",
    )
    parser.add_argument(
        "--accept-no-test",
        action="store_true",
        help="Accept the top frame as satisfied if it's currently paused in the "
        "'nothing-written' state (a criterion whose WRITE_TEST run produced "
        "no test files at all - the tester re-read the code and wrote "
        "nothing, a strong signal the criterion may already be satisfied, "
        "e.g. a test-refactor that landed before or during the run). "
        "Overrides the mechanical pre-check and the AI re-check - use this "
        "after confirming the criterion really is already met. Has no effect "
        "if the top frame isn't in that state, or if the mechanical check or "
        "AI re-check already confirmed it (nothing to override).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--manual-test",
        action="store_true",
        help="Use manually authored test(s) for the top pending test criterion "
        "instead of invoking the Tester AI. Requires scoped test references "
        "from --manual-test-ref or existing_test: tags.",
    )
    parser.add_argument(
        "--manual-test-ref",
        action="append",
        default=[],
        metavar="FILE::QUALIFIED_TEST",
        help="Scoped test reference for --manual-test. Repeatable. Format: "
        "<file>::<qualified_test_name>.",
    )
    mode_group.add_argument(
        "--skip-test",
        action="store_true",
        help="Skip WRITE_TEST for the top pending verify:test criterion and hand "
        "it directly to the Implementor with build-only gating.",
    )
    parser.add_argument(
        "--skip-implementation",
        action="store_true",
        help="Require manual implementation for red tests (pause instead of "
        "running the Implementor AI).",
    )
    parser.add_argument(
        "--no-compile-tool",
        action="store_true",
        help=(
            "Disable the AI 'compile' tool for this run. By default it is "
            "enabled and runs build_cmd during the model turn for in-turn "
            "compile checks."
        ),
    )
    parser.add_argument(
        "--no-reset-on-retry",
        action="store_true",
        help="Disable fresh-start retries for this run. By default retries "
        "reset to the test commit when available.",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        choices=["tdd", "direct"],
        help="Override the top frame's strategy for this run. Defaults to "
        "the frame's own strategy or the plan tag.",
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
    if args.manual_test_ref and not args.manual_test:
        parser.error("--manual-test-ref requires --manual-test")
    verbosity.setup_logging(args.log_level)

    config_path = Path(args.config)
    commands = lib.load_pipeline_config(config_path)
    tools_cfg = lib.load_tools_config(config_path)
    retry_cfg: dict = {}
    if config_path.exists():
        with config_path.open("rb") as f:
            loaded = tomllib.load(f)
        retry_value = loaded.get("retry")
        if isinstance(retry_value, dict):
            retry_cfg = retry_value

    model, step_models = lib.resolve_step_models(config_path, args.model)
    git_cfg = lib.load_git_config(config_path)
    try:
        retry_policy = resolve_retry_policy(
            config_path, args.retry_policy, args.max_attempts
        )
    except ValueError as e:
        parser.error(str(e))
    allow_compile_cfg = tools_cfg.get("compile", True)
    reset_on_retry_cfg = retry_cfg.get("reset_on_retry", True)
    allow_compile = bool(allow_compile_cfg) and not args.no_compile_tool
    reset_on_retry = bool(reset_on_retry_cfg) and not args.no_reset_on_retry

    while True:
        step(
            model,
            commands,
            args.continuous,
            config_path,
            step_models=step_models,
            accept_green=args.accept_green,
            accept_manual=args.accept_manual,
            accept_no_test=args.accept_no_test,
            manual_test=args.manual_test,
            manual_test_refs=args.manual_test_ref,
            skip_test=args.skip_test,
            skip_implementation=args.skip_implementation,
            allow_compile=allow_compile,
            reset_on_retry=reset_on_retry,
            max_attempts=args.max_attempts,
            retry_policy=retry_policy,
            git_cfg=git_cfg,
            strategy_override=args.strategy,
        )


if __name__ == "__main__":
    main()
