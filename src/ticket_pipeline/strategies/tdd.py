"""TDD strategy handlers for criterion execution."""

import subprocess
import sys

from .. import next_step
from ..lib import (
    ai_client,
    implement as implement_lib,
    pipeline_lib as lib,
    render,
    verbosity,
)

log = verbosity.get_logger(__name__)

PHASES = ["pending", "test-written", "green-unconfirmed", "nothing-written", "done"]
IMPL_AWAITING_STATUS = "test-written"
RED_CHECK_OUTPUT_TAIL_CHARS = 4000
GREEN_UNCONFIRMED_STATUS = "green-unconfirmed"
NOTHING_WRITTEN_STATUS = "nothing-written"


def do_await_impl(
    frame: "lib.CriterionFrame",
    test_results: list[tuple[str, str, subprocess.CompletedProcess]],
) -> None:
    render.print_line()
    red = [(f, n, r) for f, n, r in test_results if r.returncode != 0]
    green = [(f, n, r) for f, n, r in test_results if r.returncode == 0]
    if len(test_results) == 1:
        render.print_line("-- Test written. Implement now:")
    else:
        render.print_line(
            f"-- {len(red)} of {len(test_results)} test(s) still to implement:"
        )
    for f, n, _ in red:
        render.print_line(f"   {f} :: {n}")
    if green:
        render.print_line("   Already passing (no action needed on these):")
        for f, n, _ in green:
            tag = (
                " - unconfirmed, weak-test risk" if n in frame.unconfirmed_tests else ""
            )
            render.print_line(f"     {f} :: {n}{tag}")
    render.print_line(f"   Criterion: {frame.criterion}")
    render.print_line(
        "   Manual implementation required (--skip-implementation is set)."
    )
    render.print_line("   Implement it by hand, then run 'next_step' to re-check.")
    render.print_line(
        "   (Or run 'next_step' without --skip-implementation for AI implementation.)"
    )
    for _, n, r in red:
        output = ((r.stdout or "") + (r.stderr or "")).strip()
        if output:
            signal = lib.extract_test_output_signal(
                output, lib.get_toolchain().test_output_signal_pattern
            )
            render.print_line()
            label = f" for {n}" if len(test_results) > 1 else ""
            render.print_line(f"-- Red test output{label} (why it currently fails):")
            render.print_line(signal[-RED_CHECK_OUTPUT_TAIL_CHARS:])
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def do_await_green_unconfirmed(frame: "lib.CriterionFrame") -> None:
    render.print_line()
    unconfirmed = set(frame.unconfirmed_tests)
    plural = len(unconfirmed) != 1
    render.print_line(
        f"-- Test(s) passed, but {'one has' if not plural else 'some have'} "
        f"not been confirmed legitimate:"
    )
    for file_path, name in zip(frame.test_files, frame.test_names):
        tag = (
            " - UNCONFIRMED (passed without any implementation)"
            if name in unconfirmed
            else " - confirmed"
        )
        render.print_line(f"   {file_path} :: {name}{tag}")
    render.print_line(f"   Criterion: {frame.criterion}")
    render.print_line(f"   Origin: {frame.origin}")
    render.print_line()
    render.print_line(
        f"   This criterion came from {frame.origin!r}, not the ticket's initial "
        f"criteria - it exists specifically because an earlier check just judged "
        f"it unsatisfied. The UNCONFIRMED test(s) above passed this easily, which "
        f"is much more likely a weak test (not exercising the described behavior) "
        f"than the gap genuinely having disappeared. Either:"
    )
    render.print_line(
        "     - inspect the unconfirmed test(s) and fix them if they're not testing the right thing,"
    )
    render.print_line(
        "       then run 'next_step' again (a now-red test resumes the normal flow), or"
    )
    render.print_line(
        "     - if you're confident the behaviour really is already present, run "
        "'next_step --accept-green' to accept every unconfirmed test above and move on."
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def do_write_test(
    stack: list,
    frame: "lib.CriterionFrame",
    ctx: "lib.StepContext",
    feedback: str | None = None,
    previous_changed_files: list[str] | None = None,
) -> None:
    next_step._record_base_commit_if_needed(stack, frame, ctx.git_cfg)
    file_paths, test_names, test_results, compile_result, quality_concern = (
        lib.run_test_for_criterion_with_full_retry(
            frame.criterion,
            frame.plan_context,
            ctx.model,
            ctx.commands,
            retry_policy=ctx.retry_policy,
            ticket_id=frame.ticket,
            existing_test_refs=frame.existing_test_refs,
            verification=frame.verification,
            feedback=feedback,
            previous_changed_files=previous_changed_files,
        )
    )
    if file_paths is None:
        _handle_no_test_written(stack, frame, ctx)
        return
    if compile_result is None or compile_result.returncode != 0:
        exit_code = (
            compile_result.returncode if compile_result is not None else "unknown"
        )
        lib.die_with_log(
            "test-criterion",
            f"Test does not compile after retries (exit {exit_code}). See output above.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )

    if quality_concern:
        lib.log_event(
            "review-test-quality",
            "flagged-advisory-fallback",
            error=quality_concern,
            criterion=frame.criterion,
            ticket=frame.ticket,
        )
        render.print_line()
        render.print_line(
            "-- Test-quality review still flagged after retries (advisory, not blocking):"
        )
        render.print_line(quality_concern)

    frame.test_files = file_paths
    frame.test_names = test_names

    test_results_zipped = list(zip(file_paths, test_names, test_results))
    red_names = [n for n, r in zip(test_names, test_results) if r.returncode != 0]
    green_names = [n for n, r in zip(test_names, test_results) if r.returncode == 0]
    unconfirmed = [] if frame.origin == "ticket" else green_names

    if not red_names and not unconfirmed:
        log.info(
            "-- Test(s) passed without implementation - this criterion's "
            "gap didn't reproduce."
        )
        frame.status = "done"
        frame.unconfirmed_tests = []
        lib.save_stack(stack)
        return

    if not red_names and unconfirmed:
        frame.status = GREEN_UNCONFIRMED_STATUS
        frame.unconfirmed_tests = unconfirmed
        lib.save_stack(stack)
        do_await_green_unconfirmed(frame)
        return

    frame.status = "test-written"
    frame.unconfirmed_tests = unconfirmed
    lib.save_stack(stack)
    if ctx.skip_implementation:
        do_await_impl(frame, test_results_zipped)
        return
    advance(stack, frame, ctx)


def _handle_no_test_written(
    stack: list,
    frame: "lib.CriterionFrame",
    ctx: "lib.StepContext",
    skip_ai: bool = False,
) -> None:
    if ctx.accept_no_test:
        log.info(
            "-- --accept-no-test: accepting %s as satisfied despite the "
            "tester writing nothing.",
            frame.criterion,
        )
        frame.status = "done"
        frame.unconfirmed_tests = []
        lib.save_stack(stack)
        return
    if lib.check_test_refactor_satisfied(frame.criterion, frame.existing_test_refs):
        log.info(
            "-- Criterion already satisfied (mechanical check) - popping "
            "without a written test."
        )
        frame.status = "done"
        frame.unconfirmed_tests = []
        lib.save_stack(stack)
        return

    if not skip_ai:
        verdict = lib.recheck_single_criterion(
            frame.criterion,
            frame.plan_context,
            ctx.model,
            ticket_id=frame.ticket,
        )
        if verdict == "SATISFIED":
            log.info(
                "-- Recheck verdict SATISFIED - criterion already met in "
                "current code. Popping without a written test."
            )
            frame.status = "done"
            frame.unconfirmed_tests = []
            lib.save_stack(stack)
            return

    frame.status = NOTHING_WRITTEN_STATUS
    lib.save_stack(stack)
    render.print_line()
    render.print_line("-- Tester wrote no test files for this criterion.")
    render.print_line(
        "-- The criterion may already be satisfied, but it could not be "
        "confirmed mechanically"
        + (" or by an AI re-check" if not skip_ai else "")
        + "."
    )
    render.print_line("-- Review the criterion and the current code:")
    render.print_line(f"   {frame.criterion}")
    render.print_line(
        "-- If satisfied, run 'next_step --accept-no-test' to pop this frame."
    )
    render.print_line(
        "-- If not satisfied, investigate why the tester produced nothing "
        "(the gap plan's 'why:' may be stale - the refactor may have "
        "already landed)."
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def recheck_test_frame(
    stack: list,
    frame: "lib.CriterionFrame",
    ctx: "lib.StepContext",
) -> None:
    results = lib.run_scoped_tests(
        frame.test_names, ctx.commands, "phase check", quiet=True
    )
    test_results = list(zip(frame.test_files, frame.test_names, results))
    red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
    frame.unconfirmed_tests = [n for n in frame.unconfirmed_tests if n not in red_names]

    if red_names:
        frame.status = "test-written"
        lib.save_stack(stack)
        if ctx.skip_implementation:
            do_await_impl(frame, test_results)
            return
        implement(frame, ctx)
        return

    if not frame.unconfirmed_tests:
        frame.status = "done"
        lib.save_stack(stack)
        return

    if ctx.accept_green:
        log.info(
            "-- --accept-green: accepting %s as satisfied despite origin=%r "
            "(unconfirmed test(s): %s).",
            frame.criterion,
            frame.origin,
            ", ".join(frame.unconfirmed_tests),
        )
        frame.status = "done"
        frame.unconfirmed_tests = []
        lib.save_stack(stack)
        return

    frame.status = GREEN_UNCONFIRMED_STATUS
    lib.save_stack(stack)
    do_await_green_unconfirmed(frame)


def _parse_manual_test_refs(
    frame: "lib.CriterionFrame", manual_test_refs: list[str] | None
) -> tuple[list[str], list[str]]:
    refs = manual_test_refs or frame.existing_test_refs or []
    if not refs:
        lib.die_with_log(
            "manual-test",
            "Manual test mode needs at least one test reference. Pass "
            "--manual-test-ref <file::qualified_test_name> (repeatable), or "
            "use a criterion with existing_test: refs.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )
    test_files: list[str] = []
    test_names: list[str] = []
    for ref in refs:
        file_path, sep, test_name = ref.partition("::")
        file_path = file_path.strip(" `")
        test_name = test_name.strip(" `")
        if not sep or not file_path or not test_name:
            lib.die_with_log(
                "manual-test",
                f"Invalid manual test reference {ref!r}. Expected "
                "<file>::<qualified_test_name>.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        test_files.append(file_path)
        test_names.append(test_name)
    return test_files, test_names


def manual_test_authoring(
    stack: list,
    frame: "lib.CriterionFrame",
    ctx: "lib.StepContext",
    manual_test_refs: list[str] | None,
) -> None:
    next_step._record_base_commit_if_needed(stack, frame, ctx.git_cfg)
    test_files, test_names = _parse_manual_test_refs(frame, manual_test_refs)
    frame.test_files = test_files
    frame.test_names = test_names
    compile_result = lib.run_command(
        ctx.commands["test_compile_cmd"], "manual test compile gate"
    )
    if compile_result.returncode != 0:
        lib.die_with_log(
            "manual-test",
            f"Manual test compile gate failed (exit {compile_result.returncode}). "
            "Fix the test(s) and re-run 'next_step --manual-test'.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )
    scoped_results = lib.run_scoped_tests(
        test_names, ctx.commands, "manual test red check", quiet=True
    )
    red_names = [n for n, r in zip(test_names, scoped_results) if r.returncode != 0]
    green_names = [n for n, r in zip(test_names, scoped_results) if r.returncode == 0]
    unconfirmed = [] if frame.origin == "ticket" else green_names

    if not red_names and not unconfirmed:
        frame.status = "done"
        frame.unconfirmed_tests = []
        lib.save_stack(stack)
        return
    if not red_names and unconfirmed:
        frame.status = GREEN_UNCONFIRMED_STATUS
        frame.unconfirmed_tests = unconfirmed
        lib.save_stack(stack)
        do_await_green_unconfirmed(frame)
        return

    frame.status = "test-written"
    frame.unconfirmed_tests = unconfirmed
    lib.save_stack(stack)
    test_results = list(zip(test_files, test_names, scoped_results))
    if ctx.skip_implementation:
        do_await_impl(frame, test_results)
        return
    advance(stack, frame, ctx)


def skip_test_implementation(
    stack: list,
    frame: "lib.CriterionFrame",
    ctx: "lib.StepContext",
) -> None:
    next_step._record_base_commit_if_needed(stack, frame, ctx.git_cfg)
    implement_lib.run_implement_direct_with_refine(
        frame, ctx.model, ctx.commands, ctx.max_attempts, retry_policy=ctx.retry_policy
    )
    _handle_no_test_written(
        stack,
        frame,
        ctx,
        skip_ai=False,
    )


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status != "test-written":
        render.print_line(
            f"-- Top frame is not awaiting implementation (status: {frame.status!r}). Run 'next_step' first."
        )
        sys.exit(1)
    if not frame.test_files or not frame.test_names:
        render.print_line(
            f"-- Top frame is not awaiting implementation (status: {frame.status!r}, no test recorded). Run 'next_step' first."
        )
        sys.exit(1)
    if frame.verification == "test-refactor":
        render.print_line(
            "-- This is a test-refactor criterion whose rewrite came back RED."
        )
        render.print_line(
            "   There is no production code to implement - the rewrite itself"
        )
        render.print_line("   is incorrect. Fix the test by hand (keep its assertions")
        render.print_line(
            "   functionally identical; change only the structural elements"
        )
        render.print_line(
            "   the criterion describes), then run 'next_step' to re-check."
        )
        sys.exit(1)

    red_results = lib.run_scoped_tests(
        frame.test_names, ctx.commands, "pre-implement red check"
    )
    still_red = [n for n, r in zip(frame.test_names, red_results) if r.returncode != 0]
    if not still_red:
        render.print_line(
            f"-- All {len(frame.test_names)} test(s) already green. Nothing to implement. Run 'next_step' to pop this criterion."
        )
        sys.exit(0)

    render.print_line()
    if len(frame.test_names) == 1:
        render.print_line("-- Implementing:")
    else:
        render.print_line(
            f"-- Implementing ({len(still_red)} of {len(frame.test_names)} still red):"
        )
    for test_file, test_name in zip(frame.test_files, frame.test_names):
        tag = "" if test_name in still_red else " (already passing)"
        render.print_line(f"   {test_file} :: {test_name}{tag}")
    render.print_line(f"   Criterion: {frame.criterion}")

    changed_files = implement_lib.run_implement_with_refine(
        frame,
        ctx.model,
        ctx.commands,
        ctx.max_attempts,
        retry_policy=ctx.retry_policy,
        feedback=feedback,
        previous_changed_files=previous_changed_files,
    )

    render.print_line()
    render.print_line(f"-- Implemented: {frame.criterion}")
    if len(frame.test_names) == 1:
        render.print_line(
            f"   Test now green: {frame.test_files[0]} :: {frame.test_names[0]}"
        )
    else:
        render.print_line(f"   All {len(frame.test_names)} test(s) now green:")
        for test_file, test_name in zip(frame.test_files, frame.test_names):
            render.print_line(f"     {test_file} :: {test_name}")
    render.print_line(
        f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}"
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")


def recheck(stack, frame, ctx):
    return recheck_test_frame(stack, frame, ctx)


def advance(stack, frame, ctx):
    """TDD strategy dispatch: test-first -> implement -> verify green."""
    if frame.status == GREEN_UNCONFIRMED_STATUS:
        recheck_test_frame(stack, frame, ctx)
        return

    if frame.status == NOTHING_WRITTEN_STATUS:
        _handle_no_test_written(stack, frame, ctx, skip_ai=True)
        return

    if frame.status == "pending":
        if frame.verification == "test-refactor" and lib.check_test_refactor_satisfied(
            frame.criterion, frame.existing_test_refs
        ):
            frame.status = "done"
            frame.unconfirmed_tests = []
            lib.save_stack(stack)
            return
        do_write_test(
            stack,
            frame,
            ctx,
        )
        return

    if frame.status == "test-written":
        if not frame.test_files or not frame.test_names:
            do_write_test(
                stack,
                frame,
                ctx,
            )
            return
        recheck_test_frame(stack, frame, ctx)
        return

    if frame.status == "done":
        next_step.do_pop(frame, ctx)
        return

    sys.exit(0)
