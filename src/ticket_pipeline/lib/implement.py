"""Implementation execution mechanics shared by all strategies.

Contains:
- run_implement_with_refine: test-driven loop (build + green gate + tamper guard + refine)
- run_implement_direct_with_refine: direct loop (build-only gate, no tamper guard)
- Prompt builders for each implementation mode (test, direct, refactor, feedback)
- Test tamper guard (snapshot + byte-for-byte verification)

Strategy modules select which building block to call via their implement() method.
This module owns HOW implementation works; strategies own WHEN and WHICH.
"""

import re
import subprocess
from pathlib import Path

from . import pipeline_lib as lib
from . import tools, verbosity
from .ai_client import AIError, run_with_tools
from .retry import RetryPolicy

log = verbosity.get_logger(__name__)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"
DEFAULT_MAX_ATTEMPTS = 3

IMPLEMENT_CRITERION_PROMPT_FILE = lib.PROMPTS_DIR / "implement-criterion.prompt.md"
IMPLEMENT_CRITERION_DIRECT_PROMPT_FILE = (
    lib.PROMPTS_DIR / "implement-criterion-direct.prompt.md"
)
IMPLEMENT_CRITERION_DIRECT_STRATEGY_PROMPT_FILE = (
    lib.PROMPTS_DIR / "implement-criterion-direct-strategy.prompt.md"
)
IMPLEMENT_CRITERION_REFACTOR_PROMPT_FILE = (
    lib.PROMPTS_DIR / "implement-criterion-refactor.prompt.md"
)
IMPLEMENT_REFINE_PROMPT_FILE = lib.PROMPTS_DIR / "implement-refine.prompt.md"

# Pipeline bookkeeping the Implementor must never write, regardless of
# what the model decides. The named test file is deliberately NOT here -
# see the module docstring; it's guarded by the snapshot check instead.
PROTECTED_PIPELINE_PATHS = {
    str(lib.CRITERIA_STACK_FILE),
    str(lib.TICKET_FILE),
    str(lib.PLAN_FILE),
    str(lib.GAP_PLAN_FILE),
    str(lib.PIPELINE_LOG_FILE),
    str(lib.PIPELINE_CONFIG_FILE),
}


# ---------------------------------------------------------------------------
# Test-tamper guard: snapshot + byte-for-byte verification.
# Ported from the legacy pipeline's _extract_function_block /
# run_implement_for_criterion. Candidates for pipeline_lib once this
# script is accepted; kept local while it's a proposal since nothing
# else uses them.
# ---------------------------------------------------------------------------


