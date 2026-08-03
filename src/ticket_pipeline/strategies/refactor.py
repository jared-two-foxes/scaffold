"""Refactor strategy handler."""

import sys

from .. import next_step
from ..lib import ai_client, render
from ..lib import implement as implement_lib
from ..lib import pipeline_lib as lib

PHASES = ["pending", "baseline-confirmed", "done"]
IMPL_AWAITING_STATUS = "baseline-confirmed"


def do_refactor_setup(
    stack: list,
    frame: "lib.CriterionFrame",
    commands: dict,
    git_cfg: "lib.GitConfig | None" = None,
) -> None:
    next_step._record_base_commit_if_needed(stack, frame, git_cfg)

    test_files: list[str] = []
    test_names: list[str] = []
    for ref in frame.existing_test_refs:
        file_path, _, test_name = ref.partition("::")
        test_files.append(file_path)
        test_names.append(test_name)
    frame.test_files = test_files
    frame.test_names = test_names

    if not test_names:
        lib.die_with_log(
            "refactor-setup",
            "This criterion is tagged verify:refactor but carries no "
            "existing_test: refs - a refactor with no identifiable safety "
            "net should have been tagged verify:manual by the narrower. "
            "Fix the gap plan's tag or tag it manual, then re-run.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )

    results = lib.run_scoped_tests(
        test_names, commands, "refactor baseline check", quiet=True
    )
    red_names = [n for n, r in zip(test_names, results) if r.returncode != 0]
    if red_names:
        red_list = "\n".join(f"  - {n}" for n in red_names)
        lib.die_with_log(
            "refactor-setup",
            f"Safety-net tests are RED at baseline - the safety net must be "
            f"GREEN before refactoring. A GREEN-after-refactor check is "
            f"meaningless if the tests were red to begin with. Fix the "
            f"failing test(s) (or verify the existing_test refs are correct) "
            f"before re-running.\nRed at baseline:\n{red_list}",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )

    frame.status = lib.BASELINE_CONFIRMED_STATUS
    lib.save_stack(stack)
    render.print_line()
    render.print_line("-- Refactor baseline confirmed: all safety-net tests GREEN.")
    for f, n in zip(test_files, test_names):
        render.print_line(f"   {f} :: {n}")
    render.print_line(f"   Criterion: {frame.criterion}")
    render.print_line(
        "   Make the structural changes by hand, or run 'next_step' again to "
        "let the pipeline implement them automatically. A later 'next_step' "
        "run re-runs the safety-net tests and pops only if they're still "
        "GREEN *and* a production file actually changed."
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def recheck_refactor_tests(
    stack: list,
    frame: "lib.CriterionFrame",
    commands: dict,
    git_cfg: "lib.GitConfig | None" = None,
) -> None:
    results = lib.run_scoped_tests(
        frame.test_names, commands, "refactor recheck", quiet=True
    )
    red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
    if red_names:
        frame.status = lib.BASELINE_CONFIRMED_STATUS
        lib.save_stack(stack)
        render.print_line()
        render.print_line("-- Refactor broke safety-net test(s):")
        for n, r in zip(frame.test_names, results):
            if r.returncode != 0:
                render.print_line(f"   RED: {n}")
        render.print_line(f"   Criterion: {frame.criterion}")
        render.print_line(
            "   Fix the refactor by hand, or run 'next_step' again to let the "
            "pipeline repair it automatically. The safety-net tests must be "
            "GREEN before this criterion can pop."
        )
        render.print_line(f"-- Token usage: {ai_client.usage}")
        sys.exit(0)

    paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
    if paths and not (set(paths) & set(lib.git_changed_files())):
        frame.status = lib.BASELINE_CONFIRMED_STATUS
        lib.save_stack(stack)
        render.print_line()
        render.print_line(
            "-- Safety-net tests are GREEN but no production file has changed yet."
        )
        if frame.test_names:
            render.print_line("   Safety-net test(s) (still GREEN):")
            for f, n in zip(frame.test_files, frame.test_names):
                render.print_line(f"     {f} :: {n}")
        render.print_line(f"   Criterion: {frame.criterion}")
        render.print_line(
            "   Make the structural changes by hand, or run 'next_step' again "
            "to let the pipeline make them automatically."
        )
        render.print_line(f"-- Token usage: {ai_client.usage}")
        sys.exit(0)

    frame.status = "done"
    lib.save_stack(stack)
    return


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status != lib.BASELINE_CONFIRMED_STATUS:
        render.print_line(
            "-- Top frame is a refactor criterion but its status "
            + repr(frame.status)
            + " is not awaiting implementation. Run 'next_step' first to establish the baseline."
        )
        sys.exit(1)
    if not frame.test_files or not frame.test_names:
        render.print_line(
            "-- Refactor frame is baseline-confirmed but has no safety-net "
            "test(s) recorded. Run 'next_step' to re-run refactor setup."
        )
        sys.exit(1)

    green_results = lib.run_scoped_tests(
        frame.test_names, ctx.commands, "pre-refactor green check"
    )
    red_names = [
        n for n, r in zip(frame.test_names, green_results) if r.returncode != 0
    ]
    if red_names:
        render.print_line(
            "-- Safety-net test(s) are RED - the refactor cannot proceed "
            "until they are GREEN again (the safety net must hold before and "
            "after the refactor). Fix them first, then re-run."
        )
        for n in red_names:
            render.print_line("   RED: " + n)
        sys.exit(1)

    render.print_line()
    render.print_line("-- Refactoring (keeping safety-net tests GREEN):")
    for test_file, test_name in zip(frame.test_files, frame.test_names):
        render.print_line("   " + test_file + " :: " + test_name)
    render.print_line("   Criterion: " + frame.criterion)

    changed_files = implement_lib.run_implement_with_refine(
        frame,
        ctx.model,
        ctx.commands,
        ctx.max_attempts,
        retry_policy=ctx.retry_policy,
        verification=frame.verification,
        feedback=feedback,
        previous_changed_files=previous_changed_files,
        allow_compile=ctx.allow_compile,
        compile_cmd=ctx.commands.get("build_cmd"),
        reset_on_retry=ctx.reset_on_retry,
        test_commit_sha=frame.test_commit_sha,
    )

    render.print_line()
    render.print_line("-- Refactored: " + frame.criterion)
    render.print_line(
        "   All " + str(len(frame.test_names)) + " safety-net test(s) still GREEN:"
    )
    for test_file, test_name in zip(frame.test_files, frame.test_names):
        render.print_line("     " + test_file + " :: " + test_name)
    render.print_line(
        "   Files changed ("
        + str(len(changed_files))
        + "): "
        + ", ".join(changed_files)
    )
    render.print_line("-- Token usage: " + str(ai_client.usage))
    return changed_files


def recheck(stack, frame, ctx):
    return recheck_refactor_tests(stack, frame, ctx.commands, ctx.git_cfg)


def advance(stack, frame, ctx):
    if frame.status == "pending":
        do_refactor_setup(stack, frame, ctx.commands, ctx.git_cfg)
        return

    if frame.status == lib.BASELINE_CONFIRMED_STATUS:
        results = lib.run_scoped_tests(
            frame.test_names, ctx.commands, "refactor pre-implement check", quiet=True
        )
        red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
        paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
        if not red_names and (not paths or (set(paths) & set(lib.git_changed_files()))):
            frame.status = "done"
            lib.save_stack(stack)
            return

        implement(frame, ctx)
        if ctx.continuous:
            return
        sys.exit(0)

    if frame.status == "done":
        next_step.do_pop(frame, ctx)
        return

    sys.exit(0)
