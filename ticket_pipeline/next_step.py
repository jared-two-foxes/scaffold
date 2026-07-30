#!/usr/bin/env python3
"""
next_step - advance the criteria stack by exactly one phase, pausing
only when genuinely human-only input is required. The main
orchestrator of the criteria-stack pipeline; run `push_ticket <id>`
once to seed the stack, then run this repeatedly until it reports no
work remaining.

No ticket_id argument - the ticket is read from the stack itself.

Phase detection (re-run fresh from real state at the top of every
step - the frame's `status` field is a hint, never a trust boundary).
A criterion almost always tracks exactly one test, but can track
several (test_files/test_names are parallel lists) when its own
behavior genuinely spans call paths that can't share one test function
- see test-criterion.prompt.md's Step 3. Everything below describes the
general N-test case; N=1 behaves exactly as it always has.

  stack empty                              -> done, nothing to do
  top frame status == "validating"         -> TICKET_VALIDATE (a prior
                                               attempt for this ticket
                                               died partway through;
                                               resume it directly)
  top frame status == "green-unconfirmed"  -> re-run every scoped test
                                               (recheck_test_frame):
    any still red                          -> becomes a normal
                                               test-written/AWAIT_IMPL case
                                               (someone fixed it)
    all green, nothing left unconfirmed    -> mark done, re-detect (POP)
    all green, some still unconfirmed,
      --accept-green passed                -> mark done, re-detect (POP)
    all green, some still unconfirmed,
      no --accept-green                    -> pause again (always exits)
  top frame status in ("pending",
    "awaiting-manual-impl"),
    verification == "manual"                -> MANUAL_CRITERION (no test
                                               involved at all - see below)
  top frame status == "pending",
    verification == "refactor"              -> REFACTOR_SETUP (existing tests
                                               are the safety net - see below)
  top frame status == "pending",
    --skip-test                             -> SKIP_TEST_GATE (skip Tester AI
                                               and hand directly to Implementor;
                                               no red/green loop)
  top frame status == "pending",
    --manual-test                           -> MANUAL_TEST_GATE (skip Tester AI;
                                               use --manual-test-ref refs, or
                                               existing_test refs if present;
                                               run compile + scoped tests)
  top frame status == "pending",
    verification == "test-refactor",
    check_test_refactor_satisfied            -> mark done, re-detect (POP)
                                             (mechanical pre-check: the
                                              named file(s) already match the
                                              criterion's structural claims
                                              - no WRITE_TEST AI call at all)
  top frame status == "baseline-confirmed",
    verification == "refactor"              -> recheck_refactor_tests:
    any safety-net test red                  -> pause (refactor broke something)
    all green, no production file changed     -> pause (refactor not done yet)
    all green + a production file changed     -> mark done, re-detect (POP)
  top frame status == "pending"            -> WRITE_TEST
    (verification == "test-refactor" flows
     through here too: the test-writer rewrites
     an existing test; a GREEN rewrite pops
     immediately for origin="ticket")
  WRITE_TEST tester writes no files      -> _handle_no_test_written:
    --accept-no-test, or mechanical check     -> mark done, re-detect (POP)
      confirms it
    AI recheck (first visit) says SATISFIED    -> mark done, re-detect (POP)
    otherwise (or on resume, skip_ai=True)    -> pause in 'nothing-written'
                                               (--accept-no-test pops later)
  top frame status == "nothing-written"    -> _handle_no_test_written (resume,
                                               skip_ai=True): mechanical check
                                               + --accept-no-test as above,
                                               no AI re-spend; else pause again
  top frame status == "test-written",
    missing test_files/test_names          -> WRITE_TEST (retry)
  top frame status == "test-written"       -> re-run every scoped test
                                               (recheck_test_frame):
    any still red,
      --skip-implementation passed         -> AWAIT_IMPL (pause for manual impl)
    any still red,
      no --skip-implementation             -> IMPLEMENT (AI implementation)
    all green, nothing unconfirmed         -> mark done, re-detect (POP)
    all green, something unconfirmed       -> becomes green-unconfirmed
                                               (see above)
  top frame status == "done"               -> POP

WRITE_TEST (do_write_test) runs a single unified three-gate loop -
compile, then red/green, then an independent test-quality review -
sharing one bounded attempt budget. The quality review is gating inside
that loop on both red and green tests: FLAGGED feeds the concern back to
the Tester for an amendment attempt. If the budget exhausts with
quality still flagged, the concern falls back to advisory (printed and
logged, test accepted) rather than killing the run; compile still
failing at exhaustion stays fatal, as before. The origin-based
green-trust check (below) is orthogonal to the quality gate.

A test that comes back green immediately, the very first time WRITE_TEST
writes it, is trusted unconditionally only when the criterion is
origin="ticket" (the ticket's own initial criteria - one criterion's
implementation can legitimately satisfy a sibling as a side effect). For
any other origin (validate-missed/review - pushed *because* an
independent check already judged the criterion unsatisfied), that test
is untrusted by default and recorded in frame.unconfirmed_tests: much
more likely a weak test than the gap having genuinely disappeared, and
auto-trusting it here is what turns into a boundless validate ->
false-green -> pop -> re-validate loop, silently burning AI calls under
--continuous with no human ever seeing it. This check happens exactly
once, at the moment a test is first observed (do_write_test) - if other
tests in the same group are still red, the group proceeds to AWAIT_IMPL
regardless (real work remains either way); the unconfirmed name(s) only
ever actually block popping once every test in the group is green.
frame.unconfirmed_tests only ever shrinks after that first observation
(a name is dropped once observed red - real implementation happened, so
it's no longer an untested free pass) - --accept-green explicitly
confirms whatever's still in it and moves on.

A frame tagged verification="manual" (documentation, config, CI changes
- anything narrow-plan.prompt.md's Step 4 judged as having no meaningful
red/green - see extract_verification_mode) skips WRITE_TEST/AWAIT_IMPL
entirely: there's no test to write. Its own mechanical floor is weaker
by necessity - do the file(s) named in the criterion/plan_context
(extract_referenced_paths) actually show up in git_changed_files()? A
match there is real, direct evidence (unlike a test, it can't be
trivially gamed the way a weak test's false green can, so - unlike
green-unconfirmed - it's trusted regardless of origin) and pops the
frame immediately, even on the very first look, before ever pausing. No
match pauses (always exits), same shape as AWAIT_IMPL. If no file could
be identified at all, there's nothing to mechanically check - popping
requires an explicit --accept-manual, the same escape-hatch shape as
--accept-green, rather than ever silently trusting a criterion this
pipeline has no way to verify.

A frame tagged verification="test-refactor" (a structural change to
test code - imports/helpers/utilities, not assertions - see
extract_verification_mode) flows through WRITE_TEST like a normal "test"
criterion, but the test-writer is told to *rewrite* the named existing
test(s) rather than write a failing one, and the quality reviewer is
told a GREEN outcome is expected (not a suspicious tautology). A rewrite
that comes back GREEN pops immediately for origin="ticket" (same as any
green-at-write-time test); a rewrite that comes back RED is an incorrect
rewrite (not a gap to implement), so implement_step refuses it and the
human fixes the test by hand before re-running next_step.

A frame tagged verification="refactor" (a structural change to
production code that preserves behavior - see
extract_verification_mode) skips WRITE_TEST entirely: the existing tests
named in its existing_test_refs are the safety net, not the target.
REFACTOR_SETUP runs a mandatory baseline check first - every safety-net
test must be GREEN *before* the refactor starts; RED at baseline dies
hard (a GREEN-after-refactor check is meaningless if the tests were red
to begin with, so the pipeline refuses to mask a pre-existing breakage
as "the refactor's job to fix"). On GREEN, it pauses (status =
baseline-confirmed) for the human or implement_step to make the
structural changes. The next next_step call rechecks: every safety-net
test must still be GREEN *and* a production file this criterion names
must appear in git_changed_files() - the conjunction that proves the
refactor preserved behavior AND actually happened, so a no-op can't
slip through on "nothing broke" alone. A refactor criterion with no
identifiable safety-net tests is meant to be tagged manual by the
narrower (see narrow-plan.prompt.md Step 4) - it never reaches this
mode.

POP removes the top frame. If the new top frame belongs to a different
ticket (or the stack is now empty), that ticket's frames are all done:
run TICKET_VALIDATE (fresh re-narrow safety net, lint, full test suite,
smoke, code review). Before doing anything fallible, TICKET_VALIDATE
pushes a durable "validating" sentinel frame for that ticket (popped
again only on an APPROVED verdict) - so if lint/the full test suite/
smoke/an unparseable review kills the process partway through, the
sentinel survives on the stack and the next `next_step` call resumes
validation instead of reporting "no work remaining" or moving on to a
different ticket with no way back to this one. A CHANGES REQUESTED
review, or a safety-net re-narrow that still finds criteria, pushes new
frames onto the stack ahead of the sentinel (tagged with the same
ticket) instead of failing outright, so the next `next_step` call picks
the pipeline back up automatically.

Exit codes: 0 at every human pause point (green-unconfirmed,
nothing-written, manual acceptance needed, review findings pushed,
validate-missed criteria pushed, stack empty) - the user must be able
to tell "inspect or confirm something" apart from "something broke"
without parsing output. Non-zero only on a genuine pipeline failure
(compile error exhausted its retries, lint/test-suite/smoke failure,
unparseable review).

--continuous advances through every automatable transition (test
writing, implementation, popping, and the next criterion starting)
without stopping, pausing only at a genuine human pause point.

Usage:
    next_step [--model <model-id>] [--config <path>] [--continuous]
              [--manual-test [--manual-test-ref <file::qualified_test_name> ...]]
              [--skip-test]
              [--skip-implementation]
              [--log-level <level>]
"""