def _extract_function_block(content: str, qualified_test_name: str) -> str | None:
    """
    Best-effort extraction of a test function's full source (signature
    through closing brace) by its short name (the last `::`-separated
    segment of qualified_test_name). Brace-counting only works for
    brace-delimited languages (Rust/TS/JS/C++/Java/Go/...); returns None
    for anything that doesn't match (e.g. Python), in which case the
    caller skips the check rather than false-failing on a language this
    can't parse.
    """
    short_name = qualified_test_name.rsplit("::", 1)[-1]
    match = re.search(
        rf"^[ \t]*.*\b{re.escape(short_name)}\s*\(", content, re.MULTILINE
    )
    if not match:
        return None
    brace_start = content.find("{", match.end())
    if brace_start == -1:
        return None
    depth = 0
    for i in range(brace_start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[match.start() : i + 1]
    return None


def verify_test_unchanged(
    test_file: str, qualified_test_name: str, original_block: str | None, criterion: str
) -> None:
    """
    Hard stop if the named test's own source changed. Skipped (with a
    warning at snapshot time, not here) when the original block couldn't
    be extracted - a language this parser can't handle, not evidence of
    tampering.
    """
    if original_block is None:
        return
    if not Path(test_file).is_file():
        lib.die_with_log(
            "implement-criterion",
            f"the test file {test_file} no longer exists after implementation - "
            f"deleting it isn't allowed.",
            criterion=criterion,
        )
    new_block = _extract_function_block(
        Path(test_file).read_text(encoding="utf-8"), qualified_test_name
    )
    if new_block is None:
        lib.die_with_log(
            "implement-criterion",
            f"the named test {qualified_test_name} could not be found in "
            f"{test_file} after implementation - it may have been removed or "
            f"renamed, which isn't allowed.",
            criterion=criterion,
        )
    if new_block != original_block:
        lib.die_with_log(
            "implement-criterion",
            f"the named test {qualified_test_name} in {test_file} was modified "
            f"during implementation, which isn't allowed - only the "
            f"surrounding production code may change.",
            criterion=criterion,
        )


def snapshot_tests(
    test_files: list[str], test_names: list[str]
) -> dict[str, str | None]:
    """
    original_block per test_name (keyed by name - TEST_WITNESS parsing
    already requires names to be unique within a frame, since
    run_scoped_test's own filter has to unambiguously target one test).
    A None value means that specific test's tamper check will be
    skipped (see verify_test_unchanged) - almost always a single entry,
    more than one only for a criterion tracking multiple tests.
    """
    snapshots: dict[str, str | None] = {}
    for test_file, test_name in zip(test_files, test_names):
        original_content = (
            Path(test_file).read_text(encoding="utf-8")
            if Path(test_file).is_file()
            else None
        )
        original_block = (
            _extract_function_block(original_content, test_name)
            if original_content is not None
            else None
        )
        if original_block is None:
            log.warning(
                "-- Could not extract %s's source from %s for the tamper check "
                "(non-brace language, or unexpected layout) - the byte-for-byte "
                "verification will be skipped for this test.",
                test_name,
                test_file,
            )
        snapshots[test_name] = original_block
    return snapshots


def verify_tests_unchanged(
    test_files: list[str],
    test_names: list[str],
    snapshots: dict[str, str | None],
    criterion: str,
) -> None:
    """Loops verify_test_unchanged over every test in the group - a fix
    attempt aimed at one still-red test is just as capable of tampering
    with (or accidentally regressing, though that's the green-check
    gate's job to catch) an already-passing sibling as a first attempt
    is, so every test gets the same protection every attempt."""
    for test_file, test_name in zip(test_files, test_names):
        verify_test_unchanged(test_file, test_name, snapshots.get(test_name), criterion)


# ---------------------------------------------------------------------------
# Prompt builders. Same shape as pipeline_lib's build_test_criterion_* pair:
# a fresh prompt for attempt 1, a fix prompt threading error output back
# for attempts 2..N.
# ---------------------------------------------------------------------------


def build_implement_criterion_prompt(
    criterion: str, plan_context: str, test_files: list[str], test_names: list[str]
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_PROMPT_FILE)
    plural = len(test_names) != 1
    test_list = "\n".join(f"- {f} :: {n}" for f, n in zip(test_files, test_names))
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"This implementation is for exactly this one acceptance "
        f"criterion, and only this one:\n\n{criterion}\n\n"
        f"The failing test{'s' if plural else ''} that prove{'' if plural else 's'} "
        f"it (must all be made to pass without modifying "
        f"{'them' if plural else 'it'}):\n{test_list}"
    )


