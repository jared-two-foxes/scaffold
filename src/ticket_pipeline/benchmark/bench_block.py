#!/usr/bin/env python3
"""
bench_block - run exactly one pipeline_lib block once, against fixed
fixture inputs, and report machine-readable pass/fail/cost/duration.

Always invoked as a subprocess by bench.py, with cwd already set to an
isolated git worktree (so this script's own relative-path tool reads -
libs/virtual_assistant_api/... - resolve against a real checkout, and
concurrent trials never collide on .ticket.md/.tdd-plan.md/.gap-plan.md
since each worktree is its own directory).

Deliberately does not import check-ticket.py/resolve-ticket.py - those
chain blocks together (plan's real output feeds narrow). Benchmarking a
single block in isolation means feeding it a fixed fixture instead of a
live upstream result, which is the whole point: it separates "is this
model good at narrow" from "did the model before it hand narrow a good
or bad plan".

Grading for plan/narrow is a hardcoded text heuristic per (ticket,
block) pair - good enough to bulk-run many trials and get a pass-rate
signal, but it's pattern matching on the plan text, not a real
compiler/test check. Treat the reason string as a hint to spot-check,
not ground truth.

Grading for test-criterion is a real check, not a heuristic: it
compiles the test the model wrote (cargo test --no-run) and then runs
it scoped (cargo test {filter}), requiring it to fail (red) - a
criterion that isn't implemented yet should produce a failing test, not
a compile error and not an accidental pass. This is slower (real cargo
invocations) but answers the actual question instead of guessing from
text.

implement-criterion benchmarking is retired: the criteria-stack
pipeline (push_ticket.py/next_step.py) has no AI-driven per-criterion
implement step to benchmark - next_step.py always pauses for a human to
implement.

Prints exactly one line of JSON to stdout:
  {"success": bool, "reason": str, "duration_s": float,
   "cost_usd": float, "tokens_total": int, "error": str|null}
"""

import argparse
import json
import sys
import time
from pathlib import Path

from ..lib import ai_client, pipeline_lib as lib


def grade_sa452_no_file_split(plan_text: str) -> tuple[bool, str]:
    """
    SA-452's known failure mode: a flash/budget-tier model takes the
    ticket's literal "create xero_webhook_config.rs" wording at face
    value instead of noticing the codebase already implements both
    structs in infra/accounting_webhooks.rs (alongside the sibling
    QuickbooksWebhookConfig). PASS means the plan/gap-plan keeps the fix
    anchored in the existing file; FAIL means it proposes splitting into
    new per-struct files.
    """
    lowered = plan_text.lower()
    proposes_new_file = (
        "xero_webhook_config.rs" in lowered or "quickbooks_webhook_config.rs" in lowered
    )
    mentions_existing_file = "accounting_webhooks.rs" in lowered
    if proposes_new_file:
        return (
            False,
            "plan proposes new xero_webhook_config.rs/quickbooks_webhook_config.rs files",
        )
    if not mentions_existing_file:
        return False, "plan never references the existing accounting_webhooks.rs"
    return True, "plan anchors the fix in the existing accounting_webhooks.rs"


def grade_test_criterion_compiles_and_red(
    file_paths: list[str], qualified_test_names: list[str]
) -> tuple[bool, str]:
    """
    Real correctness check (not a text heuristic) for any ticket: the
    test(s) the model wrote must (a) compile as part of the whole test
    binary, and (b) each fail when run scoped to just that test - the
    criterion they cover isn't implemented yet in the fixture's gap
    plan, so every correct test must be red. A test that doesn't
    compile is a Tester bug; any test that passes green means it didn't
    actually exercise the missing behavior (the gap "didn't reproduce").
    Almost always a single test; more than one only for a criterion
    needing genuinely separate tests (test-criterion.prompt.md's Step
    3) - all must pass this check, not just one of them.
    """
    commands = lib.load_pipeline_config(Path(lib.PIPELINE_CONFIG_FILE))

    compile_result = lib.run_command(
        commands["test_compile_cmd"], "bench test compile gate"
    )
    if compile_result.returncode != 0:
        tail = (compile_result.stdout + compile_result.stderr)[-1500:]
        return (
            False,
            f"test does not compile (exit {compile_result.returncode}): {tail}",
        )

    red_results = lib.run_scoped_tests(
        qualified_test_names, commands, "bench red check"
    )
    false_greens = [
        n for n, r in zip(qualified_test_names, red_results) if r.returncode == 0
    ]
    if false_greens:
        return False, (
            f"test(s) passed without implementation - gap didn't reproduce "
            f"(false green): {', '.join(false_greens)}"
        )

    test_list = ", ".join(f"{f}::{n}" for f, n in zip(file_paths, qualified_test_names))
    return True, f"test(s) ({test_list}) compile and correctly fail red"