import argparse
import subprocess
import sys
from pathlib import Path

from .lib import ai_client, pipeline_lib as lib, render, tools, verbosity

log = verbosity.get_logger(__name__)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"


# Final safety cap after signal-extraction (see lib.extract_test_output_signal)
# - a heuristic filter can still let through a lot if a run genuinely has
# many failures, so this is a backstop, not the primary noise control.
RED_CHECK_OUTPUT_TAIL_CHARS = 4000

# A frame whose freshly-written test came back green immediately, for a
# criterion that did NOT originate from the initial push_ticket seeding
# (origin != "ticket" - i.e. origin="validate-missed" or "review", both
# pushed *because* an independent check just judged this unsatisfied).
# Distinct from "test-written": that status means a confirmed-red test
# is waiting for implementation; this one means the test is green but
# nobody has confirmed that's legitimate rather than a false green (the
# test doesn't actually exercise the described behavior). See
# do_write_test/do_await_green_unconfirmed for why this distinction
# exists - without it, a false green here loops forever: WRITE_TEST
# writes a weak test -> green -> auto-popped as done -> TICKET_VALIDATE's
# safety-net re-narrow still finds the same gap (nothing was ever
# implemented) -> pushes it right back as a new validate-missed frame ->
# repeat, silently burning AI calls under --continuous with no human
# ever seeing it.
GREEN_UNCONFIRMED_STATUS = "green-unconfirmed"

# A verification="manual" frame (see extract_verification_mode) that's
# been paused at least once already - distinguishes "first look, not
# checked yet" (status="pending") from "already told the human to make
# this change" only for the pause message's wording; the mechanical
# floor check in do_manual_criterion runs identically either way, same
# principle as every other phase (status is a hint, never a trust
# boundary).
MANUAL_PENDING_STATUS = "awaiting-manual-impl"

# A frame whose WRITE_TEST run produced no test files at all - the
# Tester re-read the code and wrote nothing, a strong signal the
# criterion may already be satisfied (the test-refactor landed before or
# during the run, or a behavior criterion was incidentally satisfied by
# a sibling). Distinguished from "pending" so the next `next_step` call
# re-enters the recovery path (_handle_no_test_written with skip_ai=True)
# rather than blindly retrying WRITE_TEST, and from "test-written"
# (which means a confirmed-red test is awaiting implementation).
# Pops via --accept-no-test, or once the mechanical check/AI recheck can
# confirm satisfaction; otherwise pauses for a human, same escape-hatch
# shape as --accept-green/--accept-manual.
NOTHING_WRITTEN_STATUS = "nothing-written"
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