def build_implement_criterion_fix_prompt(
    criterion: str,
    plan_context: str,
    test_files: list[str],
    test_names: list[str],
    still_red: list[str],
    changed_so_far: list[str],
    failure_kind: str,
    error_output: str,
    fresh_start: bool = False,
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_PROMPT_FILE)
    plural = len(test_names) != 1
    test_list = "\n".join(f"- {f} :: {n}" for f, n in zip(test_files, test_names))
    changed_list = "\n".join(f"- {p}" for p in changed_so_far) or "- (none recorded)"
    test_label = "the test" if len(still_red) == 1 else "these tests"
    if failure_kind == "compile":
        failure_desc = (
            "The previous attempt's code did not compile and has been reverted. "
            "Implement from scratch, taking the compile error into account - the "
            "previous approach had a compilation problem, so try a different "
            "structure."
            if fresh_start
            else "but the code does not compile. Fix the compile error with the "
            "smallest targeted change - do not re-implement from scratch or "
            "deviate from the approach already taken unless the error itself "
            "proves that approach can't work."
        )
    else:
        still_red_list = "\n".join(f"- {n}" for n in still_red)
        failure_desc = (
            (
                f"The previous attempt compiled but {test_label} still failed, "
                "and the code has been reverted. Implement from scratch, "
                "taking the test failure into account - the previous approach "
                f"produced the wrong behavior, so try a different approach.\n{still_red_list}\n\n"
            )
            if fresh_start
            else (
                f"and it compiles, but {test_label} still fail:\n{still_red_list}\n\n"
            )
        )
        failure_desc += (
            'Every test named above under "failing test(s)" must end up passing '
            "- including any not listed as still failing, which already pass and "
            "must not be broken while you fix the rest. "
            if plural
            else ""
        )
        failure_desc += (
            "Read the test output below to understand the gap between what "
            "the still-failing test(s) expect and what the implementation does, "
            "then make the smallest targeted fix. Do not weaken or modify any "
            "test to make it pass."
        )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"You already attempted an implementation for exactly this one "
        f"acceptance criterion, and only this one:\n\n{criterion}\n\n"
        f"The failing test{'s' if plural else ''} that prove{'' if plural else 's'} "
        f"it (must all be made to pass without modifying "
        f"{'them' if plural else 'it'}):\n{test_list}\n\n"
        f"Files changed in the previous attempt (read these first to see "
        f"what was tried):\n{changed_list}\n\n"
        f"{failure_desc}\n\n"
        f"Error output:\n\n```\n{error_output}\n```"
    )


def build_implement_criterion_direct_prompt(
    criterion: str, plan_context: str, strategy: str = "manual"
) -> str:
    prompt_file = (
        IMPLEMENT_CRITERION_DIRECT_PROMPT_FILE
        if strategy == "manual"
        else IMPLEMENT_CRITERION_DIRECT_STRATEGY_PROMPT_FILE
    )
    instructions = lib.load_prompt_body(prompt_file)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"This implementation is for exactly this one acceptance "
        f"criterion, and only this one:\n\n{criterion}"
    )


def build_implement_criterion_direct_fix_prompt(
    criterion: str,
    plan_context: str,
    changed_so_far: list[str],
    error_output: str,
    fresh_start: bool = False,
    strategy: str = "manual",
) -> str:
    prompt_file = (
        IMPLEMENT_CRITERION_DIRECT_PROMPT_FILE
        if strategy == "manual"
        else IMPLEMENT_CRITERION_DIRECT_STRATEGY_PROMPT_FILE
    )
    instructions = lib.load_prompt_body(prompt_file)
    changed_list = "\n".join(f"- {p}" for p in changed_so_far) or "- (none recorded)"
    failure_desc = (
        "A previous attempt failed and its changes have been reverted. "
        "You are starting from a clean state. Do NOT try to reproduce the "
        "previous approach. Read the error below, understand what went wrong, "
        "and try a different approach."
        if fresh_start
        else "but the project does not build. Fix the build error with the "
        "smallest targeted change - do not re-implement from scratch or "
        "deviate from the approach already taken unless the error itself "
        "proves that approach can't work."
    )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"You already attempted an implementation for exactly this one "
        f"acceptance criterion, and only this one:\n\n{criterion}\n\n"
        f"Files changed in the previous attempt (read these first to see "
        f"what was tried):\n{changed_list}\n\n"
        f"{failure_desc}\n\n"
        f"Error output:\n\n```\n{error_output}\n```"
    )


def build_implement_criterion_refactor_prompt(
    criterion: str, plan_context: str, test_files: list[str], test_names: list[str]
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_REFACTOR_PROMPT_FILE)
    test_list = "\n".join(f"- {f} :: {n}" for f, n in zip(test_files, test_names))
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"This refactoring is for exactly this one acceptance "
        f"criterion, and only this one:\n\n{criterion}\n\n"
        f"The safety-net test(s) that must remain GREEN "
        f"(must all stay passing without modifying "
        f"{'them' if len(test_names) != 1 else 'it'}):\n{test_list}"
    )