def grade_sa500_standard(plan_text: str) -> tuple[bool, str]:
    """
    SA-500 is the "standard"/easy baseline fixture, deliberately without
    a trap: a single new field on an existing struct
    (RateLimitConfig.webhook_retry_rate_limit), following an established
    pattern exactly, with no ambiguity about which file it belongs in.
    Where SA-452 measures whether a model resolves a stale/misleading
    ticket path against reality, SA-500 measures the baseline case - does
    the model do the obviously-right thing when nothing is trying to
    mislead it. A FAIL here is a much stronger signal than a FAIL on
    SA-452: it means the model struggled with a plain, unambiguous task.
    """
    lowered = plan_text.lower()
    mentions_target_file = "rate_limit_config.rs" in lowered
    mentions_env_var = "webhook_retry_rate_limit" in lowered
    proposes_new_file = (
        "new file" in lowered or "create" in lowered
    ) and "rate_limit_config.rs" not in lowered
    if not mentions_target_file:
        return False, "plan never references the existing rate_limit_config.rs"
    if not mentions_env_var:
        return (
            False,
            "plan doesn't mention webhook_retry_rate_limit/WEBHOOK_RETRY_RATE_LIMIT at all",
        )
    if proposes_new_file:
        return (
            False,
            "plan proposes a new file instead of extending rate_limit_config.rs",
        )
    return (
        True,
        "plan correctly extends the existing RateLimitConfig in rate_limit_config.rs",
    )


def grade_sa501_debug_redaction_named(plan_text: str) -> tuple[bool, str]:
    """
    SA-501's trap: EmailConfig's Debug impl is hand-written (not
    #[derive(Debug)]), so a plan that adds postmark_signing_secret
    without explicitly naming the Debug impl update would leak the
    secret in cleartext by default - there's no automatic redaction to
    fall back on the way there would be with a derived impl. This is a
    different failure shape than SA-452 (omission within one file's
    existing-but-easy-to-miss manual code, not file fragmentation).
    """
    lowered = plan_text.lower()
    mentions_target_file = "email_config.rs" in lowered
    mentions_field = "postmark_signing_secret" in lowered
    mentions_debug_update = "debug" in lowered and (
        "redact" in lowered or "[redacted]" in lowered
    )
    if not mentions_target_file:
        return False, "plan never references the existing email_config.rs"
    if not mentions_field:
        return (
            False,
            "plan doesn't mention postmark_signing_secret/POSTMARK_SIGNING_SECRET at all",
        )
    if not mentions_debug_update:
        return (
            False,
            "plan adds the field but never names updating EmailConfig's Debug impl for redaction",
        )
    return (
        True,
        "plan adds the field and explicitly calls out updating the hand-written Debug impl",
    )


def grade_sa502_already_implemented(plan_text: str) -> tuple[bool, str]:
    """
    SA-502's trap: the ticket describes validation behavior
    (quote_resend_rate_limit's env var parsing) that's already fully
    implemented via the shared parse_u32_or_default helper, with
    existing tests. PASS means the plan recognizes this and proposes no
    new implementation; FAIL means it invents new validation logic, a
    new config struct, or duplicate parsing instead of recognizing the
    real, already-satisfied location.

    `narrow`/`plan-narrow` shapes (unlike `plan`) can correctly emit "(none
    - all criteria satisfied)" with no file/symbol mentioned at all when
    every criterion is PASS - the maximally correct answer for this
    fixture's trap, not a missed reference. Check for that before the
    mentions_target gate, which assumes a plan that lists criteria (and
    so always names something), the way `plan`'s output always does.
    """
    lowered = plan_text.lower()
    if "none - all criteria satisfied" in lowered:
        return True, "plan/gap-plan correctly recognizes nothing remains unsatisfied"
    mentions_target = (
        "rate_limit_config.rs" in lowered or "quote_resend_rate_limit" in lowered
    )
    recognizes_done = any(
        phrase in lowered
        for phrase in (
            "already implement",
            "already satisf",
            "no new",
            "no production code",
            "no changes needed",
        )
    )
    proposes_new_work = (
        "new struct" in lowered or "new file" in lowered or "new validation" in lowered
    )
    if not mentions_target:
        return (
            False,
            "plan never references the existing rate_limit_config.rs/quote_resend_rate_limit",
        )
    if proposes_new_work:
        return (
            False,
            "plan proposes new validation logic/struct/file instead of recognizing existing behavior",
        )
    if not recognizes_done:
        return (
            False,
            "plan references the right field but never says this is already implemented",
        )
    return (
        True,
        "plan correctly recognizes quote_resend_rate_limit's validation is already implemented",
    )