def do_await_impl(frame: "lib.CriterionFrame", test_results: list[tuple[str, str, subprocess.CompletedProcess]]) -> None:
    """
    test_results: one (file, name, CompletedProcess) tuple per test in
    the frame's group, same order as test_files/test_names - the caller
    (do_write_test or recheck_test_frame) already ran these itself, so
    this only renders what's already known, never re-runs anything.
    Almost always a single red entry (the common N=1 case); more than
    one only for a criterion needing multiple tracked tests (see
    test-criterion.prompt.md's Step 3).
    """
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
            tag = " - unconfirmed, weak-test risk" if n in frame.unconfirmed_tests else ""
            render.print_line(f"     {f} :: {n}{tag}")
    render.print_line(f"   Criterion: {frame.criterion}")
    render.print_line("   Manual implementation required (--skip-implementation is set).")
    render.print_line("   Implement it by hand, then run 'next_step' to re-check.")
    render.print_line("   (Or run 'next_step' without --skip-implementation for AI implementation.)")
    for f, n, r in red:
        output = ((r.stdout or "") + (r.stderr or "")).strip()
        if output:
            # test_filter_cmd's default isn't scoped to a single test
            # binary/file (it's a name filter applied across every test
            # file in the project - see toolchains.py), so raw output is
            # often mostly irrelevant "running tests\other_file.rs (...)"
            # noise. Filter to the toolchain's own signal pattern first;
            # only fall back to a blind tail if that heuristic somehow
            # matched nothing.
            signal = lib.extract_test_output_signal(output, lib.get_toolchain().test_output_signal_pattern)
            render.print_line()
            label = f" for {n}" if len(test_results) > 1 else ""
            render.print_line(f"-- Red test output{label} (why it currently fails):")
            render.print_line(signal[-RED_CHECK_OUTPUT_TAIL_CHARS:])
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def do_await_green_unconfirmed(frame: "lib.CriterionFrame") -> None:
    """
    Every test in frame.test_names is currently green, but frame.
    unconfirmed_tests (a non-empty subset, possibly all of them) was
    never actually confirmed legitimate - see recheck_test_frame/
    do_write_test for how a test lands here. Lists every test, tagging
    which specific one(s) are the actual concern, since a mixed group
    can have some already-trusted tests (origin="ticket", or a
    previously-red test since fixed by real implementation) alongside
    still-unconfirmed ones.
    """
    render.print_line()
    unconfirmed = set(frame.unconfirmed_tests)
    plural = len(unconfirmed) != 1
    render.print_line(
        f"-- Test(s) passed, but {'one has' if not plural else 'some have'} "
        f"not been confirmed legitimate:"
    )
    for file_path, name in zip(frame.test_files, frame.test_names):
        tag = " - UNCONFIRMED (passed without any implementation)" if name in unconfirmed else " - confirmed"
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
    render.print_line("     - inspect the unconfirmed test(s) and fix them if they're not testing the right thing,")
    render.print_line("       then run 'next_step' again (a now-red test resumes the normal flow), or")
    render.print_line(
        "     - if you're confident the behaviour really is already present, run "
        "'next_step --accept-green' to accept every unconfirmed test above and move on."
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def do_await_manual_impl(frame: "lib.CriterionFrame", paths: list[str]) -> None:
    render.print_line()
    render.print_line("-- Manual change needed (not test-verifiable):")
    render.print_line(f"   Criterion: {frame.criterion}")
    if frame.plan_context:
        render.print_line(f"   Context: {frame.plan_context}")
    if paths:
        render.print_line(f"   Expecting changes to: {', '.join(paths)}")
        render.print_line(
            "   Make the change by hand, or run 'next_step' again to let the "
            "pipeline attempt it automatically. A later 'next_step' run checks "
            "whether those file(s) actually changed before marking this done."
        )
    else:
        render.print_line(
            "   No specific file could be identified from this criterion, so "
            "there's nothing to mechanically check here. Make the change by "
            "hand, or run 'next_step' again to let the pipeline try it, then "
            "use 'next_step --accept-manual' to confirm it's done."
        )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def do_manual_criterion(stack: list, frame: "lib.CriterionFrame", accept_manual: bool, git_cfg: "lib.GitConfig | None" = None) -> None:
    """
    Handles a verification="manual" frame - a criterion that isn't
    expressible as a red/green test (documentation, config, CI changes -
    see extract_verification_mode). There's no scoped test to mechanically
    re-check the way every other phase can, so the floor here is
    different: does git_changed_files() show the file(s) this criterion/
    plan_context actually names (extract_referenced_paths)? That's real,
    direct evidence - the exact referenced file changed - not a proxy a
    lazy edit could trivially satisfy the way a weak test's false green
    can, so unlike GREEN_UNCONFIRMED_STATUS this doesn't need an
    origin-based trust distinction: a match is trusted regardless of why
    the frame was pushed, even on the very first look before ever pausing
    (mirrors do_write_test's origin="ticket" case - one criterion's
    change can satisfy another as a side effect).

    If no file could be identified at all, there is genuinely nothing to
    check mechanically - popping requires an explicit --accept-manual,
    same escape-hatch shape as --accept-green, rather than ever silently
    trusting a criterion this pipeline has no way to verify.
    """
    # Manual criteria skip WRITE_TEST, so record base_commit here instead
    # (same purpose as do_write_test's recording) - reset-criterion works
    # on manual criteria too.
    _record_base_commit_if_needed(stack, frame, git_cfg)
    paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
    mechanically_confirmed = bool(paths) and bool(set(paths) & set(lib.git_changed_files()))
    if mechanically_confirmed or accept_manual:
        frame.status = "done"
        lib.save_stack(stack)
        return
    frame.status = MANUAL_PENDING_STATUS
    lib.save_stack(stack)
    do_await_manual_impl(frame, paths)


def do_refactor_setup(
    stack: list, frame: "lib.CriterionFrame", commands: dict, git_cfg: "lib.GitConfig | None" = None,
) -> None:
    """
    REFACTOR_SETUP: the entry point for a verification="refactor" frame
    (see extract_verification_mode) - a criterion that restructures
    production code without changing behavior, using existing tests as
    the safety net. There's no WRITE_TEST here: the safety-net tests are
    already written (named in frame.existing_test_refs), and the
    criterion is *not* about adding coverage. Instead this:

      1. records base_commit (same as do_write_test/do_manual_criterion,
         so reset-criterion works on refactor frames too),
      2. populates frame.test_files/test_names from existing_test_refs
         (the safety-net tests the rest of this mode re-runs),
      3. runs a mandatory baseline check: every safety-net test must be
         GREEN *before* the refactor starts. RED at baseline means the
         safety net is already broken, so refusing to proceed is the
         only safe move - a GREEN-after-refactor check is meaningless if
         the tests were red to begin with. dies via die_with_log with a
         clear message rather than silently masking a pre-existing
         breakage as "the refactor's job to fix."
      4. on GREEN: sets status to BASELINE_CONFIRMED_STATUS and pauses
         (the human or implement_step makes the structural changes; the
         next 'next_step' call rechecks).

    There's no origin-based trust distinction here, unlike
    GREEN_UNCONFIRMED_STATUS: the safety-net tests are *pre-existing*
    (not freshly written by this pipeline), so a baseline GREEN is real
    evidence the behavior they cover currently works - nothing about
    how this frame was pushed changes that.
    """
    _record_base_commit_if_needed(stack, frame, git_cfg)

    # Populate test_names/test_files from existing_test_refs (each a
    # "file::qualified_test_name"). An empty list here is a programming
    # error, not a runtime path: narrow-plan.prompt.md's Step 4 requires
    # existing_test: refs on every refactor criterion, and a refactor
    # frame without any is meant to be tagged manual by the narrower; a
    # manual frame never reaches this function. Guard anyway so a bad
    # tag can't crash later code that assumes non-empty lists.
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
            criterion=frame.criterion, ticket=frame.ticket,
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
            criterion=frame.criterion, ticket=frame.ticket,
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
    stack: list, frame: "lib.CriterionFrame", commands: dict, git_cfg: "lib.GitConfig | None" = None,
) -> None:
    """
    Post-implementation check for a verification="refactor" frame already
    past its baseline (status == BASELINE_CONFIRMED_STATUS): re-runs the
    safety-net tests fresh and pops only on the conjunction that makes
    a refactor "done":
      - every safety-net test GREEN (the behavior the tests cover didn't
        change), AND
      - at least one production file this criterion/plan_context names
        actually appears in git_changed_files() (the structural change
        the criterion describes really happened - not just "nothing
        broke, so pop it" which would let a no-op pass).

    Any safety-net test RED pauses for human intervention (the refactor
    broke something) rather than dying - same pause-and-resume shape as
    recheck_test_frame, not the baseline check's hard stop. All GREEN but
    no named production file changed yet also pauses (the refactor
    hasn't actually happened) - the human/ implement_step hasn't done
    the work, same shape as do_manual_criterion's "no match yet" branch.
    Status is a hint, never a trust boundary, same principle as every
    other phase: this re-derives both checks from real state every call.
    """
    results = lib.run_scoped_tests(
        frame.test_names, commands, "refactor recheck", quiet=True
    )
    red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
    if red_names:
        # Re-establish the baseline (GREEN) before pausing, so the next
        # recheck is a clean "are they green now?" rather than a stuck
        # baseline-confirmed with known-red tests. Status stays
        # BASELINE_CONFIRMED_STATUS either way - the pause message is the
        # only thing that differs from the all-green-but-no-change branch.
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

    # All safety-net tests GREEN - confirm the refactor actually happened.
    paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
    if paths and not (set(paths) & set(lib.git_changed_files())):
        frame.status = lib.BASELINE_CONFIRMED_STATUS
        lib.save_stack(stack)
        render.print_line()
        render.print_line("-- Safety-net tests are GREEN but no production file has changed yet.")
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

    # Tests GREEN + a production file changed - criterion satisfied.
    # Set status="done" and let the caller's loop re-detect and dispatch
    # to do_pop, mirroring recheck_test_frame's all-green branch (which
    # sets status="done" and returns rather than popping directly).
    frame.status = "done"
    lib.save_stack(stack)
    return