def build_implement_criterion_refactor_fix_prompt(
    criterion: str,
    plan_context: str,
    test_files: list[str],
    test_names: list[str],
    still_red: list[str],
    changed_so_far: list[str],
    failure_kind: str,
    error_output: str,
    fresh_start: bool = False,
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_REFACTOR_PROMPT_FILE)
    test_list = "\n".join(f"- {f} :: {n}" for f, n in zip(test_files, test_names))
    changed_list = "\n".join(f"- {p}" for p in changed_so_far) or "- (none recorded)"
    safety_label = "this test" if len(still_red) == 1 else "these safety-net tests"
    if failure_kind == "compile":
        failure_desc = (
            "The previous attempt's code did not build and has been reverted. "
            "Implement from scratch, taking the build error into account - the "
            "previous approach had a compilation problem, so try a different "
            "structure."
            if fresh_start
            else "but the project does not build. Fix the build error with the "
            "smallest targeted change - do not re-implement from scratch or "
            "deviate from the approach already taken unless the error itself "
            "proves that approach can't work."
        )
    else:
        still_red_list = "\n".join(f"- {n}" for n in still_red)
        safety_quote = "safety-net test(s)"
        extra = (
            f'Every test named above under "{safety_quote}" must end up '
            f"green - including any not listed as still red, which were "
            f"already green and must not be broken again while you fix the "
            f"rest. "
            if len(test_names) != 1
            else ""
        )
        failure_desc = (
            (
                f"The previous attempt built but {safety_label} broke, and the "
                "code has been reverted. Implement from scratch, taking the "
                "test failure into account - the previous approach produced the "
                f"wrong behavior, so try a different approach:\n{still_red_list}\n\n"
            )
            if fresh_start
            else (
                f"and it builds, but your refactor broke {safety_label} "
                "(they were GREEN at baseline and must be GREEN again):"
                f"\n{still_red_list}\n\n"
            )
        )
        failure_desc += (
            f"{extra}"
            "Read the test output below to understand what behavior regressed, "
            "then make the smallest targeted fix that restores the test(s) to "
            "GREEN. Do not modify any named test to make it pass - the tests "
            "are the safety net, not the target."
        )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"You already attempted a refactor for exactly this one acceptance "
        f"criterion, and only this one:\n\n{criterion}\n\n"
        f"The safety-net test(s) that must remain GREEN "
        f"(must all stay passing without modifying "
        f""
        f"{'them' if len(test_names) != 1 else 'it'}):\n{test_list}\n\n"
        f"Files changed in the previous attempt (read these first to see "
        f"what was tried):\n{changed_list}\n\n"
        f"{failure_desc}\n\n"
        f"Error output:\n\n```\n{error_output}\n```"
    )


def build_implement_feedback_prompt(
    frame: "lib.CriterionFrame",
    feedback: str,
    previous_changed_files: list[str],
    verification: str = "test",
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_REFINE_PROMPT_FILE)
    changed_list = (
        "\n".join(f"- {p}" for p in previous_changed_files) or "- (none recorded)"
    )
    test_block = ""
    if frame.test_files and frame.test_names:
        label = (
            "Safety-net tests" if verification == "refactor" else "Tests to preserve"
        )
        test_block = f"\n\n{label}:\n" + "\n".join(
            f"- {f} :: {n}" for f, n in zip(frame.test_files, frame.test_names)
        )
    mode_note = (
        "This is a refactor retry: preserve behavior, keep the named tests GREEN, "
        "and address only the structural problem called out in the feedback."
        if verification == "refactor"
        else "This is an implementation retry: preserve the criterion and make the "
        "smallest targeted correction the feedback asks for."
    )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this criterion:\n\n"
        f"{frame.plan_context}\n\n"
        f"Acceptance criterion (fixed; do not rewrite it):\n\n{frame.criterion}\n\n"
        f"{mode_note}{test_block}\n\n"
        f"Files changed in the previous attempt (read these first to see what was tried):\n"
        f"{changed_list}\n\n"
        f"User feedback to address:\n\n{feedback}"
    )


# ---------------------------------------------------------------------------
# The implement loop.
# ---------------------------------------------------------------------------


