#!/usr/bin/env python3
"""
implement_step - AI-implement the criteria stack's top frame: make its
one named failing test pass, without touching the stack. The optional
third gesture of the criteria-stack pipeline, slotting into the pause
point next_step deliberately leaves open:

    push_ticket <id>   seed the stack
    next_step          write the failing test, pause at AWAIT_IMPL
    implement_step     (this) turn that red test green
    next_step          re-detect green, pop, continue

Contract: this script's only postcondition is "the top frame's scoped
test passes". It NEVER writes .criteria-stack.json - next_step remains
the sole owner of phase transitions, and re-detects the green test via
its existing status=="test-written" phase check on the very next run.
That single-owner rule is also what makes failure here safe: if the
Implementor exhausts its attempts, the frame is still test-written, the
test is still red, and next_step drops back into AWAIT_IMPL exactly as
if this script had never run - the human implementation path is the
untouched fallback, not a mode this script replaces.

Guard-first (same principle as push_ticket): every precondition is
re-checked from real state before any AI call spends money -
  stack empty                          -> nothing to implement, exit 1
  top frame status != "test-written",
    or missing test_file/test_name     -> run next_step first, exit 1
  scoped test re-run and green         -> nothing to do, run next_step
                                          to pop it, exit 0
  scoped test red                      -> proceed

Implement loop (bounded self-correction, mirroring pipeline_lib's
run_test_for_criterion_with_compile_retry - the legacy resolve-ticket
pipeline was single-shot here, "no second attempt"; this trades that
fail-fast behaviour for the same bounded refine loop the Tester side
already has):

  attempt 1:  fresh implement prompt (frame's own plan_context, not the
              whole gap plan - frames carry their context since push
              time, so the Implementor sees exactly what the Tester saw)
  gate:       build_cmd                -> on failure, feed compile error
                                          back as a fix prompt
  gate:       scoped test green check  -> on failure, feed test output
                                          back as a fix prompt
  attempts 2..N: fix prompt (initial write + every refine share one
              budget of --max-attempts total, not N retries on top)

After EVERY attempt, the named test function is verified byte-for-byte
unchanged against a snapshot taken before the first attempt (brace-
counting extraction, ported from the legacy pipeline). protected_paths
can't do this job: when the test lives inline with the production code
it covers (Rust #[cfg(test)] mod tests - this pipeline's own Tester
default), the test file IS a file the Implementor must legitimately
edit; blocking it entirely makes the task impossible, not safe. The
snapshot check permits the surrounding edits while making test
tampering a hard, mechanical failure. Pipeline bookkeeping files
(.criteria-stack.json and the scratch files) ARE fully write-protected
via protected_paths - the Implementor has no legitimate reason to
touch those.

Level 2 - direct implementation (verification="manual" frames only):
criteria narrow-plan.prompt.md tagged manual (documentation, config, CI
- no meaningful red/green) have no named test to target, so there is
nothing to gate the build-only retry loop on except the build itself.
This mode never touches the stack either, same single-owner rule as
Level 1: next_step's do_manual_criterion is still the sole judge of
whether the criterion is actually satisfied (its own mechanical floor -
did a file this criterion names actually change - or --accept-manual),
run on the very next 'next_step' call same as if a human had made the
change by hand.

Level 3 - refactor implementation (verification="refactor" frames
only): criteria narrow-plan.prompt.md tagged refactor (structural
changes to production code that preserve behavior, with existing tests
as the safety net) have a named test that's already GREEN at baseline,
not RED. This level reuses the Level 1 implement loop (tamper guard,
build gate, green check, refine) but with a refactor-framed prompt
that tells the Implementor to keep the safety-net tests GREEN while
restructuring, not to make a red test pass. Same single-owner rule: this
never touches the stack - next_step's recheck_refactor_tests is still
the sole judge of whether the criterion is actually satisfied (safety-
net tests still GREEN *and* a production file actually changed), run on
the very next 'next_step' call. A pre-refactor green check refuses to
start if any safety-net test is RED (the safety net must hold before
*and* after the refactor).

verification="test-refactor" frames are refused here: there is no
production code to implement for a test-refactoring criterion (the
test-writer rewrites an existing test, expected GREEN). A test-refactor
frame that reached test-written (RED rewrite) is an incorrect rewrite
the human must fix by hand, not work for this script.

Exit codes: 0 when the scoped test is green (whether this run made it
green or found it already green); non-zero on exhausted attempts, a
tampered test, or any genuine pipeline failure. Composable from shell:

    implement_step && next_step --continuous

Usage:
    implement_step [--model <model-id>] [--config <path>]
                   [--max-attempts <n>] [--log-level <level>]
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from .lib import ai_client, pipeline_lib as lib, render, tools, verbosity
from .lib.ai_client import AIError, run_with_tools

log = verbosity.get_logger(__name__)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"
DEFAULT_MAX_ATTEMPTS = 3

IMPLEMENT_CRITERION_PROMPT_FILE = lib.PROMPTS_DIR / "implement-criterion.prompt.md"
IMPLEMENT_CRITERION_DIRECT_PROMPT_FILE = lib.PROMPTS_DIR / "implement-criterion-direct.prompt.md"
IMPLEMENT_CRITERION_REFACTOR_PROMPT_FILE = lib.PROMPTS_DIR / "implement-criterion-refactor.prompt.md"
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
    match = re.search(rf"^[ \t]*.*\b{re.escape(short_name)}\s*\(", content, re.MULTILINE)
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
                return content[match.start():i + 1]
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


def snapshot_tests(test_files: list[str], test_names: list[str]) -> dict[str, str | None]:
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
            Path(test_file).read_text(encoding="utf-8") if Path(test_file).is_file() else None
        )
        original_block = (
            _extract_function_block(original_content, test_name)
            if original_content is not None else None
        )
        if original_block is None:
            log.warning(
                "-- Could not extract %s's source from %s for the tamper check "
                "(non-brace language, or unexpected layout) - the byte-for-byte "
                "verification will be skipped for this test.",
                test_name, test_file,
            )
        snapshots[test_name] = original_block
    return snapshots


def verify_tests_unchanged(
    test_files: list[str], test_names: list[str], snapshots: dict[str, str | None], criterion: str
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
        f"The failing test{'s' if plural else ''} that prove{'' if plural else 's'} it "
        f"(must all be made to pass without modifying {'them' if plural else 'it'}):\n{test_list}"
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
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_PROMPT_FILE)
    plural = len(test_names) != 1
    test_list = "\n".join(f"- {f} :: {n}" for f, n in zip(test_files, test_names))
    changed_list = "\n".join(f"- {p}" for p in changed_so_far) or "- (none recorded)"
    if failure_kind == "compile":
        failure_desc = (
            "but the code does not compile. Fix the compile error with the "
            "smallest targeted change - do not re-implement from scratch or "
            "deviate from the approach already taken unless the error itself "
            "proves that approach can't work."
        )
    else:
        still_red_list = "\n".join(f"- {n}" for n in still_red)
        failure_desc = (
            f"and it compiles, but {'the test' if len(still_red) == 1 else 'these tests'} "
            f"still fail:\n{still_red_list}\n\n"
            + (
                "Every test named above under \"failing test(s)\" must end up passing "
                "- including any not listed as still failing, which already pass and "
                "must not be broken while you fix the rest. "
                if plural else ""
            )
            + "Read the test output below to understand the gap between what "
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
        f"The failing test{'s' if plural else ''} that prove{'' if plural else 's'} it "
        f"(must all be made to pass without modifying {'them' if plural else 'it'}):\n{test_list}\n\n"
        f"Files changed in the previous attempt (read these first to see "
        f"what was tried):\n{changed_list}\n\n"
        f"{failure_desc}\n\n"
        f"Error output:\n\n```\n{error_output}\n```"
    )


def build_implement_criterion_direct_prompt(criterion: str, plan_context: str) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_DIRECT_PROMPT_FILE)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"This implementation is for exactly this one acceptance "
        f"criterion, and only this one:\n\n{criterion}"
    )


def build_implement_criterion_direct_fix_prompt(
    criterion: str, plan_context: str, changed_so_far: list[str], error_output: str
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_DIRECT_PROMPT_FILE)
    changed_list = "\n".join(f"- {p}" for p in changed_so_far) or "- (none recorded)"
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"You already attempted an implementation for exactly this one "
        f"acceptance criterion, and only this one:\n\n{criterion}\n\n"
        f"Files changed in the previous attempt (read these first to see "
        f"what was tried):\n{changed_list}\n\n"
        f"but the project does not build. Fix the build error with the "
        f"smallest targeted change - do not re-implement from scratch or "
        f"deviate from the approach already taken unless the error itself "
        f"proves that approach can't work.\n\n"
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
        f"(must all stay passing without modifying {'them' if len(test_names) != 1 else 'it'}):\n{test_list}"
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
) -> str:
    instructions = lib.load_prompt_body(IMPLEMENT_CRITERION_REFACTOR_PROMPT_FILE)
    test_list = "\n".join(f"- {f} :: {n}" for f, n in zip(test_files, test_names))
    changed_list = "\n".join(f"- {p}" for p in changed_so_far) or "- (none recorded)"
    if failure_kind == "compile":
        failure_desc = (
            "but the project does not build. Fix the build error with the "
            "smallest targeted change - do not re-implement from scratch or "
            "deviate from the approach already taken unless the error itself "
            "proves that approach can't work."
        )
    else:
        still_red_list = "\n".join(f"- {n}" for n in still_red)
        safety_quote = 'safety-net test(s)'
        extra = (
            f"Every test named above under \"{safety_quote}\" must end up "
            f"green - including any not listed as still red, which were "
            f"already green and must not be broken again while you fix the "
            f"rest. "
            if len(test_names) != 1 else ""
        )
        failure_desc = (
            f"and it builds, but your refactor broke "
            f"{'this test' if len(still_red) == 1 else 'these safety-net tests'} "
            f"(they were GREEN at baseline and must be GREEN again):\n{still_red_list}\n\n"
            f"{extra}"
            f"Read the test output below to understand what behavior "
            f"regressed, then make the smallest targeted fix that restores "
            f"the test(s) to GREEN. Do not modify any named test to make it "
            f"pass - the tests are the safety net, not the target."
        )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"You already attempted a refactor for exactly this one acceptance "
        f"criterion, and only this one:\n\n{criterion}\n\n"
        f"The safety-net test(s) that must remain GREEN "
        f"(must all stay passing without modifying {'them' if len(test_names) != 1 else 'it'}):\n{test_list}\n\n"
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
    changed_list = "\n".join(f"- {p}" for p in previous_changed_files) or "- (none recorded)"
    test_block = ""
    if frame.test_files and frame.test_names:
        label = "Safety-net tests" if verification == "refactor" else "Tests to preserve"
        test_block = (
            f"\n\n{label}:\n"
            + "\n".join(f"- {f} :: {n}" for f, n in zip(frame.test_files, frame.test_names))
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
) -> list[str]:
    """
    Level 2: direct implementation for a verification="manual" frame -
    no named test, so no tamper guard and no scoped-test-green gate, just
    a build-gate retry loop sharing the same one-budget-total shape as
    run_implement_with_refine. Returns the deduplicated list of changed
    files once the build passes; dies via die_with_log on exhausted
    attempts or an AI failure. Does not judge whether the criterion is
    actually satisfied - that's next_step's do_manual_criterion, unchanged,
    run on the next 'next_step' call.
    """
    all_changed: list[str] = []
    last_error: str | None = None
    last_result: subprocess.CompletedProcess | None = None

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            if feedback:
                prompt = build_implement_feedback_prompt(
                    frame, feedback, previous_changed_files or [], verification="manual"
                )
            else:
                prompt = build_implement_criterion_direct_prompt(frame.criterion, frame.plan_context)
        else:
            log.warning(
                "-- Build failed (attempt %d/%d). Feeding the error back to Direct Implementor to fix.",
                attempt - 1, max_attempts,
            )
            prompt = build_implement_criterion_direct_fix_prompt(
                frame.criterion, frame.plan_context, sorted(set(all_changed)), last_error,
            )

        attempt_changed: list[str] = []

        def attempt_step():
            attempt_changed.clear()
            return run_with_tools(
                prompt,
                tools.READ_WRITE_TOOLS,
                tools.make_executor(
                    written_paths=attempt_changed,
                    protected_paths=PROTECTED_PIPELINE_PATHS,
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
            lib.die_with_log("implement-criterion-direct", str(e), criterion=frame.criterion)
        lib.render_step_output(result.text)
        if not attempt_changed:
            lib.die_with_log(
                "implement-criterion-direct",
                "Direct Implementor finished without writing any files.",
                criterion=frame.criterion,
            )
        all_changed.extend(attempt_changed)

        build_result = lib.run_command(
            commands["build_cmd"], f"build gate (attempt {attempt}/{max_attempts})"
        )
        if build_result.returncode == 0:
            return sorted(set(all_changed))

        last_error = (build_result.stdout or "") + (build_result.stderr or "")
        last_result = build_result
        lib.log_event(
            "implement-criterion-direct", "retry",
            error=f"build failed (attempt {attempt}/{max_attempts})",
            criterion=frame.criterion,
        )

    exit_code = last_result.returncode if last_result is not None else "unknown"
    lib.die_with_log(
        "implement-criterion-direct",
        f"Code does not build after {max_attempts} attempt(s) (exit {exit_code}). See "
        f"output above. The frame is untouched - run 'next_step' again (perhaps "
        f"with a different --model), or make the change by hand and then re-run "
        f"'next_step'.",
        criterion=frame.criterion,
    )


def run_implement_with_refine(
    frame: "lib.CriterionFrame",
    model: str,
    commands: dict,
    max_attempts: int,
    verification: str = "test",
    feedback: str | None = None,
    previous_changed_files: list[str] | None = None,
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

    all_changed: list[str] = []
    failure_kind: str | None = None
    last_error: str | None = None
    last_result: subprocess.CompletedProcess | None = None
    # For a refactor frame the safety-net tests are GREEN at baseline, so
    # nothing is "still red" before the first attempt; the initial value
    # only matters to the fix prompt, which recomputes it after each
    # attempt's green check anyway.
    still_red: list[str] = [] if verification == "refactor" else list(test_names)

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            if feedback:
                prompt = build_implement_feedback_prompt(
                    frame, feedback, previous_changed_files or [], verification=verification
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
            log.warning(
                "-- %s failed (attempt %d/%d). Feeding the error back to Implementor to fix.",
                "Build" if failure_kind == "compile" else "Green check",
                attempt - 1, max_attempts,
            )
            if verification == "refactor":
                prompt = build_implement_criterion_refactor_fix_prompt(
                    frame.criterion, frame.plan_context, test_files, test_names, still_red,
                    sorted(set(all_changed)), failure_kind, last_error,
                )
            else:
                prompt = build_implement_criterion_fix_prompt(
                    frame.criterion, frame.plan_context, test_files, test_names, still_red,
                    sorted(set(all_changed)), failure_kind, last_error,
                )

        attempt_changed: list[str] = []

        def attempt_step():
            attempt_changed.clear()
            return run_with_tools(
                prompt,
                tools.READ_WRITE_TOOLS,
                tools.make_executor(
                    written_paths=attempt_changed,
                    protected_paths=PROTECTED_PIPELINE_PATHS,
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
            commands["build_cmd"], f"build gate (attempt {attempt}/{max_attempts})"
        )
        if build_result.returncode != 0:
            failure_kind = "compile"
            last_error = (build_result.stdout or "") + (build_result.stderr or "")
            last_result = build_result
            lib.log_event(
                "implement-criterion", "retry",
                error=f"build failed (attempt {attempt}/{max_attempts})",
                criterion=frame.criterion,
            )
            continue

        green_results = lib.run_scoped_tests(
            test_names, commands, f"green check (attempt {attempt}/{max_attempts})"
        )
        still_red = [n for n, r in zip(test_names, green_results) if r.returncode != 0]
        if not still_red:
            return sorted(set(all_changed))

        failure_kind = "test-red"
        last_error = "\n\n".join(
            f"{n}:\n" + (r.stdout or "") + (r.stderr or "")
            for n, r in zip(test_names, green_results) if r.returncode != 0
        )
        last_result = next(r for n, r in zip(test_names, green_results) if n in still_red)
        lib.log_event(
            "implement-criterion", "retry",
            error=f"{len(still_red)} test(s) still red (attempt {attempt}/{max_attempts})",
            criterion=frame.criterion,
        )

    exit_code = last_result.returncode if last_result is not None else "unknown"
    what = "Code does not compile" if failure_kind == "compile" else f"{len(still_red)} test(s) still fail"
    if verification == "refactor":
        tail = (
            " See output above. The frame is untouched - the safety-net test(s) "
            "were broken by the refactor and 'next_step' still pauses at "
            "baseline-confirmed, so you can fix the refactor by hand (or run "
            "'next_step' again, perhaps with a different --model)."
        )
    else:
        tail = (
            ". See output above. The frame is untouched - the test(s) are "
            "still red and 'next_step' still reports AWAIT_IMPL, so you can "
            "implement by hand (or run 'next_step' again, perhaps with a "
            "different --model)."
        )
    lib.die_with_log(
        "implement-criterion",
        f"{what} after {max_attempts} attempt(s) (exit {exit_code}){tail}",
        criterion=frame.criterion,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI-implement the top frame's criterion: make its named "
                     "failing test pass without modifying it. Never touches "
                     "the stack - run 'next_step' afterward to pop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"opencode zen model ID to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--config",
        default=str(lib.PIPELINE_CONFIG_FILE),
        help=f"Path to the build/test command config (default: {lib.PIPELINE_CONFIG_FILE}).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"Total Implementor attempts, initial write + refines sharing "
             f"one budget (default: {DEFAULT_MAX_ATTEMPTS}).",
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

    commands = lib.load_pipeline_config(Path(args.config))

    # ── Guard: re-check every precondition from real state ─────────────────
    stack = lib.load_stack()
    if not stack:
        render.print_line("-- Stack is empty. Nothing to implement. Run 'push_ticket <id>' first.")
        sys.exit(1)

    frame = stack[0]
    log.info(
        "-- implement_step: ticket=%s status=%s verification=%s criterion=%s",
        frame.ticket, frame.status, frame.verification, frame.criterion,
    )

    # ── Level 2: manual-verification frames have no target test, so they
    # branch out here before Level 1's test-written guard even applies.
    # "pending"/"awaiting-manual-impl" mirror next_step.py's own pending
    # and MANUAL_PENDING_STATUS values - duplicated as literals rather
    # than imported, same as this script already does for "test-written".
    if frame.verification == "manual":
        if frame.status not in ("pending", "awaiting-manual-impl"):
            render.print_line(
                f"-- Top frame is a manual-verification criterion but its status "
                f"({frame.status!r}) isn't awaiting implementation. Run 'next_step' first."
            )
            sys.exit(1)

        render.print_line()
        render.print_line("-- Implementing directly (verification=manual, no target test):")
        render.print_line(f"   Criterion: {frame.criterion}")

        changed_files = run_implement_direct_with_refine(
            frame, args.model, commands, args.max_attempts
        )

        render.print_line()
        render.print_line(f"-- Implemented: {frame.criterion}")
        render.print_line(f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}")
        render.print_line(
            "-- Run 'next_step' to check whether this satisfies the criterion and continue."
        )
        render.print_line(f"-- Token usage: {ai_client.usage}")
        return

    # Level 3: refactor-verification frames. The safety-net test(s)
    # (named in existing_test_refs, mirrored into test_files/test_names
    # by next_step's do_refactor_setup) are already GREEN at baseline -
    # this level restructures the production code while keeping them
    # GREEN, rather than making a red test pass. Pre-conditions mirror
    # next_step.py's refactor dispatch: status must be baseline-confirmed
    # (do_refactor_setup ran the baseline check), and the safety-net
    # tests must currently be GREEN (not RED like Level 1).
    if frame.verification == "refactor":
        if frame.status != lib.BASELINE_CONFIRMED_STATUS:
            render.print_line(
                "-- Top frame is a refactor criterion but its status "
                + repr(frame.status)
                + " is not awaiting implementation. Run 'next_step' first "
                "to establish the baseline."
            )
            sys.exit(1)
        if not frame.test_files or not frame.test_names:
            render.print_line(
                "-- Refactor frame is baseline-confirmed but has no "
                "safety-net test(s) recorded. Run 'next_step' to re-run "
                "refactor setup."
            )
            sys.exit(1)

        green_results = lib.run_scoped_tests(
            frame.test_names, commands, "pre-refactor green check"
        )
        red_names = [n for n, r in zip(frame.test_names, green_results) if r.returncode != 0]
        if red_names:
            render.print_line(
                "-- Safety-net test(s) are RED - the refactor cannot proceed "
                "until they are GREEN again (the safety net must hold before "
                "and after the refactor). Fix them first, then re-run."
            )
            for n in red_names:
                render.print_line("   RED: " + n)
            sys.exit(1)

        render.print_line()
        render.print_line("-- Refactoring (keeping safety-net tests GREEN):")
        for f, n in zip(frame.test_files, frame.test_names):
            render.print_line("   " + f + " :: " + n)
        render.print_line("   Criterion: " + frame.criterion)

        changed_files = run_implement_with_refine(
            frame, args.model, commands, args.max_attempts,
            verification=frame.verification,
        )

        render.print_line()
        render.print_line("-- Refactored: " + frame.criterion)
        render.print_line(
            "   All " + str(len(frame.test_names)) + " safety-net test(s) still GREEN:"
        )
        for f, n in zip(frame.test_files, frame.test_names):
            render.print_line("     " + f + " :: " + n)
        render.print_line(
            "   Files changed (" + str(len(changed_files)) + "): " + ", ".join(changed_files)
        )
        render.print_line("-- Run 'next_step' to re-check and pop this criterion.")
        render.print_line(f"-- Token usage: {ai_client.usage}")
        return

    # Refuse test-refactor frames: there is no production code to
    # implement for a test-refactoring criterion. If the test-writer's
    # rewrite came back RED (status="test-written"), the rewrite itself
    # is incorrect - the human must fix the test by hand and re-run
    # 'next_step', which rechecks and pops once it is GREEN.
    if frame.verification == "test-refactor" and frame.status == "test-written":
        render.print_line(
            "-- This is a test-refactor criterion whose rewrite came back RED."
        )
        render.print_line(
            "   There is no production code to implement - the rewrite itself"
        )
        render.print_line(
            "   is incorrect. Fix the test by hand (keep its assertions"
        )
        render.print_line(
            "   functionally identical; change only the structural elements"
        )
        render.print_line(
            "   the criterion describes), then run 'next_step' to re-check."
        )
        sys.exit(1)

    if frame.status != "test-written" or not frame.test_files or not frame.test_names:
        render.print_line(
            f"-- Top frame is not awaiting implementation (status: {frame.status}"
            f"{'' if frame.test_files else ', no test recorded'}). "
            f"Run 'next_step' to advance it to AWAIT_IMPL first."
        )
        sys.exit(1)

    red_results = lib.run_scoped_tests(frame.test_names, commands, "pre-implement red check")
    still_red = [n for n, r in zip(frame.test_names, red_results) if r.returncode != 0]
    if not still_red:
        render.print_line(
            f"-- All {len(frame.test_names)} test(s) already green. "
            f"Nothing to implement. Run 'next_step' to pop this criterion."
        )
        sys.exit(0)

    # ── Implement ───────────────────────────────────────────────────────────
    render.print_line()
    if len(frame.test_names) == 1:
        render.print_line("-- Implementing:")
    else:
        render.print_line(f"-- Implementing ({len(still_red)} of {len(frame.test_names)} still red):")
    for f, n in zip(frame.test_files, frame.test_names):
        tag = "" if n in still_red else " (already passing)"
        render.print_line(f"   {f} :: {n}{tag}")
    render.print_line(f"   Criterion: {frame.criterion}")

    changed_files = run_implement_with_refine(frame, args.model, commands, args.max_attempts)

    render.print_line()
    render.print_line(f"-- Implemented: {frame.criterion}")
    if len(frame.test_names) == 1:
        render.print_line(f"   Test now green: {frame.test_files[0]} :: {frame.test_names[0]}")
    else:
        render.print_line(f"   All {len(frame.test_names)} test(s) now green:")
        for f, n in zip(frame.test_files, frame.test_names):
            render.print_line(f"     {f} :: {n}")
    render.print_line(f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}")
    render.print_line("-- Run 'next_step' to pop this criterion and continue.")
    render.print_line(f"-- Token usage: {ai_client.usage}")


if __name__ == "__main__":
    main()