def do_write_test(
    stack: list, frame: "lib.CriterionFrame", model: str, commands: dict,
    accept_no_test: bool = False,
    skip_implementation: bool = False,
    continuous: bool = False,
    max_attempts: int = 3,
    accept_green: bool = False,
    git_cfg: "lib.GitConfig | None" = None,
    feedback: str | None = None,
    previous_changed_files: list[str] | None = None,
) -> None:
    """
    Writes (or retries writing) frame's test(s) through the unified
    three-gate loop (lib.run_test_for_criterion_with_full_retry):
    compile -> red/green -> test-quality review, sharing one bounded
    attempt budget. Almost always exactly one test; more than one only
    when the criterion needed genuinely separate tests (test-criterion.
    prompt.md's Step 3). At least one test still red either pauses
    (when --skip-implementation is set) or immediately enters the AI
    implementation phase.

    The quality review is *gating* inside that loop: if it flags, the
    Tester gets the concern fed back and amends the test, up to the
    shared budget. If the budget exhausts with quality still flagged,
    the concern falls back to advisory here (printed + logged, test
    accepted) rather than killing the run - and it runs on green tests
    too, so a weak test that passes trivially gets a chance to be
    amended before bubbling up as green-unconfirmed.

    Each test's green-at-write-time handling depends on origin:
      - origin="ticket" (the ticket's own initial criteria): trusted
        unconditionally. Legitimate here because one criterion's
        implementation can easily satisfy a sibling criterion as a side
        effect within the same initial pass.
      - any other origin (validate-missed/review - pushed *because* an
        independent check already judged this unsatisfied): NOT trusted.
        A test passing this easily is much more likely weak than the
        gap having genuinely disappeared - see GREEN_UNCONFIRMED_STATUS's
        module-level comment for why auto-trusting it created a boundless
        loop. Recorded in frame.unconfirmed_tests rather than gating
        anything immediately if other tests in the group are still red
        (there's real work either way); only actually pauses here if
        EVERY test in the group is already green with none of them
        trusted (see the branches below). This origin check happens
        exactly once, right here, at the moment each test is first
        observed - never re-applied on a later resume (recheck_test_frame
        trusts any test that goes red-then-green after this point, since
        that's necessarily real implementation work, not an untested
        free pass). Orthogonal to the quality gate: a green test the
        quality loop accepted can still be unconfirmed by origin.
    """
    # Layer 2: record the pre-WRITE_TEST HEAD as this frame's base_commit
    # the first time it enters test writing, so reset-criterion can `git
    # reset --hard` back to exactly this point. Only recorded when
    # git_workflow is on and not already set (a retry re-entering
    # WRITE_TEST keeps the original base, not a moved-forward one).
    if git_cfg is not None and git_cfg.git_workflow and frame.base_commit is None:
        try:
            frame.base_commit = lib.git_current_head()
            lib.save_stack(stack)
        except lib.GitError as e:
            log.warning("-- git_workflow: could not record base_commit (non-fatal): %s", e)
    file_paths, test_names, test_results, compile_result, quality_concern = (
        lib.run_test_for_criterion_with_full_retry(
            frame.criterion, frame.plan_context, model, commands, ticket_id=frame.ticket,
            existing_test_refs=frame.existing_test_refs,
            verification=frame.verification,
            feedback=feedback,
            previous_changed_files=previous_changed_files,
        )
    )
    if file_paths is None:
        # The Tester wrote nothing - a strong signal this criterion may
        # already be satisfied (see _handle_no_test_written). Dispatch to
        # the recovery path rather than treating an empty result as a
        # normal test-written frame (there are no test_files/test_names
        # to dispatch on).
        _handle_no_test_written(stack, frame, model, accept_no_test)
        return
    if compile_result is None or compile_result.returncode != 0:
        exit_code = compile_result.returncode if compile_result is not None else "unknown"
        lib.die_with_log(
            "test-criterion",
            f"Test does not compile after retries (exit {exit_code}). See output above.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )

    # Advisory fallback: the quality gate is gating inside the loop, so
    # reaching here with a non-None concern means the budget exhausted
    # with quality still flagged. Printed so it's visible even on a path
    # that doesn't pause (origin="ticket", every test green-and-done).
    if quality_concern:
        lib.log_event(
            "review-test-quality", "flagged-advisory-fallback", error=quality_concern,
            criterion=frame.criterion, ticket=frame.ticket,
        )
        render.print_line()
        render.print_line("-- Test-quality review still flagged after retries (advisory, not blocking):")
        render.print_line(quality_concern)

    frame.test_files = file_paths
    frame.test_names = test_names

    # test_results is already populated by the loop (its last Gate 2
    # run). do_await_impl below prints its own filtered version of the
    # red output, so the loop ran those scoped tests quiet=True.
    test_results_zipped = list(zip(file_paths, test_names, test_results))
    red_names = [n for n, r in zip(test_names, test_results) if r.returncode != 0]
    green_names = [n for n, r in zip(test_names, test_results) if r.returncode == 0]
    # Ticket-origin green is always trusted, so nothing from it is ever
    # "unconfirmed" - every other origin's green needs confirmation.
    unconfirmed = [] if frame.origin == "ticket" else green_names

    if not red_names and not unconfirmed:
        # Every test is green, and none needed confirmation (only
        # possible for origin="ticket" - see above).
        log.info(
            "-- Test(s) passed without implementation - this criterion's "
            "gap didn't reproduce."
        )
        frame.status = "done"
        frame.unconfirmed_tests = []
        lib.save_stack(stack)
        return

    if not red_names and unconfirmed:
        # Every test is green, but at least one needs confirmation.
        frame.status = GREEN_UNCONFIRMED_STATUS
        frame.unconfirmed_tests = unconfirmed
        lib.save_stack(stack)
        do_await_green_unconfirmed(frame)
        return

    # At least one test is still red - real work remains regardless of
    # how many siblings are already (trusted or unconfirmed) green.
    frame.status = "test-written"
    frame.unconfirmed_tests = unconfirmed
    lib.save_stack(stack)
    if skip_implementation:
        do_await_impl(frame, test_results_zipped)
        return
    _run_implementation_phase(
        stack,
        frame,
        model,
        commands,
        continuous,
        max_attempts,
        accept_green=accept_green,
        accept_manual=False,
        git_cfg=git_cfg,
    )


def _handle_no_test_written(
    stack: list, frame: "lib.CriterionFrame", model: str,
    accept_no_test: bool, skip_ai: bool = False,
) -> None:
    """
    Recovery path for the "Tester wrote nothing" sentinel (file_paths is
    None from run_test_for_criterion_with_full_retry): the Tester re-read
    the code and wrote no test - a strong signal the criterion may
    already be satisfied (the test-refactor landed before or during this
    run, or a behavior criterion was incidentally satisfied by a
    sibling's implementation). Decides pop-vs-pause via two escalating
    checks, same "status is a hint, re-detect from real state" principle
    as every other phase:

      1. --accept-no-test, or the mechanical pre-check
         (check_test_refactor_satisfied) confirming it -> pop now (set
         status="done" and return; the next step() iteration's
         status=="done" branch calls do_pop, the same defer-and-pop
         shape do_write_test's all-green origin="ticket" path uses).
      2. Otherwise (skip_ai=False only) a focused single-criterion AI
         re-narrow (recheck_single_criterion) as a second opinion for the
         behavioral cases a text search can't verify. SATISFIED -> pop;
         NOT SATISFIED/UNKNOWN -> fall through.
      3. Otherwise pause for a human (exit 0), setting status to
         NOTHING_WRITTEN_STATUS so the next `next_step` re-enters this
         path with skip_ai=True (so it doesn't re-spend the AI call every
         resume) rather than blindly retrying WRITE_TEST.

    skip_ai=True is the resume-mode flag: a NOTHING_WRITTEN_STATUS frame
    re-entering this path has already had its AI second opinion (or lack
    thereof); re-running it on every resume would just burn calls. The
    mechanical check still runs on resume (cheap, and the codebase may
    have changed since the pause - that's also what lets a human who
    fixed the code in the meantime auto-pop without the flag), and
    --accept-no-test still pops.
    """
    # Step 1: explicit accept, or mechanical confirmation. The
    # mechanical check runs on every visit (including resumes) - it's
    # cheap, and a frame first reached here via the WRITE_TEST path may
    # not have had it run yet (the step() pre-check only fires for
    # verification="test-refactor"; a "test" criterion that wrote
    # nothing reaches here without it).
    if accept_no_test:
        log.info(
            "-- --accept-no-test: accepting %s as satisfied despite the "
            "tester writing nothing.", frame.criterion,
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

    # Step 2: AI second opinion (first visit only).
    if not skip_ai:
        verdict = lib.recheck_single_criterion(
            frame.criterion, frame.plan_context, model, ticket_id=frame.ticket,
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
        # NOT SATISFIED or UNKNOWN -> fall through to the human pause.

    # Step 3: pause for human confirmation.
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
    model: str,
    commands: dict,
    accept_green: bool,
    continuous: bool,
    max_attempts: int,
    skip_implementation: bool,
    git_cfg: "lib.GitConfig | None",
) -> None:
    """
    Re-verifies a frame already past its initial WRITE_TEST look (status
    "test-written" or GREEN_UNCONFIRMED_STATUS) by re-running every test
    in its group fresh - status is a hint, never a trust boundary, same
    principle as every other phase. Shared by both of step()'s resume
    branches (see below) since the logic converges regardless of which
    one the frame arrived from: origin-based trust was already decided
    once, at write time (do_write_test); this only ever shrinks
    frame.unconfirmed_tests (dropping any name now found red - it's no
    longer a suspiciously-easy green, it's just a normal red test again),
    never re-derives it from origin.
    """
    results = lib.run_scoped_tests(frame.test_names, commands, "phase check", quiet=True)
    test_results = list(zip(frame.test_files, frame.test_names, results))
    red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
    frame.unconfirmed_tests = [n for n in frame.unconfirmed_tests if n not in red_names]

    if red_names:
        frame.status = "test-written"
        lib.save_stack(stack)
        if skip_implementation:
            do_await_impl(frame, test_results)
            return
        _run_implementation_phase(
            stack,
            frame,
            model,
            commands,
            continuous,
            max_attempts,
            accept_green=False,
            accept_manual=False,
            git_cfg=git_cfg,
        )
        return

    # Every test is green now.
    if not frame.unconfirmed_tests:
        frame.status = "done"
        lib.save_stack(stack)
        return

    if accept_green:
        log.info(
            "-- --accept-green: accepting %s as satisfied despite origin=%r "
            "(unconfirmed test(s): %s).",
            frame.criterion, frame.origin, ", ".join(frame.unconfirmed_tests),
        )
        frame.status = "done"
        frame.unconfirmed_tests = []
        lib.save_stack(stack)
        return

    frame.status = GREEN_UNCONFIRMED_STATUS
    lib.save_stack(stack)
    do_await_green_unconfirmed(frame)


def _run_feedback_retry(
    stack: list,
    frame: "lib.CriterionFrame",
    model: str,
    commands: dict,
    accept_no_test: bool,
    max_attempts: int,
    skip_implementation: bool,
    continuous: bool,
    git_cfg: "lib.GitConfig | None",
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
        if not git_cfg or not git_cfg.git_workflow or frame.base_commit is None:
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
        do_write_test(
            stack,
            frame,
            model,
            commands,
            accept_no_test=accept_no_test,
            feedback=feedback,
            previous_changed_files=previous_changed_files,
            skip_implementation=skip_implementation,
            continuous=continuous,
            max_attempts=max_attempts,
            git_cfg=git_cfg,
        )
        return

    if target == lib.FEEDBACK_TARGET_IMPLEMENTOR:
        import ticket_pipeline.implement_step as implement_step

        lib.save_stack(stack)
        lib.log_feedback_event(
            "apply-implementor",
            feedback,
            criterion=frame.criterion,
            ticket=frame.ticket,
            target=target,
        )
        if frame.verification == "refactor":
            implement_step.run_implement_with_refine(
                frame,
                model,
                commands,
                max_attempts,
                verification="refactor",
                feedback=feedback,
                previous_changed_files=previous_changed_files,
            )
            recheck_refactor_tests(stack, frame, commands, git_cfg)
            return
        implement_step.run_implement_with_refine(
            frame,
            model,
            commands,
            max_attempts,
            feedback=feedback,
            previous_changed_files=previous_changed_files,
        )
        recheck_test_frame(
            stack,
            frame,
            model,
            commands,
            accept_green=False,
            continuous=continuous,
            max_attempts=max_attempts,
            skip_implementation=skip_implementation,
            git_cfg=git_cfg,
        )
        return

    lib.die_with_log(
        "feedback",
        f"Human-targeted feedback is not an automated retry path for verification={frame.verification!r}.",
        criterion=frame.criterion,
        ticket=frame.ticket,
    )


def _run_implementation_phase(
    stack: list,
    frame: "lib.CriterionFrame",
    model: str,
    commands: dict,
    continuous: bool,
    max_attempts: int,
    accept_green: bool,
    accept_manual: bool,
    git_cfg: "lib.GitConfig | None",
) -> None:
    import ticket_pipeline.implement_step as implement_step

    if frame.verification == "manual":
        _record_base_commit_if_needed(stack, frame, git_cfg)
        paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
        mechanically_confirmed = bool(paths) and bool(set(paths) & set(lib.git_changed_files()))
        if mechanically_confirmed or accept_manual:
            frame.status = "done"
            lib.save_stack(stack)
            return

        changed_files = implement_step.run_implement_direct_with_refine(
            frame, model, commands, max_attempts
        )
        render.print_line()
        render.print_line(f"-- Implemented: {frame.criterion}")
        render.print_line(f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}")
        render.print_line(
            "-- Run 'next_step' again to check whether this satisfies the criterion and continue."
        )
        render.print_line(f"-- Token usage: {ai_client.usage}")
        if continuous and paths:
            return
        sys.exit(0)

    if frame.verification == "refactor":
        results = lib.run_scoped_tests(
            frame.test_names, commands, "refactor pre-implement check", quiet=True
        )
        red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
        paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
        if not red_names and (not paths or (set(paths) & set(lib.git_changed_files()))):
            frame.status = "done"
            lib.save_stack(stack)
            return

        changed_files = implement_step.run_implement_with_refine(
            frame,
            model,
            commands,
            max_attempts,
            verification="refactor",
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
        render.print_line("-- Run 'next_step' again to re-check and pop this criterion.")
        render.print_line(f"-- Token usage: {ai_client.usage}")
        if continuous:
            return
        sys.exit(0)

    results = lib.run_scoped_tests(frame.test_names, commands, "pre-implement phase check", quiet=True)
    red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
    frame.unconfirmed_tests = [n for n in frame.unconfirmed_tests if n not in red_names]
    if not red_names:
        if not frame.unconfirmed_tests:
            frame.status = "done"
            lib.save_stack(stack)
            return
        if accept_green:
            frame.status = "done"
            frame.unconfirmed_tests = []
            lib.save_stack(stack)
            return
        frame.status = GREEN_UNCONFIRMED_STATUS
        lib.save_stack(stack)
        do_await_green_unconfirmed(frame)
        return

    changed_files = implement_step.run_implement_with_refine(
        frame, model, commands, max_attempts
    )
    render.print_line()
    render.print_line(f"-- Implemented: {frame.criterion}")
    if len(frame.test_names) == 1:
        render.print_line(f"   Test now green: {frame.test_files[0]} :: {frame.test_names[0]}")
    else:
        render.print_line(f"   All {len(frame.test_names)} test(s) now green:")
        for f, n in zip(frame.test_files, frame.test_names):
            render.print_line(f"     {f} :: {n}")
    render.print_line(f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}")
    render.print_line("-- Run 'next_step' again to pop this criterion and continue.")
    render.print_line(f"-- Token usage: {ai_client.usage}")
    if continuous:
        return
    sys.exit(0)


def _parse_manual_test_refs(
    frame: "lib.CriterionFrame", manual_test_refs: list[str] | None
) -> tuple[list[str], list[str]]:
    # Precedence: explicit CLI refs first, then existing_test: refs from
    # the criterion as a fallback for manual mode.
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
        # Strip optional markdown-style quoting/backticks from refs users
        # copied out of plans/comments.
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


def do_manual_test_authoring(
    stack: list,
    frame: "lib.CriterionFrame",
    commands: dict,
    manual_test_refs: list[str] | None,
    model: str,
    continuous: bool,
    max_attempts: int,
    skip_implementation: bool,
    git_cfg: "lib.GitConfig | None" = None,
) -> None:
    _record_base_commit_if_needed(stack, frame, git_cfg)
    test_files, test_names = _parse_manual_test_refs(frame, manual_test_refs)
    frame.test_files = test_files
    frame.test_names = test_names
    compile_result = lib.run_command(commands["test_compile_cmd"], "manual test compile gate")
    if compile_result.returncode != 0:
        lib.die_with_log(
            "manual-test",
            f"Manual test compile gate failed (exit {compile_result.returncode}). "
            "Fix the test(s) and re-run 'next_step --manual-test'.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )
    scoped_results = lib.run_scoped_tests(test_names, commands, "manual test red check", quiet=True)
    red_names = [n for n, r in zip(test_names, scoped_results) if r.returncode != 0]
    green_names = [n for n, r in zip(test_names, scoped_results) if r.returncode == 0]
    # Ticket-origin criteria can trust a green-at-write-time sibling
    # effect; validate-missed/review origins stay unconfirmed until
    # explicit acceptance (same trust split as do_write_test).
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
    if skip_implementation:
        do_await_impl(frame, test_results)
        return
    _run_implementation_phase(
        stack,
        frame,
        model,
        commands,
        continuous,
        max_attempts,
        accept_green=False,
        accept_manual=False,
        git_cfg=git_cfg,
    )


def do_skip_test_direct_implementation(
    stack: list,
    frame: "lib.CriterionFrame",
    model: str,
    commands: dict,
    max_attempts: int,
    accept_no_test: bool,
    git_cfg: "lib.GitConfig | None" = None,
) -> None:
    import ticket_pipeline.implement_step as implement_step

    _record_base_commit_if_needed(stack, frame, git_cfg)
    implement_step.run_implement_direct_with_refine(frame, model, commands, max_attempts)
    _handle_no_test_written(
        stack,
        frame,
        model,
        accept_no_test=accept_no_test,
        skip_ai=False,
    )


def do_pop(frame: "lib.CriterionFrame", continuous: bool, model: str, step_models: dict[str, str], commands: dict, config_path: Path, git_cfg: "lib.GitConfig | None" = None) -> None:
    just_popped_ticket = frame.ticket
    just_popped_criterion = frame.criterion
    # Layer 2: commit this criterion's worth of work *before* popping the
    # frame, while its criterion text is still in hand for the commit
    # message. A None (empty diff) is a logged skip, never a blocker -
    # the criterion is already verified green, that's the gate, not the
    # commit. A real git error is also non-fatal here: the POP still
    # happens so the stack advances; the uncommitted changes just ride
    # along into the next criterion's commit.
    if git_cfg is not None and git_cfg.git_workflow:
        try:
            sha = lib.commit_criterion(git_cfg, just_popped_ticket, just_popped_criterion)
            if sha is not None:
                frame.commit_sha = sha
                lib.log_event(
                    "git-workflow", "criterion-committed",
                    ticket=just_popped_ticket, criterion=just_popped_criterion,
                    commit_sha=sha,
                )
        except lib.GitError as e:
            log.warning("-- git_workflow: commit-on-POP failed (non-fatal): %s", e)
    lib.pop_frame()
    new_stack = lib.load_stack()

    render.print_line()
    render.print_line(f"-- Criterion done: {just_popped_criterion}")

    if not new_stack or new_stack[0].ticket != just_popped_ticket:
        do_ticket_validate(just_popped_ticket, model, step_models, commands, config_path, git_cfg)
        return

    if not continuous:
        render.print_line(f"-- Next: {new_stack[0].criterion}")
        render.print_line(f"-- Token usage: {ai_client.usage}")
        sys.exit(0)
    # continuous: fall through, letting the caller's loop re-dispatch
    # straight into the new top frame's WRITE_TEST phase.


def do_ticket_validate(ticket_id: str, model: str, step_models: dict[str, str], commands: dict, config_path: Path, git_cfg: "lib.GitConfig | None" = None) -> None:
    """
    Full ticket-validation gate, run once a ticket's per-criterion
    frames are all popped: fresh re-fetch + re-narrow (safety net for
    criteria the per-criterion gates missed), lint, full test suite,
    smoke test, code review. A CHANGES REQUESTED review or a non-empty
    safety-net re-narrow pushes new frames instead of failing outright -
    next_step is meant to be re-run, not treated as a one-shot gate.

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
    plan_model = step_models.get("plan", model)
    narrow_model = step_models.get("narrow", model)
    review_model = step_models.get("review", model)
    lib.ensure_validating_sentinel(ticket_id)

    render.print_line()
    render.print_line(f"-- All criteria for {ticket_id} done. Running full ticket validation ...")

    ticket_content = lib.fetch_ticket_text(ticket_id)
    tools.write_file_block(str(lib.TICKET_FILE))(ticket_content)

    if lib.PLAN_FILE.is_file():
        plan_text = lib.PLAN_FILE.read_text(encoding="utf-8")
    else:
        plan_text = lib.run_plan_step(ticket_content, plan_model, ticket_id=ticket_id)

    gap_plan_content = lib.run_narrow_step(ticket_content, plan_text, narrow_model, ticket_id=ticket_id)
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
                plan_context=lib.extract_plan_context_for_criterion(criterion, gap_plan_content),
                test_files=None,
                test_names=None,
                status="pending",
                origin="validate-missed",
                verification=lib.extract_verification_mode(criterion),
                existing_test_refs=lib.extract_existing_test_refs(criterion),
            )
            for criterion in remaining
        ]
        missed_frames, newly_declined, skipped_count = lib.filter_grounded_frames(candidate_frames)
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

    lib.run_lint_gate(commands)

    result = lib.run_command(commands["test_cmd"], "full test suite gate")
    if result.returncode != 0:
        lib.die_with_log(
            "test-suite",
            f"Full test suite fails after all criteria implemented (exit "
            f"{result.returncode}). A criterion's scoped test passing doesn't "
            f"guarantee an earlier criterion's test still does - see output above.",
            ticket=ticket_id,
        )

    smoke_cmd = lib.load_smoke_cmd(config_path)
    lib.run_smoke_gate(smoke_cmd)

    changed_files = lib.git_changed_files()
    if not changed_files:
        lib.die_with_log(
            "review", "No changed files found (git diff/untracked are both empty). Nothing to review.",
            ticket=ticket_id,
        )

    # plan_text (the full original plan), not gap_plan_content: by this
    # point remaining is guaranteed empty (a non-empty one would have
    # exited above), so gap_plan_content's Acceptance Criteria section
    # always reads "nothing left to do" here - useless, misleading scope
    # for the reviewer. plan_text is the actual full ticket scope the
    # implementation was supposed to satisfy, same as the legacy
    # validate-and-review.py's review gate always used.
    verdict, review_text = lib.run_review_gate(changed_files, plan_text, review_model, ticket_id=ticket_id)

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
        render.print_line(f"   Smoke test: {'passed' if smoke_cmd else 'skipped (not configured)'}")
        render.print_line(f"   Files reviewed ({len(changed_files)}): {', '.join(changed_files)}")
        render.print_line("   Code review: APPROVED")
        render.print_line()
        render.print_line(f"-- {ticket_id} fully validated. Success.")
        # Layer 3: merge the ticket branch back to its base (Tier 1) or
        # push + open a PR (Tier 2). Runs after the sentinel is popped so
        # a failure here can't leave a stale "still needs validating"
        # marker. Non-fatal: the verdict is already APPROVED, so a merge
        # conflict or push failure is surfaced as a warning, not a die.
        if git_cfg is not None:
            lib.post_validate_git(
                git_cfg, ticket_id,
                title=f"{ticket_id}: validated",
                body=f"Ticket {ticket_id} passed the pipeline's full validation gate "
                     f"(lint, test suite, smoke, code review).",
            )
        render.print_line(f"-- Token usage: {ai_client.usage}")
        sys.exit(0)

    do_push_review_findings(ticket_id, review_text)


def print_declined_criteria(newly_declined: list[tuple["lib.CriterionFrame", list[str]]]) -> None:
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
    render.print_line(f"-- {len(newly_declined)} {noun} failed mechanical grounding - NOT pushed:")
    for frame, reasons in newly_declined:
        render.print_line(f"   {frame.criterion}")
        for reason in reasons:
            render.print_line(f"     - {reason}")
    render.print_line(
        f"-- Not resolved automatically. Fix the ticket/gap-plan wording, or if this is a "
        f"false positive, review and clear the entry from {lib.DECLINED_CRITERIA_FILE}."
    )


def do_push_review_findings(ticket_id: str, review_text: str) -> None:
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
        )
        for finding in findings
    ]
    new_frames, newly_declined, skipped_count = lib.filter_grounded_frames(candidate_frames)
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
    model: str, commands: dict, continuous: bool, config_path: Path,
    step_models: dict[str, str] | None = None,
    accept_green: bool = False, accept_manual: bool = False,
    accept_no_test: bool = False,
    manual_test: bool = False,
    manual_test_refs: list[str] | None = None,
    skip_test: bool = False,
    skip_implementation: bool = False,
    max_attempts: int = 3,
    git_cfg: "lib.GitConfig | None" = None,
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
    log.info("-- next_step: ticket=%s status=%s criterion=%s", frame.ticket, frame.status, frame.criterion)

    if manual_test:
        if frame.status != "pending":
            lib.die_with_log(
                "manual-test",
                "Manual test mode only applies when the top frame is pending.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        if frame.verification in ("manual", "refactor"):
            lib.die_with_log(
                "manual-test",
                f"Manual test mode is not valid for verification={frame.verification!r}.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        do_manual_test_authoring(
            stack,
            frame,
            commands,
            manual_test_refs,
            model,
            continuous,
            max_attempts,
            skip_implementation,
            git_cfg,
        )
        return

    if skip_test:
        if frame.status != "pending":
            lib.die_with_log(
                "skip-test",
                "Skip-test mode only applies when the top frame is pending.",
                criterion=frame.criterion,
                ticket=frame.ticket,
            )
        if frame.verification != "test":
            lib.die_with_log(
                "skip-test",
                f"Skip-test mode is only valid for verification='test' (got {frame.verification!r}).",
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
        do_skip_test_direct_implementation(
            stack, frame, model, commands, max_attempts, accept_no_test, git_cfg
        )
        return

    if frame.status == lib.VALIDATING_STATUS:
        # A prior TICKET_VALIDATE attempt for this ticket died partway
        # through (lint/test-suite/smoke/unparseable review) and left
        # this sentinel behind - re-enter validation directly, no pop
        # needed (there's nothing left to pop; the real criteria are
        # long gone).
        do_ticket_validate(frame.ticket, model, step_models, commands, config_path, git_cfg)
        return

    if frame.status == FEEDBACK_READY_STATUS:
        _run_feedback_retry(
            stack,
            frame,
            model,
            commands,
            accept_no_test,
            max_attempts,
            skip_implementation=skip_implementation,
            continuous=continuous,
            git_cfg=git_cfg,
        )
        return

    if frame.status == GREEN_UNCONFIRMED_STATUS:
        # Always re-verify real state rather than trusting the stored
        # status (same principle as every other phase) - the human may
        # have fixed the test(s) in the meantime, in which case this
        # becomes a normal AWAIT_IMPL case. Shared with the "test-written"
        # resume branch below - see recheck_test_frame.
        recheck_test_frame(
            stack,
            frame,
            model,
            commands,
            accept_green,
            continuous,
            max_attempts,
            skip_implementation,
            git_cfg,
        )
        return

    if frame.status == NOTHING_WRITTEN_STATUS:
        # A prior WRITE_TEST run wrote no test files and paused here -
        # re-enter the recovery path with skip_ai=True so it doesn't
        # re-spend the AI second opinion every resume. The mechanical
        # check still runs (cheap, and may now confirm a human fix), and
        # --accept-no-test still pops. See _handle_no_test_written.
        _handle_no_test_written(stack, frame, model, accept_no_test, skip_ai=True)
        return

    if frame.verification == "manual":
        if frame.status == "pending":
            do_manual_criterion(stack, frame, accept_manual, git_cfg)
            return
        if frame.status == MANUAL_PENDING_STATUS:
            _run_implementation_phase(
                stack, frame, model, commands, continuous, max_attempts,
                accept_green, accept_manual, git_cfg
            )
            return

    # refactor mode (see extract_verification_mode): a structural change to
    # production code, using existing tests as the safety net. No WRITE_TEST -
    # the safety-net tests already exist (named in existing_test_refs). Two
    # phases: baseline confirmation (GREEN required before the refactor
    # starts), then post-implementation recheck (GREEN + a production file
    # changed). test-refactor mode needs no dispatch of its own here: it flows
    # through the existing pending/test-written branches, with the
    # test-writer's rewrite branching on the verification tag (see
    # build_test_criterion_prompt).
    if frame.verification == "refactor":
        if frame.status == "pending":
            do_refactor_setup(stack, frame, commands, git_cfg)
            return
        if frame.status == lib.BASELINE_CONFIRMED_STATUS:
            _run_implementation_phase(
                stack, frame, model, commands, continuous, max_attempts,
                accept_green, accept_manual, git_cfg
            )
            return
        # A refactor frame in any other status (e.g. "done") falls through
        # to the general handling below.

    if frame.status == "pending":
        # Pre-check verify="test-refactor" frames for already-satisfied
        # criteria before spending a WRITE_TEST AI call: a test-refactor
        # criterion has no red/green signal to re-detect satisfaction from
        # (expected GREEN throughout), so the only mechanical floor is
        # "do the named file(s) actually contain (or no longer contain)
        # what the criterion describes?" (check_test_refactor_satisfied).
        # This is the same "status is a hint, re-detect from real state"
        # principle the state machine already applies to verify="test"
        # (red/green) and verify="refactor" (baseline + git-changed-files).
        # A confirmed-satisfied frame pops without ever calling the Tester
        # - mirroring do_write_test's all-green origin="ticket" path (set
        # status="done" and return; the next step() iteration pops).
        if (
            frame.verification == "test-refactor"
            and lib.check_test_refactor_satisfied(frame.criterion, frame.existing_test_refs)
        ):
            log.info(
                "-- test-refactor criterion already satisfied (mechanical "
                "pre-check) - popping without WRITE_TEST."
            )
            frame.status = "done"
            frame.unconfirmed_tests = []
            lib.save_stack(stack)
            return
        do_write_test(
            stack,
            frame,
            model,
            commands,
            accept_no_test=accept_no_test,
            skip_implementation=skip_implementation,
            continuous=continuous,
            max_attempts=max_attempts,
            accept_green=accept_green,
            git_cfg=git_cfg,
        )
        return

    if frame.status == "test-written":
        if not frame.test_files or not frame.test_names:
            log.warning("-- Frame is test-written but missing test_files/test_names - retrying WRITE_TEST.")
            do_write_test(
                stack,
                frame,
                model,
                commands,
                accept_no_test=accept_no_test,
                skip_implementation=skip_implementation,
                continuous=continuous,
                max_attempts=max_attempts,
                accept_green=accept_green,
                git_cfg=git_cfg,
            )
            return
        # Re-run the scoped tests first so green/unconfirmed cases still
        # resolve exactly as before; _run_implementation_phase already
        # did this pre-check too, so this keeps that behavior centralized
        # while also honoring --skip-implementation in one place.
        recheck_test_frame(
            stack,
            frame,
            model,
            commands,
            accept_green,
            continuous,
            max_attempts,
            skip_implementation=skip_implementation,
            git_cfg=git_cfg,
        )
        return

    # frame.status == "done" - shouldn't normally be seen fresh off disk
    # (WRITE_TEST/the phase-check above both cascade into this same step()
    # call rather than persisting a "done" frame across invocations), but
    # handled the same way regardless of how we got here.
    do_pop(frame, continuous, model, step_models, commands, config_path, git_cfg)


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

    model, step_models = lib.resolve_step_models(config_path, args.model)
    git_cfg = lib.load_git_config(config_path)

    while True:
        step(
            model, commands, args.continuous, config_path,
            step_models=step_models,
            accept_green=args.accept_green, accept_manual=args.accept_manual,
            accept_no_test=args.accept_no_test,
            manual_test=args.manual_test,
            manual_test_refs=args.manual_test_ref,
            skip_test=args.skip_test,
            skip_implementation=args.skip_implementation,
            max_attempts=args.max_attempts,
            git_cfg=git_cfg,
        )


if __name__ == "__main__":
    main()