def run_implement_direct_with_refine(
    frame: "lib.CriterionFrame",
    model: str,
    commands: dict,
    max_attempts: int,
    feedback: str | None = None,
    previous_changed_files: list[str] | None = None,
    retry_policy: RetryPolicy | None = None,
    allow_compile: bool = False,
    compile_cmd: str | None = None,
    reset_on_retry: bool = False,
    test_commit_sha: str | None = None,
) -> list[str]:
    """
    Level 2: direct implementation for a single criterion - no named
    test, so no tamper guard and no scoped-test-green gate, just a
    build-gate retry loop sharing the same one-budget-total shape as
    run_implement_with_refine. Returns the deduplicated list of changed
    files once the build passes; dies via die_with_log on exhausted
    attempts or an AI failure. Does not judge whether the criterion is
    actually satisfied - that's next_step's do_manual_criterion, unchanged,
    run on the next 'next_step' call.
    """
    from .retry import FixedBudgetPolicy

    policy = retry_policy or FixedBudgetPolicy(max_attempts)
    limit_desc = policy.describe_limit()

    all_changed: list[str] = []
    last_error: str | None = None
    last_result: subprocess.CompletedProcess | None = None

    attempt = 0
    while True:
        attempt += 1
        if attempt == 1:
            if feedback:
                prompt = build_implement_feedback_prompt(
                    frame, feedback, previous_changed_files or [], verification="manual"
                )
            else:
                prompt = build_implement_criterion_direct_prompt(
                    frame.criterion, frame.plan_context, strategy=frame.strategy
                )
        else:
            prev_changed = sorted(set(all_changed))
            fresh_start = False
            if reset_on_retry and test_commit_sha:
                try:
                    lib.git_reset_hard(test_commit_sha)
                    fresh_start = True
                    log.info(
                        "-- Fresh-start retry: reset to test commit %s",
                        test_commit_sha[:8],
                    )
                except lib.GitError as e:
                    lib.die_with_log(
                        "implement-criterion-direct",
                        f"git reset --hard {test_commit_sha} failed: {e}",
                        criterion=frame.criterion,
                    )
                all_changed = []
            log.warning(
                "-- Build failed (attempt %d, %s). Feeding the error back to "
                "Direct Implementor to fix.",
                attempt - 1,
                limit_desc,
            )
            prompt = build_implement_criterion_direct_fix_prompt(
                frame.criterion,
                frame.plan_context,
                prev_changed,
                last_error,
                fresh_start=fresh_start,
                strategy=frame.strategy,
            )

        attempt_changed: list[str] = []

        def attempt_step():
            attempt_changed.clear()
            return run_with_tools(
                prompt,
                (
                    tools.READ_WRITE_TOOLS_WITH_COMPILE
                    if allow_compile
                    else tools.READ_WRITE_TOOLS
                ),
                tools.make_executor(
                    written_paths=attempt_changed,
                    protected_paths=PROTECTED_PIPELINE_PATHS,
                    allow_compile=allow_compile,
                    compile_cmd=compile_cmd,
                ),
                "implement-criterion-direct",
                model=model,
                summarize_call=tools.summarize_tool_call,
            )

        try:
            result = lib.run_ai_step_with_retry(
                attempt_step, "implement-criterion-direct", criterion=frame.criterion
            )
        except (AIError, tools.PipelineAbort) as e:
            lib.die_with_log(
                "implement-criterion-direct", str(e), criterion=frame.criterion
            )
        lib.render_step_output(result.text)
        if not attempt_changed:
            paths = lib.extract_referenced_paths(
                f"{frame.criterion}\n{frame.plan_context}"
            )
            if paths and set(paths) & set(lib.git_changed_files()):
                return sorted(
                    set(all_changed)
                )  # a previous criterion already did the work
            lib.die_with_log(
                "implement-criterion-direct",
                "Direct Implementor finished without writing any files.",
                criterion=frame.criterion,
            )
        all_changed.extend(attempt_changed)

        build_result = lib.run_command(
            commands["build_cmd"], f"build gate (attempt {attempt}, {limit_desc})"
        )
        if build_result.returncode == 0:
            return sorted(set(all_changed))

        last_error = (build_result.stdout or "") + (build_result.stderr or "")
        last_result = build_result
        lib.log_event(
            "implement-criterion-direct",
            "retry",
            error=f"build failed (attempt {attempt}, {limit_desc})",
            criterion=frame.criterion,
        )
        if not policy.should_continue(attempt, "compile", frame, None):
            policy.on_exhausted(
                "compile",
                last_error or "",
                sorted(set(all_changed)),
                last_result,
                frame,
                None,
            )
            return sorted(set(all_changed))