GRADERS = {
    ("sa452", "plan"): grade_sa452_no_file_split,
    ("sa452", "narrow"): grade_sa452_no_file_split,
    ("sa452", "plan-narrow"): grade_sa452_no_file_split,
    # sa452-proposed: same codebase claim (accounting_webhooks.rs, not a
    # split-file layout), just a ticket revised by review-ticket.py/
    # propose-ticket-edit.py to remove the stale file-list trap - same
    # grader applies unchanged.
    ("sa452-proposed", "plan"): grade_sa452_no_file_split,
    ("sa452-proposed", "narrow"): grade_sa452_no_file_split,
    ("sa500", "plan"): grade_sa500_standard,
    ("sa500", "narrow"): grade_sa500_standard,
    ("sa500", "plan-narrow"): grade_sa500_standard,
    ("sa501", "plan"): grade_sa501_debug_redaction_named,
    ("sa501", "narrow"): grade_sa501_debug_redaction_named,
    ("sa501", "plan-narrow"): grade_sa501_debug_redaction_named,
    ("sa501-proposed", "plan"): grade_sa501_debug_redaction_named,
    ("sa501-proposed", "narrow"): grade_sa501_debug_redaction_named,
    ("sa502", "plan"): grade_sa502_already_implemented,
    ("sa502", "narrow"): grade_sa502_already_implemented,
    ("sa502", "plan-narrow"): grade_sa502_already_implemented,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--block",
        required=True,
        choices=["plan", "narrow", "plan-narrow", "test-criterion"],
    )
    parser.add_argument("--ticket-name", required=True, help="Grader key, e.g. sa452")
    parser.add_argument("--ticket-file", required=True, type=Path)
    parser.add_argument(
        "--plan-file",
        type=Path,
        help="Required for --block narrow/test-criterion",
    )
    parser.add_argument("--criterion", help="Required for --block test-criterion")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    if args.block in ("plan", "narrow", "plan-narrow"):
        grader = GRADERS.get((args.ticket_name, args.block))
        if grader is None:
            print(
                json.dumps(
                    {"error": f"no grader for ({args.ticket_name}, {args.block})"}
                )
            )
            sys.exit(1)

    ticket_content = args.ticket_file.read_text(encoding="utf-8")

    start = time.monotonic()
    error = None
    success = False
    reason = ""
    try:
        # ticket_id=args.ticket_name below is a fixture label ("sa452"),
        # not a real Linear ticket - but .pipeline-log.jsonl doesn't care
        # which, and tagging bench trials with it means bench runs get
        # the same per-block token/cost accounting as real pipeline runs,
        # broken out by fixture, for free.
        if args.block == "plan":
            output_text = lib.run_plan_step(
                ticket_content, args.model, ticket_id=args.ticket_name
            )
            success, reason = grader(output_text)
        elif args.block == "narrow":
            if not args.plan_file:
                raise ValueError("--plan-file is required for --block narrow")
            plan_content = args.plan_file.read_text(encoding="utf-8")
            output_text = lib.run_narrow_step(
                ticket_content, plan_content, args.model, ticket_id=args.ticket_name
            )
            success, reason = grader(output_text)
        elif args.block == "plan-narrow":
            output_text = lib.run_plan_narrow_step(
                ticket_content, args.model, ticket_id=args.ticket_name
            )
            success, reason = grader(output_text)
        else:  # test-criterion
            if not args.plan_file:
                raise ValueError(
                    "--plan-file (the gap plan) is required for --block test-criterion"
                )
            if not args.criterion:
                raise ValueError("--criterion is required for --block test-criterion")
            plan_content = args.plan_file.read_text(encoding="utf-8")
            plan_context = lib.extract_plan_context_for_criterion(
                args.criterion, plan_content
            )
            file_paths, qualified_test_names = lib.run_test_for_criterion(
                args.criterion, plan_context, args.model, ticket_id=args.ticket_name
            )
            success, reason = grade_test_criterion_compiles_and_red(
                file_paths, qualified_test_names
            )
    except SystemExit as e:
        error = f"block aborted (exit {e.code}) - see die() output above"
    except (
        Exception
    ) as e:  # noqa: BLE001 - report any failure as a graded trial, not a crash
        error = f"{type(e).__name__}: {e}"
    duration_s = time.monotonic() - start

    cost_usd, unpriced = ai_client.usage.total_cost_usd()
    result = {
        "duration_s": round(duration_s, 2),
        "cost_usd": round(cost_usd, 4),
        "tokens_total": ai_client.usage.total_tokens,
        "unpriced_models": unpriced,
        "error": error,
    }
    if error:
        result["success"] = False
        result["reason"] = error
    else:
        result["success"] = success
        result["reason"] = reason

    # pipeline_lib's own run_*_step calls print progress lines (and
    # render_markdown output) to stdout throughout the run - this marker
    # lets bench.py find our result line without needing to silence them.
    print("===BENCH_RESULT===")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