def run_implement_with_refine(
    frame: "lib.CriterionFrame",
    model: str,
    commands: dict,
    max_attempts: int,
    verification: str = "test",
    feedback: str | None = None,
    previous_changed_files: list[str] | None = None,
    retry_policy: RetryPolicy | None = None,
    allow_compile: bool = False,
    compile_cmd: str | None = None,
    reset_on_retry: bool = False,
    test_commit_sha: str | None = None,
) -> list[str]:
    """
    Implement the frame's criterion against its named failing test(s),
    gated on build + every test green, feeding failures back to the
    Implementor for a fix attempt - up to max_attempts attempts *total*
    (the initial implement plus every refine counts against one budget).
    Returns the deduplicated list of changed files on success; dies via
    die_with_log on exhausted attempts, a tampered test, or an AI
    failure, leaving the stack untouched in every case. Almost always
    one test; more than one only when the criterion tracks a genuinely
    separate group (see test-criterion.prompt.md's Step 3) - every gate
    below applies to the whole group, not just whichever test(s) started
    red, since a fix aimed at one could otherwise silently regress an
    already-passing sibling with nothing to catch it.

    `verification` selects the prompt family: "test" (the default) uses
    the regular Implementor prompts framed around making a red test pass;
    "refactor" uses the Refactor Implementor prompts framed around
    keeping an already-green safety net green while restructuring
    production code. The loop structure (tamper guard, build gate, green
    check, refine) is identical either way - the only thing that differs
    is how each attempt's prompt is worded.
    """
    test_files, test_names = frame.test_files, frame.test_names
    snapshots = snapshot_tests(test_files, test_names)

    from .retry import FixedBudgetPolicy

    policy = retry_policy or FixedBudgetPolicy(max_attempts)
    limit_desc = policy.describe_limit()

    all_changed: list[str] = []
    failure_kind: str | None = None
    last_error: str | None = None
    last_result: subprocess.CompletedProcess | None = None
    # For a refactor frame the safety-net tests are GREEN at baseline, so
    # nothing is "still red" before the first attempt; the initial value
    # only matters to the fix prompt, which recomputes it after each
    # attempt's green check anyway.
    still_red: list[str] = [] if verification == "refactor" else list(test_names)

    attempt = 0
    while True:
        attempt += 1
        if attempt == 1:
            if feedback:
                prompt = build_implement_feedback_prompt(
                    frame,
                    feedback,
                    previous_changed_files or [],
                    verification=verification,
                )
            elif verification == "refactor":
                prompt = build_implement_criterion_refactor_prompt(
                    frame.criterion, frame.plan_context, test_files, test_names
                )
            else:
                prompt = build_implement_criterion_prompt(
                    frame.criterion, frame.plan_context, test_files, test_names
                )
        else:
            prev_changed = sorted(set(all_changed))
            fresh_start = False
            if reset_on_retry and test_commit_sha:
                try:
                    lib.git_reset_hard(test_commit_sha)
                    fresh_start = True
                    log.info(
                        "-- Fresh-start retry: reset to test commit %s",
                        test_commit_sha[:8],
                    )
                except lib.GitError as e:
                    lib.die_with_log(
                        "implement-criterion",
                        f"git reset --hard {test_commit_sha} failed: {e}",
                        criterion=frame.criterion,
                    )
                all_changed = []
                still_red = list(test_names)
                snapshots = snapshot_tests(test_files, test_names)
            log.warning(
                "-- %s failed (attempt %d, %s). Feeding the error back to Implementor to fix.",
                "Build" if failure_kind == "compile" else "Green check",
                attempt - 1,
                limit_desc,
            )
            if verification == "refactor":
                prompt = build_implement_criterion_refactor_fix_prompt(
                    frame.criterion,
                    frame.plan_context,
                    test_files,
                    test_names,
                    still_red,
                    prev_changed,
                    failure_kind,
                    last_error,
                    fresh_start=fresh_start,
                )
            else:
                prompt = build_implement_criterion_fix_prompt(
                    frame.criterion,
                    frame.plan_context,
                    test_files,
                    test_names,
                    still_red,
                    prev_changed,
                    failure_kind,
                    last_error,
                    fresh_start=fresh_start,
                )

        attempt_changed: list[str] = []

        def attempt_step():
            attempt_changed.clear()
            return run_with_tools(
                prompt,
                (
                    tools.READ_WRITE_TOOLS_WITH_COMPILE
                    if allow_compile
                    else tools.READ_WRITE_TOOLS
                ),
                tools.make_executor(
                    written_paths=attempt_changed,
                    protected_paths=PROTECTED_PIPELINE_PATHS,
                    allow_compile=allow_compile,
                    compile_cmd=compile_cmd,
                ),
                "implement-criterion",
                model=model,
                summarize_call=tools.summarize_tool_call,
            )

        try:
            result = lib.run_ai_step_with_retry(
                attempt_step, "implement-criterion", criterion=frame.criterion
            )
        except (AIError, tools.PipelineAbort) as e:
            lib.die_with_log("implement-criterion", str(e), criterion=frame.criterion)
        lib.render_step_output(result.text)
        if not attempt_changed:
            paths = lib.extract_referenced_paths(
                f"{frame.criterion}\n{frame.plan_context}"
            )
            if paths and set(paths) & set(lib.git_changed_files()):
                return sorted(
                    set(all_changed)
                )  # a previous criterion already did the work
            lib.die_with_log(
                "implement-criterion",
                "Implementor finished without writing any files.",
                criterion=frame.criterion,
            )

        all_changed.extend(attempt_changed)

        # Tamper check after EVERY attempt, over every test in the group
        # - a refine attempt aimed at one test is just as capable of
        # "fixing" a sibling test as a first attempt is.
        verify_tests_unchanged(test_files, test_names, snapshots, frame.criterion)

        build_result = lib.run_command(
            commands["build_cmd"], f"build gate (attempt {attempt}, {limit_desc})"
        )
        if build_result.returncode != 0:
            failure_kind = "compile"
            last_error = (build_result.stdout or "") + (build_result.stderr or "")
            last_result = build_result
            lib.log_event(
                "implement-criterion",
                "retry",
                error=f"build failed (attempt {attempt}, {limit_desc})",
                criterion=frame.criterion,
            )
            if not policy.should_continue(attempt, failure_kind, frame, None):
                policy.on_exhausted(
                    failure_kind,
                    last_error or "",
                    sorted(set(all_changed)),
                    last_result,
                    frame,
                    None,
                )
                return sorted(set(all_changed))
            continue

        green_results = lib.run_scoped_tests(
            test_names, commands, f"green check (attempt {attempt}, {limit_desc})"
        )
        still_red = [n for n, r in zip(test_names, green_results) if r.returncode != 0]
        if not still_red:
            return sorted(set(all_changed))

        failure_kind = "test-red"
        last_error = "\n\n".join(
            f"{n}:\n" + (r.stdout or "") + (r.stderr or "")
            for n, r in zip(test_names, green_results)
            if r.returncode != 0
        )
        last_result = next(
            r for n, r in zip(test_names, green_results) if n in still_red
        )
        lib.log_event(
            "implement-criterion",
            "retry",
            error=f"{len(still_red)} test(s) still red (attempt {attempt}, {limit_desc})",
            criterion=frame.criterion,
        )
        if not policy.should_continue(attempt, failure_kind, frame, None):
            policy.on_exhausted(
                failure_kind,
                last_error or "",
                sorted(set(all_changed)),
                last_result,
                frame,
                None,
            )
            return sorted(set(all_changed))
