"""
pipeline_lib - shared step library for push_ticket.py and next_step.py
(the criteria-stack pipeline), plus the benchmark/bench.py and
benchmark/bench_block.py harness.

push_ticket.py seeds a `.criteria-stack.json` work-queue (fetch -> plan
-> narrow -> one CriterionFrame per remaining acceptance criterion).
next_step.py reads that stack and advances it one step at a time -
writing a test, pausing for a human to implement, detecting green
mechanically, or running the full ticket-validation gate (narrow again,
lint, full test suite, review) once a ticket's frames are exhausted.
Both scripts keep their own argparse setup and main() control flow;
everything else - prompt builders, the run_with_tools call shapes, the
stack's own I/O, the build/test command plumbing - lives here.

Functions named build_*_prompt() are pure string builders. Functions
named run_*_step()/run_*_gate() wrap a build_*_prompt() call with its
run_with_tools call, error handling (die() on AIError/PipelineAbort),
result validation, and console rendering - the same block every caller
of that step needs, so the only thing left at each CLI script's call
site is the step name and the variables it threads to the next step.

This module previously backed a wider set of scripts (check-ticket.py,
write-tests.py, resolve-ticket.py, implement-tests.py,
validate-and-review.py, tdd-pipeline.py) that implemented pieces of the
same fetch-plan-test-implement-review flow the criteria stack now owns
directly. Those scripts, and the functions that existed only to serve
them (the markdown "Test:" annotation mechanism, AI-driven
per-criterion implementation, the whole-plan non-per-criterion test/
implement/coverage steps, and the STALE_FILES/RESETTABLE_FILES generic
cleanup lists), are retired.
"""

import json
import logging
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import tomllib
import urllib.error
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from importlib.resources import files

from . import ai_client
from .ai_client import AIError, run_with_tools
from . import fetch_ticket as ticket_source
from . import render
from .render import render_markdown
from . import repo_context
from . import tools
from . import toolchains
from . import verbosity
from .retry import RetryPolicy

log = verbosity.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = files("ticket_pipeline") / "prompts"
TICKET_FILE = Path(".ticket.md")
PLAN_FILE = Path(".tdd-plan.md")
UPDATED_PLAN_FILE = Path(".updated-plan.md")
PIPELINE_CONFIG_FILE = Path(".dev-pipeline.toml")

# Transient scratch artifacts, not cross-invocation state: push_ticket
# writes these once to seed the stack, and next_step's TICKET_VALIDATE
# phase rewrites them fresh at validation time. .criteria-stack.json
# (CRITERIA_STACK_FILE, below) is the only file either script trusts
# across invocations - nothing here is read back by a later run the way
# the old STALE_FILES/RESETTABLE_FILES scripts relied on. Cleanup of
# these is push_ticket.py's own explicit responsibility (see its guard-
# then-cleanup ordering), not a shared generic list - a shared list
# serving callers with different lifecycles is exactly what caused a
# real ordering bug in an earlier draft of this pipeline (a cleanup step
# deleting the stack file before a re-entrancy guard could check it).
GAP_PLAN_FILE = Path(".gap-plan.md")
PIPELINE_LOG_FILE = Path(".pipeline-log.jsonl")

# The pipeline's canonical work-queue and sole cross-invocation source
# of truth - see CriterionFrame/load_stack/save_stack below.
CRITERIA_STACK_FILE = Path(".criteria-stack.json")

# Ledger of criteria a mechanical grounding check rejected before they
# ever became a stack frame - see verify_criterion_grounding/
# filter_grounded_frames. Append-only, never read back into the stack
# itself; its existence is what makes a decline sticky across repeated
# next_step/push_ticket calls (see DeclinedCriterion/is_declined).
DECLINED_CRITERIA_FILE = Path(".declined-criteria.json")

# Per-ticket git-workflow state (base_branch recorded at push-ticket time,
# read at TICKET_VALIDATE merge/PR time). A sidecar rather than a frame
# field because base_branch is per-ticket, not per-criterion - and the
# sentinel frame that would carry it is popped *before* the merge runs.
# Gitignored like every other pipeline state file (see
# ensure_gitignore_entries) so `git reset --hard` never touches it.
GIT_STATE_FILE = Path(".pipeline-git-state.json")
SCAFFOLD_TEMP_DIR = Path(".scaffold")

PLAN_PROMPT_FILE = PROMPTS_DIR / "plan.prompt.md"
NARROW_PROMPT_FILE = PROMPTS_DIR / "narrow-plan.prompt.md"
PLAN_NARROW_PROMPT_FILE = PROMPTS_DIR / "plan-narrow.prompt.md"
REVIEW_PROMPT_FILE = PROMPTS_DIR / "review-singlepass.prompt.md"
TEST_CRITERION_PROMPT_FILE = PROMPTS_DIR / "test-criterion.prompt.md"
TEST_REFINE_PROMPT_FILE = PROMPTS_DIR / "test-refine.prompt.md"
TEST_QUALITY_REVIEW_PROMPT_FILE = PROMPTS_DIR / "review-test-quality.prompt.md"
RECHECK_CRITERION_PROMPT_FILE = PROMPTS_DIR / "recheck-criterion.prompt.md"
EXPLORE_CRITERION_PROMPT_FILE = PROMPTS_DIR / "explore-criterion.prompt.md"

# Host OS name, injected into every test-criterion and test-quality
# review prompt so the Tester/Reviewer write tests that compile on the
# platform actually running the pipeline (see test-criterion.prompt.md's
# Platform portability rule and review-test-quality.prompt.md's
# platform-specific-API check).
_HOST_PLATFORM_NOTE = f"The host platform is {platform.system()} (tests must compile and run on this platform)."

# Always injected. Instructs the planner to self-clarify before planning,
# since none of these scripts have a path for a human to answer follow-up
# questions mid-run.
AUTO_PREAMBLE = (
    "Before producing the TDD plan, identify any ambiguities or missing details "
    "in the ticket. For each one, state the question and then answer it with your "
    "best inference from the ticket context. Then produce the full TDD plan.\n\n"
)

DEFAULT_MODEL = "opencode:gpt-5.4-mini"  # ultimate fallback for unlested steps

DEFAULT_STEP_MODELS: dict[str, str] = {
    "review": "opencode:claude-sonnet-4-6",
    "plan": "opencode:claude-sonnet-4-6",
    "narrow": "opencode:claude-sonnet-4-6",
}

FEEDBACK_READY_STATUS = "feedback-ready"
FEEDBACK_MAX_RETRIES = 3
FEEDBACK_TARGET_TESTER = "tester"
FEEDBACK_TARGET_IMPLEMENTOR = "implementor"
FEEDBACK_TARGET_HUMAN = "human"
FEEDBACK_TARGETS = frozenset(
    {
        FEEDBACK_TARGET_TESTER,
        FEEDBACK_TARGET_IMPLEMENTOR,
        FEEDBACK_TARGET_HUMAN,
    }
)

USER_CONFIG_FILE = Path.home() / ".config" / "scaffold.toml"


def resolve_step_models(
    project_config_path: Path, cli_model: str | None
) -> tuple[str, dict[str, str]]:
    """
    Resolves per-step model overrides across three config levels plus
    the CLI flag. Returns (fallback_model, step_models) where step_models
    is a merged dict of per-step overrides and fallback_model is used for
    any step not in step_models.

    Precedence (highest wins):
      1. --model CLI flag  → cli_model for ALL steps, step_models = {}
      2. project .dev-pipeline.toml [step_models]
      3. user ~/.config/scaffold.toml [step_models]
      4. DEFAULT_STEP_MODELS (app-level per-step defaults)
      5. DEFAULT_MODEL (ultimate fallback for unlisted steps)
    """
    if cli_model is not None:
        return cli_model, {}

    # Start with app-level defaults (lowest config priority)
    step_models = dict(DEFAULT_STEP_MODELS)

    # Merge user-level overrides
    step_models.update(load_step_models(USER_CONFIG_FILE))

    # Merge project-level overrides (highest config priority)
    step_models.update(load_step_models(project_config_path))

    return DEFAULT_MODEL, step_models


# Per-toolchain defaults (rust/cargo, bazel, cmake/ctest, sveltekit/npm,
# generic typescript/npm), used only if no project-local config file is
# present - see toolchains.py. Detected lazily (see get_toolchain) rather
# than at import time, and cached, since detection reads the project
# root's marker files (Cargo.toml, WORKSPACE, CMakeLists.txt,
# svelte.config.*, package.json) relative to cwd - the same cwd
# convention every other relative path in this module already relies on.
#
# test_filter_cmd is used by next_step.py's per-criterion phases
# (run_scoped_test) - {filter} is substituted with the qualified test
# name recorded for that criterion's frame. Compiling can't be scoped to
# one test (a test binary compiles everything in it regardless of which
# test you'll filter at runtime), so there's no filtered equivalent of
# test_compile_cmd - only the run is ever scoped.
#
# fmt_fix_cmd/clippy_fix_cmd/fmt_check_cmd/clippy_cmd are used only by
# next_step.py's TICKET_VALIDATE phase (run_lint_gate) - lint/style
# checks run once, after every criterion in a ticket is implemented and
# passing, right before code review - not as acceptance-criteria
# evidence (see extract_plan_commands). Names
# are inherited from the original Rust-only defaults (fmt/clippy) but are
# generic format-fix/lint-fix/format-check/lint-check slots regardless of
# toolchain - not worth a breaking key rename across every existing
# .dev-pipeline.toml for what's just a label. The *_fix_cmd entries
# attempt the mechanical, no-judgment-call fix before the
# *_check_cmd/clippy_cmd gate gets to fail anything.
_toolchain_cache: toolchains.Toolchain | None = None
_toolchain_detected = False


def get_toolchain() -> toolchains.Toolchain:
    """
    Detects the project's toolchain once (by marker file at cwd) and
    caches it for the rest of the process. Falls back to the Rust/cargo
    defaults if nothing is detected, preserving this module's original
    behavior for projects with no recognized marker file and no
    .dev-pipeline.toml override.
    """
    global _toolchain_cache, _toolchain_detected
    if not _toolchain_detected:
        _toolchain_cache = toolchains.detect_toolchain()
        _toolchain_detected = True
    return _toolchain_cache or toolchains.RUST


def extract_test_output_signal(
    output: str, pattern: str, context_lines: int = 15
) -> str:
    """
    Filter a test run's raw stdout+stderr down to the lines a human
    actually needs to see why it's red, using the current toolchain's
    test_output_signal_pattern (see toolchains.py - built for exactly
    this, previously unused). Necessary because test_filter_cmd's
    default (e.g. cargo's "cargo test {filter}") isn't scoped to a
    single test binary - it's a name filter applied across every
    integration test file in the project, so the raw output is mostly
    "Running tests\\X.rs (...)" binary-listing noise for files that
    have nothing to do with the criterion at hand, with the actual
    panic/assertion detail buried or scrolled past entirely.

    Keeps each matching line plus up to `context_lines` following lines
    (a panic message's expected-vs-actual detail follows the "panicked
    at"/"---- test_name stdout ----" line that matches, not the line
    itself) and one line of leading context, collapsing gaps between
    kept ranges with a "..." separator so multiple failures in the same
    run all show, without the noise between them. Falls back to the
    full, untouched output if the pattern matches nothing - a test
    runner this heuristic doesn't fit should still show *something*
    rather than silently discarding real output.
    """
    if not output.strip():
        return output
    lines = output.splitlines()
    compiled = re.compile(pattern)
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if compiled.search(line):
            keep.update(range(max(0, i - 1), min(len(lines), i + context_lines + 1)))
    if not keep:
        return output

    result_lines = []
    prev_idx = None
    for idx in sorted(keep):
        if prev_idx is not None and idx > prev_idx + 1:
            result_lines.append("...")
        result_lines.append(lines[idx])
        prev_idx = idx
    return "\n".join(result_lines)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def die(msg: str) -> None:
    log.error("error: %s", msg)
    log.error("-- Token usage so far: %s", ai_client.usage)
    sys.exit(1)


def load_prompt_body(prompt_file: Path) -> str:
    """
    Read a prompts/*.prompt.md file and return its role/steps/rules body,
    stripped of the YAML frontmatter and the trailing '## Task' example
    block. That block is written for VS Code's chat composer - #file:
    autocomplete and ${workspaceFolder} - which has no meaning here.

    Reading the file fresh on every run means edits to the prompt
    templates take effect here without touching any of the CLI scripts.
    """
    if not prompt_file.exists():
        die(f"Prompt template not found: {prompt_file}")
    text = prompt_file.read_text(encoding="utf-8")

    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            text = text[end + len("\n---\n") :]

    body, _, _ = text.partition("\n## Task")
    body = body.rstrip()
    if body.endswith("---"):
        body = body[:-3].rstrip()
    return body


def remove_scratch_files(paths: tuple[Path, ...]) -> None:
    """
    push_ticket.py's own explicit cleanup step, called only after its
    re-entrancy/--force guard has already passed - see its module
    docstring for why this isn't a shared generic list run ahead of that
    guard the way the old STALE_FILES mechanism was.
    """
    for path in paths:
        if path.exists():
            path.unlink()
            log.info("-- Removed %s from a previous run", path)


# ---------------------------------------------------------------------------
# Diagnostic log - next_step.py only. Purely informational: "why did
# this fail last time" for a human reading the next invocation's output.
# Resumption decisions never consult this - phase detection always
# re-inspects real output state (the stack file, a scoped test run) for
# that, so a stale or missing log entry can't mislead the pipeline into
# the wrong resume point, only a human glancing at the wrong "last
# failure" note.
# ---------------------------------------------------------------------------


def log_event(
    block: str,
    status: str,
    error: str | None = None,
    criterion: str | None = None,
    ticket: str | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """
    tokens_prompt/tokens_completion/cost_usd are a *delta* attributable
    to this one event, not a running total - callers computing one use
    _usage_snapshot()/_usage_delta() (see below) around just the work
    this event reports on. None (not 0) for any event with no AI call
    behind it (a lint/build/test-suite gate failure, for instance) -
    0 would falsely claim "measured, cost nothing" for a block that was
    never priced at all.
    """
    entry = {
        "block": block,
        "ticket": ticket,
        "criterion": criterion,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "error": error,
        "tokens_prompt": tokens_prompt,
        "tokens_completion": tokens_completion,
        "cost_usd": cost_usd,
    }
    with PIPELINE_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_feedback_event(
    status: str,
    feedback_text: str,
    criterion: str | None = None,
    ticket: str | None = None,
    target: str | None = None,
) -> None:
    """
    Specialized wrapper for feedback log entries so call sites don't need
    to thread user feedback through log_event's generic `error` parameter.
    """
    prefix = f"target={target}: " if target else ""
    log_event(
        "feedback",
        status,
        error=prefix + feedback_text,
        criterion=criterion,
        ticket=ticket,
    )


def die_with_log(
    block: str,
    msg: str,
    criterion: str | None = None,
    ticket: str | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """
    Like die(), but also records the failure to the diagnostic log first
    - used by every push_ticket.py/next_step.py failure path. Token
    fields are optional (see log_event) - most die_with_log call sites
    are gate failures (lint/build/test-suite) with no AI cost to report;
    a caller that does have a delta on hand (e.g. an AIError raised by a
    step this function wraps) should pass it through rather than losing
    it at the point of failure.
    """
    log_event(
        block,
        "failed",
        error=msg,
        criterion=criterion,
        ticket=ticket,
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        cost_usd=cost_usd,
    )
    die(msg)


@dataclass
class _UsageSnapshot:
    """
    A point-in-time read of ai_client.usage's running totals - UsageTracker
    itself only exposes cumulative-since-process-start totals, not deltas,
    so a block wanting "what did just this call cost" snapshots before and
    after and subtracts. Safe because these totals only ever grow across a
    single-process run (see ai_client.UsageTracker) - never reset, never
    decrease - so a later-minus-earlier subtraction is exactly what was
    added in between, with no risk of going negative.
    """

    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def _usage_snapshot() -> _UsageSnapshot:
    cost, _unpriced = ai_client.usage.total_cost_usd()
    return _UsageSnapshot(
        ai_client.usage.prompt_tokens, ai_client.usage.completion_tokens, cost
    )


def _usage_delta(start: _UsageSnapshot) -> dict:
    """Returns a dict of tokens_prompt/tokens_completion/cost_usd kwargs
    ready to splat into log_event/die_with_log, covering everything
    ai_client.usage has accumulated since `start` was taken."""
    now = _usage_snapshot()
    return {
        "tokens_prompt": now.prompt_tokens - start.prompt_tokens,
        "tokens_completion": now.completion_tokens - start.completion_tokens,
        "cost_usd": round(now.cost_usd - start.cost_usd, 6),
    }


AI_STEP_MAX_ATTEMPTS = 3
AI_STEP_RETRY_BACKOFF_BASE_S = 5.0


def run_ai_step_with_retry(
    step_fn: Callable[[], object],
    label: str,
    criterion: str | None = None,
    ticket: str | None = None,
    max_attempts: int = AI_STEP_MAX_ATTEMPTS,
) -> object:
    """
    Calls step_fn() - a zero-arg closure performing one run_with_tools
    round trip and returning its parsed result - retrying only on
    AIError. tools.PipelineAbort (ask_user_prompt) propagates immediately,
    unretried: that's a deliberate model signal ("I need a human"), not a
    transient infra failure, and retrying changes nothing about a
    model's decision to ask for clarification. run_command is not a
    PipelineAbort - calling it returns a recoverable tool error to the
    model instead (see tools.py), so it never reaches this layer at all.

    Each retry calls step_fn() completely fresh (new message history
    inside run_with_tools); any written_paths/changed_files accumulator
    a caller threads into its closure must be cleared by step_fn itself
    at the top of each call, since a failed attempt's partial writes
    must not carry over into the next attempt's result.

    Every outcome is logged with its own token/cost delta (see
    _usage_snapshot/_usage_delta), not just failures - this is the data
    a later `.pipeline-log.jsonl` pass needs to compare models per
    block/ticket empirically instead of by feel:
      - "retry": one failed attempt's own delta, before sleeping with
        exponential backoff (matches ai_client's own transient-HTTP-retry
        backoff shape).
      - "success": step_fn() returned - the *cumulative* delta across
        every attempt this call made (including any that failed and were
        retried), so one block invocation nets exactly one success
        entry with its true total cost, not one entry per attempt.
      - "exhausted": every attempt failed and max_attempts is used up -
        same cumulative delta as "success" would have carried, logged
        here (not left for the call site) since only this function
        tracked it across every attempt. This helper still never itself
        decides to die - it logs, then re-raises the last AIError
        untouched, same as before; the call site's existing
        except/die_with_log handling is unchanged.
    """
    attempt = 0
    call_start = _usage_snapshot()
    while True:
        attempt += 1
        attempt_start = _usage_snapshot()
        try:
            result = step_fn()
        except ai_client.StepBudgetExceeded:
            raise
        except AIError as e:
            if attempt >= max_attempts:
                log_event(
                    label,
                    "exhausted",
                    error=str(e),
                    criterion=criterion,
                    ticket=ticket,
                    **_usage_delta(call_start),
                )
                raise
            log_event(
                label,
                "retry",
                error=str(e),
                criterion=criterion,
                ticket=ticket,
                **_usage_delta(attempt_start),
            )
            backoff_s = AI_STEP_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
            log.warning(
                "-- %s: attempt %d/%d failed (%s), retrying in %.0fs ...",
                label,
                attempt,
                max_attempts,
                e,
                backoff_s,
            )
            time.sleep(backoff_s)
            continue
        log_event(
            label,
            "success",
            criterion=criterion,
            ticket=ticket,
            **_usage_delta(call_start),
        )
        return result


def show_last_failure() -> None:
    """
    Prints the most recent log entry if it was a failure, so a re-entrant
    run immediately shows "what broke last time" instead of making the
    user scroll back through old terminal output.
    """
    if not PIPELINE_LOG_FILE.exists():
        return
    lines = PIPELINE_LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        return
    if last.get("status") != "failed":
        return
    where = last.get("block", "?")
    if last.get("criterion"):
        where += f" (criterion: {last['criterion']})"
    log.warning("-- Note: last attempt failed at %s: %s", where, last.get("error"))


# ---------------------------------------------------------------------------
# Generic block/walk helper - used for the linear planning pipeline
# (fetch -> plan -> narrow). The per-criterion implementation loop is
# NOT forced through this same walker - it has a real early-exit branch
# (test unexpectedly green at the red-check means the gap didn't
# reproduce; skip straight to the next criterion) that doesn't fit "run,
# then must satisfy check" cleanly. That loop applies the identical
# check-before-run principle by hand instead.
# ---------------------------------------------------------------------------


@dataclass
class Block:
    name: str
    check: Callable[[], bool]
    run: Callable[[], None]


def walk(blocks: list[Block]) -> None:
    for block in blocks:
        if block.check():
            log.info("-- %s: already satisfied, skipping", block.name)
            continue
        log.info("-- %s: running ...", block.name)
        block.run()
        if not block.check():
            die_with_log(
                block.name,
                f"{block.name} ran but its postcondition still isn't satisfied.",
            )


def build_planning_blocks(
    ticket_id: str,
    model: str,
    step_models: dict[str, str] | None = None,
    ticket_file_in: Path | None = None,
) -> list[Block]:
    """
    The fetch -> plan -> narrow Block list used by push_ticket.py to seed
    the stack, and by next_step.py's TICKET_VALIDATE phase to re-fetch
    and re-plan before its safety-net re-narrow. Re-entrant: each block
    is skipped if its file already exists and passes its validity check,
    so calling this against a ticket that already has a valid
    .gap-plan.md from an earlier run does nothing here.

    ticket_file_in: if given, the fetch_ticket block reads this local
    file instead of calling Linear - e.g. a proposed revision from
    propose-ticket-edit.py that hasn't been pushed to Linear yet. Either
    way the content still gets written to TICKET_FILE (.ticket.md) -
    that's still this pipeline's one canonical ticket-state file; this
    only changes where its content originally comes from.
    """
    step_models = step_models or {}
    plan_model = step_models.get("plan", model)
    narrow_model = step_models.get("narrow", model)

    def fetch_ticket_content() -> str:
        if ticket_file_in is not None:
            if not ticket_file_in.is_file():
                die(f"--ticket-file-in {ticket_file_in} not found.")
            return ticket_file_in.read_text(encoding="utf-8")
        return fetch_ticket_text(ticket_id)

    return [
        Block(
            name="fetch_ticket",
            check=lambda: TICKET_FILE.is_file()
            and bool(TICKET_FILE.read_text(encoding="utf-8").strip()),
            run=lambda: tools.write_file_block(str(TICKET_FILE))(
                fetch_ticket_content()
            ),
        ),
        Block(
            name="planner",
            check=lambda: PLAN_FILE.is_file()
            and "## Acceptance Criteria" in PLAN_FILE.read_text(encoding="utf-8"),
            run=lambda: run_plan_step(
                TICKET_FILE.read_text(encoding="utf-8"), plan_model, ticket_id=ticket_id
            ),
        ),
        Block(
            name="narrower",
            check=lambda: GAP_PLAN_FILE.is_file()
            and "## Acceptance Criteria" in GAP_PLAN_FILE.read_text(encoding="utf-8"),
            run=lambda: run_narrow_step(
                TICKET_FILE.read_text(encoding="utf-8"),
                PLAN_FILE.read_text(encoding="utf-8"),
                narrow_model,
                ticket_id=ticket_id,
            ),
        ),
    ]


def find_verdict(text: str, tokens_by_priority: list[str]) -> str | None:
    """
    Look for the first matching verdict token, checking more specific
    tokens before their substrings (e.g. INADEQUATE before ADEQUATE) -
    callers must order tokens_by_priority accordingly.
    """
    for token in tokens_by_priority:
        if token in text:
            return token
    return None


def load_pipeline_config(config_path: Path) -> dict:
    toolchain = get_toolchain()
    commands = dict(toolchain.commands)
    if not config_path.exists():
        log.info(
            "-- Detected toolchain: %s. No %s found, using its defaults: %s",
            toolchain.name,
            config_path,
            commands,
        )
        return commands

    log.info(
        "-- Detected toolchain: %s. Loading overrides from %s ...",
        toolchain.name,
        config_path,
    )
    with config_path.open("rb") as f:
        data = tomllib.load(f)

    # Keys outside the toolchain's command table that this module
    # reads separately (step_models via load_step_models; smoke_cmd via
    # load_smoke_cmd; the git_workflow.* keys via load_git_config). All
    # other top-level keys stay unknown-key-rejected so a typo in a
    # toolchain command name is still caught loudly.
    _ALLOWED_EXTRA_KEYS = {"step_models", "smoke_cmd", "retry"} | set(
        GitConfig.__annotations__
    )
    unknown = set(data) - set(toolchain.commands) - _ALLOWED_EXTRA_KEYS
    if unknown:
        die(
            f"{config_path}: unknown key(s) {sorted(unknown)}. "
            f"Allowed: {sorted(toolchain.commands)} plus "
            f"{sorted(_ALLOWED_EXTRA_KEYS)}"
        )
    for key, value in data.items():
        if key not in toolchain.commands:
            continue
        if not isinstance(value, str) or not value.strip():
            die(f"{config_path}: '{key}' must be a non-empty string")
        commands[key] = value
    return commands


def render_step_output(text: str, level: int = logging.DEBUG) -> None:
    """
    Renders a model step's raw markdown output (a plan, gap plan, or
    review verdict), gated by the current log level instead of always
    printing. This is the model's intermediate working output, not a
    script's actual result - the CLI scripts now report that separately
    via render.print_line, unconditionally - so it's hidden by default
    and only shown when --log-level asks for debug or below.
    """
    if log.isEnabledFor(level):
        render_markdown(text)


def run_command(
    command_str: str, label: str, quiet: bool = False
) -> subprocess.CompletedProcess:
    """
    Commands come from the project-local pipeline config, which is
    user-authored and trusted (unlike ticket-derived text) - shlex-split
    and run as an argv list, never shell=True, simply because there's no
    reason to invoke a shell for a fixed toolchain command.

    Output logs at DEBUG on success and ERROR on failure - a passing
    `cargo test`/`clippy` run can be hundreds of lines that add nothing
    once the gate it's feeding into already passed; a failing one is
    exactly the output whoever's watching needs to diagnose it. Visible
    at the default `info` level only on failure, same as before; `debug`
    and below also show it on success.

    quiet=True always logs at DEBUG regardless of returncode - for a
    call where a nonzero exit is the *expected*, not exceptional, result
    (a red-test check) and the caller is about to present a filtered
    version of this same output itself (see next_step.py's
    do_await_impl/lib.extract_test_output_signal) - logging the raw dump
    at ERROR there would just be the same content twice, once as noise.
    """
    command_tokens = shlex.split(command_str)
    log.info("-- Running '%s' (%s) ...", command_str, label)
    result = subprocess.run(command_tokens, capture_output=True, text=True, check=False)
    log_fn = log.debug if quiet or result.returncode == 0 else log.error
    if result.stdout:
        log_fn(result.stdout)
    if result.stderr:
        log_fn(result.stderr)
    return result


# ---------------------------------------------------------------------------
# Ticket fetch
# ---------------------------------------------------------------------------


def fetch_ticket_text(ticket_id: str) -> str:
    """
    Calls fetch_ticket.py's fetch_ticket()/render() directly and returns
    the rendered markdown - no subprocess, no file I/O here. The caller
    pipes the result through tools.write_file_block to persist it, and
    passes the same in-memory string straight into the prompt builders
    instead of re-reading it off disk.
    """
    log.info("-- Fetching ticket %s ...", ticket_id)
    try:
        data = ticket_source.fetch_ticket(ticket_id)
    except urllib.error.HTTPError as e:
        die(f"Ticket fetch failed: HTTP {e.code}: {e.read().decode()}")
    return ticket_source.render(data)


# A ticket naming a real source file almost always does so backtick-
# quoted, either as a full path (`libs/virtual_assistant_api/src/
# stripe_webhook.rs`) or, when referring to something the ticket treats
# as already-familiar (e.g. "follows the existing stripe_webhook.rs
# pattern"), just the bare filename. Both are deliberately conservative
# (dotted extension required) so they don't match stray code
# identifiers or prose.
REFERENCED_PATH_RE = re.compile(r"`([\w./-]+/[\w.-]+\.[A-Za-z]{1,5})`")
REFERENCED_FILENAME_RE = re.compile(r"`([\w-]+\.[A-Za-z]{1,5})`")

# Directories never worth rglob-searching for a bare filename match -
# build output and dependency trees are huge and never what a ticket
# means by a short filename like "lib.rs".
_PREFETCH_SEARCH_PRUNE = {".git", "target", "node_modules", "dist", "build", ".venv"}

# Caps keep a pathological ticket (lots of backtick-quoted paths, or one
# huge file) from blowing up the prompt - this is meant to save the
# model a handful of read_file turns on the files it's overwhelmingly
# likely to want, not to front-load the whole repo.
PREFETCH_MAX_FILES = 12
PREFETCH_MAX_CHARS_PER_FILE = 8_000


def _resolve_bare_filename(name: str) -> str | None:
    """
    Bare filenames (no directory) have to be searched for - returns the
    repo-relative path if exactly one file with this name exists under
    cwd, None if zero or more than one (ambiguous; guessing wrong is
    worse than not prefetching at all).
    """
    matches = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [
            d for d in dirs if d not in _PREFETCH_SEARCH_PRUNE and not d.startswith(".")
        ]
        if name in files:
            matches.append(str(Path(root, name)).removeprefix(".\\").removeprefix("./"))
            if len(matches) > 1:
                return None
    return matches[0] if len(matches) == 1 else None


def extract_referenced_paths(text: str) -> list[str]:
    """
    Pulls candidate file paths out of `text` (a ticket body) - full
    paths via REFERENCED_PATH_RE kept as-is, bare filenames via
    REFERENCED_FILENAME_RE resolved against the repo tree - then keeps
    only the ones that actually exist as files (a ticket mentioning a
    path that doesn't exist, e.g. a file it wants created, is exactly
    the kind of false positive this filters out). Order-preserving,
    deduped.
    """
    seen: set[str] = set()
    paths: list[str] = []
    for match in REFERENCED_PATH_RE.finditer(text):
        candidate = match.group(1)
        if candidate in seen:
            continue
        seen.add(candidate)
        if Path(candidate).is_file():
            paths.append(candidate)
    for match in REFERENCED_FILENAME_RE.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        resolved = _resolve_bare_filename(name)
        if resolved and resolved not in paths:
            paths.append(resolved)
    return paths


def prefetch_referenced_files(
    text: str, max_files: int = PREFETCH_MAX_FILES
) -> tuple[str, set[str]]:
    """
    Reads the first `max_files` real files referenced by backtick-quoted
    paths in `text` and renders them as a prompt block, so a model
    reviewing/planning against the ticket starts with the files it's
    overwhelmingly likely to ask for already in context - saving the
    read_file turns (and the variance in whether/when a weaker model
    gets around to asking) the same way build_plan_prompt's ticket/
    repo-context embedding does.

    Returns (block, paths) - paths is meant for make_executor's
    preloaded_paths, so a model that still calls read_file on one of
    these gets the short dedup note instead of paying for the content
    twice. block is "" (and paths empty) if nothing was found, so
    callers can unconditionally interpolate it without an empty-block
    check at each call site.
    """
    paths = extract_referenced_paths(text)[:max_files]
    if not paths:
        return "", set()

    sections = []
    for path in paths:
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(content) > PREFETCH_MAX_CHARS_PER_FILE:
            content = (
                content[:PREFETCH_MAX_CHARS_PER_FILE]
                + "\n... (truncated - read_file for the rest)"
            )
        sections.append(f"### {path}\n```\n{content}\n```")

    if not sections:
        return "", set()

    block = (
        "Here are files the ticket references by path, prefetched so you "
        "don't need to read_file them again unless you need a part that "
        "was truncated below:\n\n" + "\n\n".join(sections)
    )
    return block, set(paths)


# ---------------------------------------------------------------------------
# Plan step
# ---------------------------------------------------------------------------


def build_plan_prompt(ticket_content: str) -> str:
    """
    Embeds the ticket content and a repo-context block directly, rather
    than making the model spend tool-call turns fetching things we
    already know with certainty it's going to want - the planner always
    needs the ticket, and orientation (toolchain, layout, module
    boundaries - see repo_context.py) is cheap to give upfront. Content
    embedded in the prompt is processed identically to content returned
    from a tool call (it's all just tokens in context), so this loses
    nothing - it just removes the variance of whether/when the model
    gets around to asking for it.
    """
    instructions = load_prompt_body(PLAN_PROMPT_FILE)
    repo_context_block = repo_context.render_repo_context_block(
        repo_context.gather_repo_context()
    )
    return (
        f"{instructions}\n\n---\n\n"
        f"{AUTO_PREAMBLE}"
        f"Here is the ticket ({TICKET_FILE}) - already complete and "
        f"current, no need to read_file it again:\n\n{ticket_content}\n\n"
        f"Here is repo orientation context - already current, no need to "
        f"list_dir('.') again for the top-level layout:\n{repo_context_block}\n\n"
        f"This is a clean run: {PLAN_FILE} and {UPDATED_PLAN_FILE} do not "
        f"exist yet - there is no prior plan or interrogation output to "
        f"check for, so don't spend a tool call confirming that.\n\n"
        f"Use read_file/list_dir/search_files for any other files you need "
        f"to inspect before planning - but only files you have a concrete "
        f"reason to need, not speculative browsing; every tool call you "
        f"make gets resent in full on every subsequent turn, so prefer one "
        f"targeted search_files call over open-ended directory browsing "
        f"when you're looking for something specific. Produce a TDD plan "
        f"in the exact format from Step 4 above. Your final response (no "
        f"further tool calls) must "
        f"be exactly that plan text - the caller writes it to {PLAN_FILE} "
        f"itself, so do not call write_file and do not add any chat "
        f"header or commentary around the plan."
    )


def run_plan_step(ticket_content: str, model: str, ticket_id: str | None = None) -> str:
    """
    Runs the plan step end to end: prompt, run_with_tools, validity
    check, write to disk, render. Returns plan_text for the caller to
    thread into whichever step comes next. ticket_id is passed straight
    through to run_ai_step_with_retry purely for its log_event calls -
    it's not needed for the prompt or the step itself, only so
    .pipeline-log.jsonl entries for this block can be attributed to the
    right ticket.
    """
    try:
        result = run_ai_step_with_retry(
            lambda: run_with_tools(
                build_plan_prompt(ticket_content),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(
                    allow_write=False, preloaded_paths={str(TICKET_FILE)}
                ),
                "plan",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "plan",
            ticket=ticket_id,
        )
    except (AIError, tools.PipelineAbort) as e:
        die(str(e))
    if "## Acceptance Criteria" not in result.text:
        render_step_output(result.text)
        die("Planner did not produce a valid plan (see output above).")
    log.info("-- Plan generated, writing to disk ...")
    plan_content = tools.write_file_block(str(PLAN_FILE))(result.text)
    render_step_output(plan_content)
    return plan_content


# ---------------------------------------------------------------------------
# Validate step (coverage of the plan against the current codebase)
# ---------------------------------------------------------------------------

LIST_MARKER_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+")
BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")


def extract_plan_files(plan_content: str) -> list[str]:
    """
    Pull file paths out of the plan's '## Implementation Plan' section.
    The plan prompt's template uses '- `path`: change' bullets, but
    model output is not deterministic about list style (numbered lists,
    em-dash separators instead of colons, etc.) - so this tolerates any
    -/*/numbered list marker and pulls the path from the first
    backtick-quoted token on the line rather than assuming a fixed
    separator after it.
    """
    match = re.search(
        r"^## Implementation Plan\s*\n(.*?)(?:\n## |\Z)",
        plan_content,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        return []
    paths = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not LIST_MARKER_RE.match(line):
            continue
        line = LIST_MARKER_RE.sub("", line, count=1)
        backtick_match = BACKTICK_TOKEN_RE.search(line)
        if backtick_match:
            path = backtick_match.group(1).strip()
        else:
            path = line.split(":", 1)[0].split(" - ", 1)[0].strip()
        if path:
            paths.append(path)
    return paths


def gather_plan_file_context(plan_content: str) -> tuple[str, set[str]]:
    """
    Plan and validate run as separate sessions with clean context
    windows - the validator has no memory of which files the planner
    looked at. But the plan's own '## Implementation Plan' section is a
    curated prediction of what the validator will need too, so read
    those files here (host-side, not a model tool call) and hand them
    to the validator already in its initial prompt, instead of making it
    rediscover the same files from scratch.

    This is deliberately narrower than "every file the planner read" -
    planning involves speculative exploration (checking an existing
    similar module, etc.) that doesn't belong in the validator's
    context. The Implementation Plan list is the planner's stated
    conclusion, not its scratch work.

    Falls back to listing a missing path's parent directory -
    implementations sometimes land under a different name than planned.
    Returns (formatted text block, set of paths actually read - safe to
    mark as preloaded so a redundant read_file call is deduped).
    """
    paths = extract_plan_files(plan_content)
    if not paths:
        return "(plan's Implementation Plan section listed no file paths)", set()

    blocks = []
    read_paths: set[str] = set()
    for path_str in paths:
        file_path = Path(path_str)
        if file_path.is_file():
            content = file_path.read_text(encoding="utf-8", errors="replace")
            blocks.append(f"### {path_str}\n```\n{content}\n```")
            read_paths.add(path_str)
            continue

        parent = file_path.parent
        if parent.is_dir():
            entries = sorted(p.name for p in parent.iterdir() if p.is_file())
            listing = "\n".join(f"- {e}" for e in entries) or "(empty)"
            blocks.append(
                f"### {path_str} - not found at this exact path\n"
                f"Actual contents of `{parent}/`:\n{listing}"
            )
        else:
            blocks.append(
                f"### {path_str} - not found, and parent directory `{parent}/` "
                f"does not exist either"
            )

    return "\n\n".join(blocks), read_paths


# ---------------------------------------------------------------------------
# Narrow step - same evidence-gathering a coverage validator would need,
# but the output is plan-shaped (like the plan step's own output)
# instead of a prose verdict. push_ticket.py counts the remaining
# '## Acceptance Criteria' bullets to build the initial stack;
# next_step.py's TICKET_VALIDATE phase re-runs this fresh as a safety
# net after a ticket's frames are exhausted. Always writes to
# GAP_PLAN_FILE - transient scratch, not cross-invocation state (see the
# module-level comment above GAP_PLAN_FILE's definition).
# ---------------------------------------------------------------------------


def build_narrow_prompt(
    ticket_content: str, plan_content: str, plan_file_context: str
) -> str:
    instructions = load_prompt_body(NARROW_PROMPT_FILE)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the original ticket ({TICKET_FILE}) - already complete "
        f"and current, no need to read_file it again:\n\n{ticket_content}\n\n"
        f"Here is the TDD plan to narrow ({PLAN_FILE}) - already "
        f"complete and current, no need to read_file it again:\n\n"
        f"{plan_content}\n\n"
        f"Here is the current content of the files the plan's "
        f"Implementation Plan section names - already provided, no need "
        f"to read_file these again unless you need a file the plan didn't "
        f"name:\n\n{plan_file_context}\n\n"
        f"Use read_file/list_dir/search_files for anything else you need - "
        f"when hunting for evidence of a criterion across the codebase, "
        f"prefer one targeted search_files call over list_dir-then-read_file "
        f"fishing; every tool call gets resent in full on every subsequent "
        f"turn, so fewer, more targeted calls keep this cheaper without "
        f"costing you any evidence. Narrow the plan to just the criteria "
        f"not yet satisfied, per the steps and rules in your instructions."
    )


def run_narrow_step(
    ticket_content: str, plan_content: str, model: str, ticket_id: str | None = None
) -> str:
    plan_file_context, plan_file_paths = gather_plan_file_context(plan_content)
    preloaded = {str(TICKET_FILE), str(PLAN_FILE)} | plan_file_paths
    try:
        result = run_ai_step_with_retry(
            lambda: run_with_tools(
                build_narrow_prompt(ticket_content, plan_content, plan_file_context),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(allow_write=False, preloaded_paths=preloaded),
                "narrow",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "narrow",
            ticket=ticket_id,
        )
    except (AIError, tools.PipelineAbort) as e:
        die_with_log("narrow", str(e), ticket=ticket_id)
    if "## Acceptance Criteria" not in result.text:
        render_step_output(result.text)
        die_with_log(
            "narrow",
            "Narrower did not produce a valid gap plan (see output above).",
            ticket=ticket_id,
        )
    log.info("-- Gap plan generated, writing to disk ...")
    gap_plan_content = tools.write_file_block(str(GAP_PLAN_FILE))(result.text)
    render_step_output(gap_plan_content)
    return gap_plan_content


# ---------------------------------------------------------------------------
# Plan-narrow step (merged) - experimental alternative to running plan and
# narrow as two separate sessions. The model extracts acceptance criteria
# and checks each one against the codebase's current state in one pass,
# using live tools throughout instead of the host pre-reading plan-named
# files and handing them back (see gather_plan_file_context) - that
# handoff only existed to bridge two separate sessions with clean context
# windows; one session needs no bridge. Produces a single artifact, the
# gap plan - there's no separate full-plan output, since the only thing
# that consumed the full plan (run_review_gate's "review against full
# ticket scope") doesn't need criteria that are already satisfied and
# untouched.
# ---------------------------------------------------------------------------


def build_plan_narrow_prompt(ticket_content: str) -> str:
    instructions = load_prompt_body(PLAN_NARROW_PROMPT_FILE)
    repo_context_block = repo_context.render_repo_context_block(
        repo_context.gather_repo_context()
    )
    ticket_evidence_seed_block = repo_context.render_ticket_evidence_seed_block(
        repo_context.gather_ticket_evidence_seed(ticket_content)
    )
    return (
        f"{instructions}\n\n---\n\n"
        f"{AUTO_PREAMBLE}"
        f"Here is the ticket ({TICKET_FILE}) - already complete and "
        f"current, no need to read_file it again:\n\n{ticket_content}\n\n"
        f"Here is repo orientation context - already current, no need to "
        f"list_dir('.') again for the top-level layout:\n{repo_context_block}\n\n"
        f"Here is a ticket-aware evidence seed - preliminary host-side "
        f"search results for likely identifiers from the ticket. Use it "
        f"to orient faster, but do not treat it as proof by itself; you "
        f"still need to verify each criterion before marking it PASS:\n"
        f"{ticket_evidence_seed_block}\n\n"
        f"This is a clean run: {GAP_PLAN_FILE} does not exist yet - there "
        f"is no prior gap plan to check for, so don't spend a tool call "
        f"confirming that.\n\n"
        f"Use read_file/list_dir/search_files for anything else you need - "
        f"but only files you have a concrete reason to need, not "
        f"speculative browsing; every tool call you make gets resent in "
        f"full on every subsequent turn, so prefer one targeted "
        f"search_files call over open-ended directory browsing when "
        f"you're looking for something specific. Produce the gap plan in "
        f"the exact format from Step 5 above. Your final response (no "
        f"further tool calls) must be exactly that plan text - the caller "
        f"writes it to {GAP_PLAN_FILE} itself, so do not call write_file "
        f"and do not add any chat header or commentary around the plan."
    )


def run_plan_narrow_step(
    ticket_content: str, model: str, ticket_id: str | None = None
) -> str:
    """
    Merged plan+narrow: one model session, one artifact (the gap plan).
    See module-level comment above for why this doesn't also produce
    PLAN_FILE/.tdd-plan.md the way the two-step path does.
    """
    try:
        result = run_ai_step_with_retry(
            lambda: run_with_tools(
                build_plan_narrow_prompt(ticket_content),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(
                    allow_write=False, preloaded_paths={str(TICKET_FILE)}
                ),
                "plan-narrow",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "plan-narrow",
            ticket=ticket_id,
        )
    except (AIError, tools.PipelineAbort) as e:
        die_with_log("plan-narrow", str(e), ticket=ticket_id)
    if "## Acceptance Criteria" not in result.text:
        render_step_output(result.text)
        die_with_log(
            "plan-narrow",
            "Planner-Narrower did not produce a valid gap plan (see output above).",
            ticket=ticket_id,
        )
    log.info("-- Gap plan generated, writing to disk ...")
    gap_plan_content = tools.write_file_block(str(GAP_PLAN_FILE))(result.text)
    render_step_output(gap_plan_content)
    return gap_plan_content


# ---------------------------------------------------------------------------
# Per-criterion parsing (push_ticket.py) and scoped test execution
# (next_step.py). The old markdown "Test: <file> :: <name>" annotation
# mechanism that used to record a criterion's test pointer directly on
# .gap-plan.md is retired - CriterionFrame.test_files/test_names (see
# the stack section below) is the same information, stored as real
# structured state on the stack instead of a text annotation on a
# scratch file.
# ---------------------------------------------------------------------------


def extract_acceptance_criteria(plan_content: str) -> list[str]:
    """
    Pull each '- [ ] ...' line directly under '## Acceptance Criteria' -
    same list-tolerant parsing style as extract_plan_files, applied to
    the criteria section instead of the implementation section. Returns
    the exact bullet line text (including any trailing HTML comment) -
    push_ticket.py uses this verbatim text as a new CriterionFrame's
    `criterion` field.
    """
    match = re.search(
        r"^## Acceptance Criteria\s*\n(.*?)(?:\n## |\Z)",
        plan_content,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        return []
    return [
        line.strip()
        for line in match.group(1).splitlines()
        if LIST_MARKER_RE.match(line.strip())
    ]


VERIFICATION_TAG_RE = re.compile(
    r"verify:\s*(test(?:-refactor)?|refactor|manual)\b", re.IGNORECASE
)
STRATEGY_TAG_RE = re.compile(r"strategy:\s*(\w+)\b", re.IGNORECASE)
KNOWN_STRATEGIES = frozenset({"tdd", "direct", "manual", "refactor"})


def extract_verification_mode(criterion: str) -> str:
    """
    Parses a "verify: test|test-refactor|refactor|manual" tag out of a
    criterion's trailing HTML comment (narrow-plan.prompt.md's Final
    answer format embeds this alongside the existing "why" reason, per
    its Step 3 - Narrower tags each retained criterion this way at the
    source, rather than a separate classification pass).

    Defaults to "test" - the universal behavior before this
    classification existed, and the safe default for anything this can't
    parse a tag from: a review/validate-missed finding (extracted from
    reviewer prose, never carries this tag at all), a hand-written or
    foreign criterion, or a criterion from a .gap-plan.md that predates
    this tag.
    """
    match = VERIFICATION_TAG_RE.search(criterion)
    if match:
        mode = match.group(1).lower()
        if mode in ("manual", "test-refactor", "refactor"):
            return mode
    return "test"


EXISTING_TEST_TAG_RE = re.compile(r"existing_test:\s*(\S+)")


def extract_strategy(criterion: str) -> str:
    """
    Parses a "strategy: <name>" tag from a criterion's trailing HTML
    comment. Defaults from the verification mode when no explicit tag is
    present.
    """
    match = STRATEGY_TAG_RE.search(criterion)
    if match:
        name = match.group(1).lower()
        if name in KNOWN_STRATEGIES:
            return name
    verification = extract_verification_mode(criterion)
    if verification == "manual":
        return "manual"
    if verification == "refactor":
        return "refactor"
    return "tdd"


def extract_existing_test_refs(criterion: str) -> list[str]:
    """
    Parses every "existing_test: <file>::<test_name>" tag out of a
    criterion's trailing HTML comment, alongside the "why"/"verify" tags
    extract_verification_mode reads (narrow-plan.prompt.md's Step 2
    surfaces one of these when a criterion is FAIL specifically because
    an existing test asserts the *old* behavior, rather than "no
    coverage found at all" - Narrower already located that test to reach
    its verdict; this just keeps the pointer instead of discarding it).
    The tag is repeatable - a criterion whose old behavior is asserted
    by more than one pre-existing test carries one `existing_test:`
    clause per test, all in the same trailing comment.

    An empty list means "write a new test (or tests)" - the universal
    behavior before this tag existed, and the correct default for
    anything that doesn't carry it: a review/validate-missed finding, a
    hand-written criterion, or a criterion this specific FAIL reason
    didn't apply to. Deliberately a list, not `list[str] | None` -
    callers only ever need to loop 0..N times over it, never branch on
    None specifically.
    """
    refs = []
    for match in EXISTING_TEST_TAG_RE.finditer(criterion):
        ref = match.group(1)
        if ref.endswith("-->"):
            ref = ref[:-3]
        ref = ref.rstrip(";").strip()
        if ref:
            refs.append(ref)
    return refs


def resolve_feedback_target(frame: "CriterionFrame", requested: str | None) -> str:
    """
    Resolve a user-requested feedback target for the top frame. `requested`
    may be None/"auto" to select the natural retry target for the frame's
    verification mode and current phase.
    """
    requested = (requested or "auto").strip().lower()
    if requested != "auto" and requested not in FEEDBACK_TARGETS:
        raise ValueError(
            f"unknown feedback target {requested!r}; choose one of: "
            "auto, tester, implementor, human"
        )

    strategy = frame.strategy
    if strategy == "tdd":
        if frame.verification == "test-refactor":
            default = FEEDBACK_TARGET_TESTER
            allowed = {FEEDBACK_TARGET_TESTER, FEEDBACK_TARGET_HUMAN}
        else:
            default = (
                FEEDBACK_TARGET_IMPLEMENTOR
                if frame.status == "test-written"
                else FEEDBACK_TARGET_TESTER
            )
            allowed = {
                FEEDBACK_TARGET_TESTER,
                FEEDBACK_TARGET_IMPLEMENTOR,
                FEEDBACK_TARGET_HUMAN,
            }
    elif strategy in ("direct", "manual", "refactor"):
        default = FEEDBACK_TARGET_IMPLEMENTOR
        allowed = {FEEDBACK_TARGET_IMPLEMENTOR, FEEDBACK_TARGET_HUMAN}
    else:
        raise ValueError(f"unknown strategy {strategy!r}")

    target = default if requested == "auto" else requested
    if target not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(
            f"feedback target {target!r} is not valid for verification="
            f"{frame.verification!r} and strategy={strategy!r}; allowed targets: {allowed_text}"
        )
    return target


# ---------------------------------------------------------------------------
# Criteria stack - .criteria-stack.json is the pipeline's canonical
# work-queue and the only file either push_ticket.py or next_step.py
# trusts across invocations. See the module docstring for how this
# replaces the old per-script state files.
# ---------------------------------------------------------------------------


@dataclass
class StepContext:
    """Shared context passed to strategy handlers."""

    model: str
    step_models: dict[str, str]
    commands: dict
    config_path: Path
    continuous: bool
    max_attempts: int
    retry_policy: RetryPolicy | None = None
    accept_green: bool = False
    accept_manual: bool = False
    accept_no_test: bool = False
    skip_implementation: bool = False
    git_cfg: "GitConfig | None" = None


@dataclass
class CriterionFrame:
    ticket: str  # e.g. "SA-42"
    criterion: str  # verbatim bullet from the gap plan, e.g. "- [ ] ..."
    plan_context: str  # Implementation Plan lines relevant to this
    # criterion, extracted at push time (see
    # extract_plan_context_for_criterion)
    test_files: list[str] | None  # set once the test-writer runs - None
    # until then, otherwise parallel to
    # test_names (same length, same order).
    # Almost always length 1; more than one only
    # when this criterion's behavior genuinely
    # spans call paths/subjects that can't share
    # a single test function (see
    # test-criterion.prompt.md's Step 3).
    test_names: list[str] | None  # fully-qualified, parallel to
    # test_files, for run_scoped_test/
    # run_scoped_tests.
    status: str  # "pending" | "test-written" | "done" |
    # "baseline-confirmed" (refactor mode:
    # safety-net tests confirmed GREEN at
    # baseline, awaiting the structural
    # refactor before recheck).
    origin: str  # "ticket" | "review" | "validate-missed" -
    # recorded but not yet acted on differently;
    # all origins go through the identical
    # test-write -> implement -> gate cycle.
    verification: str = "test"  # "test" | "test-refactor" | "refactor"
    # | "manual" - set from the gap plan's own
    strategy: str = "tdd"  # "tdd" | "direct" | "manual" | "refactor"
    # - records how the criterion should be implemented, independent of
    # the verification gate. The default stays "tdd" for backwards
    # compatibility with old stacks and plans that do not carry an
    # explicit strategy tag.
    # "verify:" tag (see
    # extract_verification_mode). "test" is the
    # default for behavior changes a red/green
    # test proves (write a failing test ->
    # implement). "manual" is for criteria with
    # no meaningful red/green (documentation,
    # config, CI). "test-refactor" is for
    # criteria that restructure existing test
    # code (imports/helpers/utilities) without
    # changing assertions - the test-writer
    # rewrites the test, expected GREEN after.
    # "refactor" is for criteria that
    # restructure production code without
    # changing behavior - existing tests are the
    # safety net (kept GREEN), WRITE_TEST is
    # skipped.
    # Defaults to "test" - the universal behavior
    # before this field existed, and the safe
    # default for anything the tag-parsing can't
    # find one on (review/validate-missed
    # findings, which come from prose rather
    # than a plan step, and older stack files
    # from before this field existed - see
    # load_stack's **entry unpacking).
    existing_test_refs: list[str] = field(default_factory=list)  # each a
    # "file::test_name" reference, from the gap
    # plan's repeatable "existing_test:" tag (see
    # extract_existing_test_refs) when this
    # criterion is about changing behavior one or
    # more existing tests already cover, rather
    # than adding new coverage. Empty (the
    # default) means WRITE_TEST writes brand-new
    # test(s), same as always; each entry present
    # names one existing test to modify instead
    # of duplicating with a new one.
    unconfirmed_tests: list[str] = field(default_factory=list)  # subset
    # of test_names currently believed green
    # without any implementation having happened
    # (origin != "ticket", observed green the
    # very first time WRITE_TEST looked at it -
    # see next_step.py's do_write_test/
    # recheck_test_frame). Only ever shrinks
    # after that first observation (a name is
    # dropped once observed red - it's no longer
    # a "suspiciously easy green" at that point,
    # it's just a normal red test needing real
    # implementation) - never re-added later.
    # Gates the final "pop as done" decision
    # once every test is green: non-empty means
    # --accept-green is required, same escape
    # hatch GREEN_UNCONFIRMED_STATUS has always
    # used, generalized from "the one test" to
    # "whichever of the N were never confirmed."
    base_commit: str | None = None  # git-workflow only (git_workflow =
    # true in .dev-pipeline.toml). The HEAD SHA
    # recorded when this frame first entered
    # WRITE_TEST, so reset-criterion can `git
    # reset --hard` back to exactly the pre-test
    # state. None when git_workflow is off, or
    # before WRITE_TEST has run for this frame.
    commit_sha: str | None = None  # git-workflow only. The SHA of the
    # commit next_step created on POP (see
    # do_pop's commit-on-pop). None until the
    # criterion is popped and committed; stays
    # None for criteria that popped with an
    # empty diff (nothing to stage).
    feedback: str | None = None
    feedback_target: str | None = None
    feedback_attempts: int = 0
    ticket_snapshot: str | None = None  # rendered ticket markdown captured
    # at push_ticket seed time (or at --validate-only push time, if the
    # caller provides it). Re-used by TICKET_VALIDATE's re-narrow so the
    # same ticket text that was planned against is also what validation
    # checks, without an unconditional Linear round-trip. None means no
    # snapshot was captured (e.g. --from-gap-plan, --validate-only with
    # no fetch, or a stack file written before this field existed) -
    # TICKET_VALIDATE falls back to a live fetch in that case.


def load_stack() -> list[CriterionFrame]:
    """
    Read .criteria-stack.json. Returns [] if the file does not exist or
    is empty. A corrupt or schema-mismatched file is a hard stop
    (die_with_log), not something to silently reset - the stack is the
    pipeline's only cross-invocation state, so guessing at a recovery
    would risk silently discarding in-progress work.

    Entries written by a pre-multi-test schema (singular "test_file"/
    "test_name" string keys, and/or a singular "existing_test_ref"
    string-or-null key) are upgraded in place before construction: the
    old keys are popped and rewrapped into the new list-shaped ones
    (None stays None; a non-None existing_test_ref becomes a one-element
    list; absent entirely becomes []). Without this, a stack file
    written before this migration would raise TypeError here - the only
    schema-compatibility concern this rename introduces, since every
    other field on this dataclass was additive (a new field with a
    default), not a rename.
    """
    if not CRITERIA_STACK_FILE.is_file():
        return []
    text = CRITERIA_STACK_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        die_with_log("stack", f"{CRITERIA_STACK_FILE} is not valid JSON: {e}")
    for entry in raw:
        if "test_file" in entry or "test_name" in entry:
            old_file = entry.pop("test_file", None)
            old_name = entry.pop("test_name", None)
            entry.setdefault("test_files", [old_file] if old_file is not None else None)
            entry.setdefault("test_names", [old_name] if old_name is not None else None)
        if "existing_test_ref" in entry:
            old_ref = entry.pop("existing_test_ref")
            entry.setdefault(
                "existing_test_refs", [old_ref] if old_ref is not None else []
            )
    try:
        return [CriterionFrame(**entry) for entry in raw]
    except TypeError as e:
        die_with_log(
            "stack",
            f"{CRITERIA_STACK_FILE} does not match the expected frame schema: {e}",
        )


def save_stack(frames: list[CriterionFrame]) -> None:
    """
    Serialise and write atomically: write to a temp file, then
    os.replace. Prevents a partial write from corrupting the stack if
    the process is interrupted mid-write.
    """
    SCAFFOLD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = SCAFFOLD_TEMP_DIR / (CRITERIA_STACK_FILE.name + ".tmp")
    tmp_path.write_text(
        json.dumps([asdict(frame) for frame in frames], indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, CRITERIA_STACK_FILE)


def push_frames(frames: list[CriterionFrame]) -> None:
    """
    Prepend frames to the front of the existing stack and save. Used
    only by next_step.py, for review findings and validate-missed
    criteria - both prepend onto a stack that may already have content
    (the ticket's remaining frames, or none). push_ticket.py does NOT
    use this - it owns full-stack replacement directly via save_stack,
    since it only ever runs once its own guard confirms there's nothing
    worth preserving (see push_ticket.py's module docstring).
    """
    save_stack(frames + load_stack())


def pop_frame() -> CriterionFrame | None:
    """Remove and return frame 0, saving the updated stack to disk
    first. Returns None if the stack is empty."""
    stack = load_stack()
    if not stack:
        return None
    frame = stack.pop(0)
    save_stack(stack)
    return frame


def peek_frame() -> CriterionFrame | None:
    """Return frame 0 without modifying the stack. None if empty."""
    stack = load_stack()
    return stack[0] if stack else None


# A verification="refactor" frame (see extract_verification_mode) whose
# baseline check has passed: the safety-net tests named in
# existing_test_refs were all GREEN at baseline, so the refactor is
# cleared to proceed. The frame sits in this status while the human
# (or lib/implement.py) makes the structural changes to production code;
# the next 'next_step' call re-runs the safety-net tests and pops only if
# they're still GREEN *and* a production file actually changed. Shared
# here (not private to next_step.py) because lib/implement.py needs to
# recognize it as the "refactor awaiting implementation" status too.
BASELINE_CONFIRMED_STATUS = "baseline-confirmed"

# A ticket's per-criterion frames are all real acceptance criteria with
# a test to write; this sentinel is not one - it's a durable stand-in
# for "TICKET_VALIDATE still needs to run/re-run for this ticket",
# recognized specially by next_step.py's phase detection rather than
# flowing through WRITE_TEST/AWAIT_IMPL/POP. Shared here (not private to
# next_step.py) because push_ticket.py's --validate-only pushes the same
# sentinel directly, without a pop ever having happened first.
VALIDATING_STATUS = "validating"
VALIDATING_ORIGIN = "ticket-validate"
VALIDATING_CRITERION_TEXT = "(ticket validation pending)"


def ensure_validating_sentinel(
    ticket_id: str, ticket_snapshot: str | None = None
) -> None:
    """
    Makes "this ticket still needs TICKET_VALIDATE" a durable stack
    frame instead of a fact that only exists for the duration of one
    call. Two callers:
      - next_step.py's do_ticket_validate, as the first thing it does,
        before any fallible step (fetch, plan, narrow, lint, test suite,
        smoke, review) - so if any of those dies, this sentinel is
        already safely on disk, and the next `next_step` invocation's
        phase detection finds it and retries validation from scratch
        instead of reporting "no work remaining" or moving on to a
        different ticket with no way back to this one.
      - push_ticket.py's --validate-only, to trigger a validation pass
        on demand for a ticket with no criteria currently on the stack
        (e.g. you believe it's already fully implemented and just want
        lint/test-suite/smoke/review to run) - same sentinel, same
        resume mechanism, just pushed directly instead of arrived at
        via a pop.

    ticket_snapshot: if provided, the rendered ticket markdown captured
    at push_ticket seed time. Stored in the sentinel so that a resumed
    retry (next `next_step` after a failed validation) can use the same
    ticket text without re-fetching from Linear.

    Idempotent: does nothing if this exact sentinel is already the top
    frame - the case where this is a resumed retry after a prior
    validation failure, not a fresh one.
    """
    top = peek_frame()
    if top is not None and top.ticket == ticket_id and top.status == VALIDATING_STATUS:
        return
    push_frames(
        [
            CriterionFrame(
                ticket=ticket_id,
                criterion=VALIDATING_CRITERION_TEXT,
                plan_context="",
                test_files=None,
                test_names=None,
                status=VALIDATING_STATUS,
                origin=VALIDATING_ORIGIN,
                ticket_snapshot=ticket_snapshot,
            )
        ]
    )


# ---------------------------------------------------------------------------
# Declined-criteria ledger - .declined-criteria.json records every
# criterion a mechanical grounding check has rejected (see
# verify_criterion_grounding/filter_grounded_frames below), so a
# criterion Narrower or a reviewer regenerates identically on a later run
# doesn't need a human to re-notice and re-diagnose the same already-
# rejected claim. Append-only and never read back into the stack itself -
# resolving a false positive is manual (edit or delete the offending
# entry), the same recovery-by-editing-the-state-file precedent as
# deleting .criteria-stack.json directly to abandon an in-progress
# ticket.
# ---------------------------------------------------------------------------


@dataclass
class DeclinedCriterion:
    ticket: str
    criterion: str  # verbatim, same text a CriterionFrame would carry
    origin: str  # "ticket" | "validate-missed" | "review"
    reasons: list[str]  # from verify_criterion_grounding
    ts: str  # ISO timestamp, for a human skimming the file


def load_declined() -> list[DeclinedCriterion]:
    """
    Read .declined-criteria.json. Returns [] if the file does not exist
    or is empty. Same hard-stop-on-corruption stance as load_stack - a
    malformed ledger is worth a human's attention, not a silent reset.
    """
    if not DECLINED_CRITERIA_FILE.is_file():
        return []
    text = DECLINED_CRITERIA_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        die_with_log("grounding", f"{DECLINED_CRITERIA_FILE} is not valid JSON: {e}")
    try:
        return [DeclinedCriterion(**entry) for entry in raw]
    except TypeError as e:
        die_with_log(
            "grounding",
            f"{DECLINED_CRITERIA_FILE} does not match the expected schema: {e}",
        )


def is_declined(ticket: str, criterion: str) -> bool:
    """
    Exact string match on (ticket, criterion) - deliberately not fuzzy. A
    criterion Narrower rewords slightly between runs won't match a prior
    decline and will be re-checked from scratch by
    verify_criterion_grounding; fuzzy matching would risk conflating two
    genuinely different criteria that happen to share wording, which is
    worse than occasionally re-flagging the same underlying fact twice.
    """
    return any(d.ticket == ticket and d.criterion == criterion for d in load_declined())


def record_declined(
    ticket: str, criterion: str, origin: str, reasons: list[str]
) -> None:
    """
    Appends one entry and saves. Never overwrites or dedupes existing
    entries - the ledger is a record of what's been flagged and when, not
    live state like the stack, so there's no "current" entry to replace.
    """
    entries = load_declined()
    entries.append(
        DeclinedCriterion(
            ticket=ticket,
            criterion=criterion,
            origin=origin,
            reasons=reasons,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    )
    DECLINED_CRITERIA_FILE.write_text(
        json.dumps([asdict(e) for e in entries], indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Mechanical criterion grounding - a non-AI, non-interactive check that a
# freshly-generated criterion's own factual claims (a referenced existing
# test, a bare symbol-shaped term) actually hold against the codebase,
# before that criterion becomes a stack frame and starts costing further
# AI cycles. See criterion-grounding-plan.md for the full design and the
# SA-454 case this was built to catch: a criterion claiming QuickBooks
# invoices should map to an `Outstanding` status that was never a real
# InvoiceStatus variant - traced back to the raw ticket text itself, not
# something Narrower invented independently, and missed by review-ticket
# too. Applied to every real criterion regardless of origin (see
# GROUNDING_CHECKED_ORIGINS/filter_grounded_frames) - originally scoped
# to validate-missed/review only, widened after that finding showed the
# same hallucination can arrive via the raw ticket text and flow straight
# into an origin="ticket" frame with nobody having actually grounded it
# against the codebase first.
# ---------------------------------------------------------------------------

GROUNDING_CANDIDATE_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]{2,}\b")

# Deliberately near-empty - grown from real false positives observed in
# practice, not populated speculatively (see criterion-grounding-plan.md's
# Constraints). Case-sensitive, matched as whole words.
GROUNDING_STOPLIST: frozenset[str] = frozenset()


def extract_grounding_candidates(criterion: str) -> list[str]:
    """
    Pulls "claim tokens" - bare capitalized words that read as symbol
    references (a status/variant/type name) rather than prose - out of a
    criterion's visible text only (before its trailing HTML comment; the
    comment is Narrower's own reasoning about the gap, not itself a claim
    to verify).

    Deliberately not backtick-aware: this codebase's own gap-plan
    convention nests backticks without escaping (the whole criterion is
    backtick-wrapped, and individual terms inside it are too), so a naive
    `` `([^`]+)` `` regex pairs the wrong spans - verified directly
    against the SA-454 criterion, where it captures prose fragments ("to",
    ", and") instead of the terms ("Outstanding", "Paid"). Scanning the
    raw text for bare capitalized words, treating backticks as ordinary
    characters, sidesteps the nesting problem entirely.

    The first token is always dropped (sentence-initial capitalization,
    not a symbol claim), duplicates are deduped preserving order, and
    anything in GROUNDING_STOPLIST is dropped.
    """
    visible = criterion.split(" <!--", 1)[0]
    matches = GROUNDING_CANDIDATE_RE.findall(visible)
    if not matches:
        return []
    seen: set[str] = {matches[0]}
    candidates = []
    for token in matches[1:]:
        if token in seen or token in GROUNDING_STOPLIST:
            continue
        seen.add(token)
        candidates.append(token)
    return candidates


def check_symbol_grounding(candidates: list[str]) -> list[str]:
    """
    For each candidate token, runs `git grep -q -F -w -- <token>` (fixed
    string, whole word, tracked files only - the default git grep scope,
    no --untracked) from the repo root. Returns the subset with zero
    matches anywhere in tracked source.

    Case-sensitive and tracked-only are both deliberate, not incidental:
    a real symbol would need to appear capitalized as such somewhere - a
    lenient, case-insensitive search would also match the common-English
    reading of the same word in prose/comments and defeat the point.
    Tracked-only also means this can never be fooled by the very
    criterion text it's checking: this pipeline's own scratch files
    (.gap-plan.md, .criteria-stack.json, etc.) are untracked (see
    _SCAFFOLDING_PATHS), so git grep never finds a candidate there.

    A `git grep` exit code other than 0 (found) or 1 (not found) - e.g.
    128 outside a git working tree - is treated as inconclusive, not a
    hallucination signal, and is never flagged: this check exists to
    catch a fabricated claim, not to misreport an environment problem as
    one.
    """
    ungrounded = []
    for token in candidates:
        result = subprocess.run(
            ["git", "grep", "-q", "-F", "-w", "--", token],
            capture_output=True,
            check=False,
        )
        if result.returncode == 1:
            ungrounded.append(token)
        elif result.returncode not in (0, 1):
            log.debug(
                "-- git grep errored checking grounding candidate %r (exit %d) - not flagging it.",
                token,
                result.returncode,
            )
    return ungrounded


def verify_existing_test_refs_resolve(existing_test_refs: list[str]) -> list[str]:
    """
    For each "file::name" ref, confirms the file exists and that the
    function name (the last "::"-separated segment of the qualified test
    name) appears somewhere in it as a whole word - a plain text search,
    not full AST parsing, language-agnostic on purpose (unlike
    lib/implement.py's brace-counting _extract_function_block, this only
    needs to know the name exists *somewhere* plausible, not extract its
    full body). Returns one human-readable reason per ref that doesn't
    resolve.

    The ref format is "file::qualified_test_name" where the test name is
    "in whatever form your test runner's filter syntax expects" (see
    test-criterion.prompt.md). For Rust that's "mod::test_name" (e.g.
    "tests::quickbooks_oauth_token_url_uses_production_endpoint"), which
    contains its own "::" separator. Splitting on the FIRST "::"
    (partition, not rpartition) keeps the file path intact while leaving
    the full qualified name as the test part; the function name (last
    "::"-separated segment of the qualified name) is what's searched for
    in the file.
    """
    unresolved = []
    for ref in existing_test_refs:
        if "::" not in ref:
            unresolved.append(f"existing_test ref '{ref}' is not in 'file::name' shape")
            continue
        file_part, _, name = ref.partition("::")
        path = Path(file_part)
        if not path.is_file():
            unresolved.append(f"existing_test ref '{ref}': file does not exist")
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            unresolved.append(f"existing_test ref '{ref}': could not read file ({e})")
            continue
        func_name = name.rpartition("::")[2] if "::" in name else name
        if not re.search(r"\b" + re.escape(func_name) + r"\b", content):
            unresolved.append(
                f"existing_test ref '{ref}': no symbol named '{func_name}' found in {file_part}"
            )
    return unresolved


def verify_criterion_grounding(
    criterion: str, existing_test_refs: list[str]
) -> list[str]:
    """
    Combines both mechanical grounding checks (see module comment above)
    into the reasons a criterion fails grounding. Empty list means
    grounded - safe to build a real stack frame from.
    """
    reasons = list(verify_existing_test_refs_resolve(existing_test_refs))
    candidates = extract_grounding_candidates(criterion)
    ungrounded = check_symbol_grounding(candidates)
    reasons += [
        f"claims `{token}` but no tracked file contains that token"
        for token in ungrounded
    ]
    return reasons


# Frame origins that are real acceptance criteria, checked by
# filter_grounded_frames below. VALIDATING_ORIGIN ("ticket-validate") is
# deliberately excluded - it's a control-flow sentinel, not a claim about
# the codebase, and its criterion text ("(ticket validation pending)")
# has no symbol-shaped tokens to check anyway.
GROUNDING_CHECKED_ORIGINS = frozenset({"ticket", "validate-missed", "review"})


def filter_grounded_frames(
    frames: list[CriterionFrame],
) -> tuple[list[CriterionFrame], list[tuple[CriterionFrame, list[str]]], int]:
    """
    The shared gate every frame-construction call site (push_ticket's two
    paths, next_step's validate-missed and review-findings paths) runs
    candidate frames through before any of them becomes real, AI-costing
    work on the stack. Never blocks and never makes an AI call - see the
    module comment above.

    Returns (to_push, newly_declined, skipped_count):
      - to_push: frames that passed grounding, or whose origin isn't
        checked at all (VALIDATING_ORIGIN), in input order.
      - newly_declined: (frame, reasons) pairs that failed grounding on
        this call - already recorded to DECLINED_CRITERIA_FILE as a side
        effect; the caller is responsible only for printing them.
      - skipped_count: how many input frames were already in the ledger
        from a prior call (is_declined) and were skipped without
        re-running grounding at all.
    """
    to_push: list[CriterionFrame] = []
    newly_declined: list[tuple[CriterionFrame, list[str]]] = []
    skipped_count = 0
    for frame in frames:
        if frame.origin not in GROUNDING_CHECKED_ORIGINS:
            to_push.append(frame)
            continue
        if is_declined(frame.ticket, frame.criterion):
            skipped_count += 1
            continue
        reasons = verify_criterion_grounding(frame.criterion, frame.existing_test_refs)
        if reasons:
            record_declined(frame.ticket, frame.criterion, frame.origin, reasons)
            newly_declined.append((frame, reasons))
        else:
            to_push.append(frame)
    return to_push, newly_declined, skipped_count


def extract_plan_context_for_criterion(criterion: str, gap_plan_text: str) -> str:
    """
    Extract the Implementation Plan entries relevant to `criterion`, for
    a CriterionFrame's plan_context field. Heuristic: lines from
    '## Implementation Plan' that mention any backtick-quoted token from
    the criterion's own text (file paths, type names, function names -
    the same convention the plan/gap-plan prompts use throughout).
    Falls back to the full Implementation Plan section if the criterion
    has no backtick tokens, or none of them match any line; falls back
    to the entire gap plan text if there's no Implementation Plan
    section at all. Never returns an empty string - the tester needs
    something to work from either way.
    """
    match = re.search(
        r"^## Implementation Plan\s*\n(.*?)(?:\n## |\Z)",
        gap_plan_text,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        return gap_plan_text
    impl_section = match.group(1).strip()
    if not impl_section:
        return gap_plan_text

    nouns = {tok.strip() for tok in BACKTICK_TOKEN_RE.findall(criterion) if tok.strip()}
    if not nouns:
        return impl_section

    matching_lines = [
        line
        for line in impl_section.splitlines()
        if any(noun in line for noun in nouns)
    ]
    return "\n".join(matching_lines) if matching_lines else impl_section


# ---------------------------------------------------------------------------
# Mechanical pre-check for verify="test-refactor" criteria - the same
# "status is a hint, re-detect from real state" principle the state
# machine already applies to verify="test" (red/green) and verify=
# "refactor" (baseline + git-changed-files) criteria. A test-refactor
# criterion is about structural changes to existing test code
# (imports/helpers/utilities, not assertions - see
# narrow-plan.prompt.md's Step 4), expected GREEN throughout, so there
# is no red/green signal to re-detect satisfaction from: the only
# mechanical floor is "do the named file(s) actually contain (or no
# longer contain) what the criterion describes?" This parses the
# criterion's structural claims into positive ("imports X from Z",
# "uses X::method") and negative ("contains no local X struct", "no
# local X() helper") assertions, reads the named file(s), and returns
# True only when every parsed assertion passes. Deliberately
# conservative: a criterion whose wording matches no known pattern, or
# that names a file this can't read, returns False (inconclusive) so
# the caller falls through to the normal WRITE_TEST path, same as
# today - this never pops a frame it can't positively confirm.
# ---------------------------------------------------------------------------

# The leading identifier-path of a backtick-quoted "uses `X::method(...)`"
# claim - everything from the start of the token up to the first
# character that isn't a word char or ":" (so "EnvVarGuard::unset("
# yields "EnvVarGuard::unset"). Used only for the "uses" positive
# assertion, where matching the whole call expression verbatim would
# be too brittle (quote style, spacing in the arguments vary).
_RECHECK_USES_LEADING_RE = re.compile(r"[A-Za-z_][\w:]*")

# "imports `X` and `Y` from `Z`" - the backtick-quoted source module
# after "from", plus every backtick-quoted imported name between
# "imports" and "from" (an import assertion is positive: the file must
# contain the source string AND every imported name).
_RECHECK_IMPORTS_RE = re.compile(r"\bimports?\b(.+?)\bfrom\s*`([^`]+)`", re.IGNORECASE)

# "no local `X` struct" - a negative assertion: the file must NOT
# define `struct X`.
_RECHECK_NO_LOCAL_STRUCT_RE = re.compile(r"no local `([^`]+)` struct", re.IGNORECASE)

# "no local `X()` or `Y()` helper" - the span between "no local" and
# "helper" holds one or more backtick-quoted `name()` tokens; each is a
# negative assertion (the file must NOT define `fn name`).
_RECHECK_NO_LOCAL_HELPER_RE = re.compile(r"no local (.+?) helper", re.IGNORECASE)

# "uses `X::method(...)`" - a positive assertion: the file must contain
# the token's leading identifier-path (see _RECHECK_USES_LEADING_RE).
_RECHECK_USES_RE = re.compile(r"\buses?\s*`([^`]+)`", re.IGNORECASE)


def _parse_test_refactor_assertions(
    criterion_visible: str,
) -> tuple[list[str], list[re.Pattern]]:
    """
    Parse a test-refactor criterion's visible text (before its trailing
    HTML comment) into (positives, negatives):
      - positives: substrings the named file(s) must contain (a plain
        ``in`` check, like verify_existing_test_refs_resolve's name
        search - no regex, so an import path like ``crate::test_support``
        or a call like ``EnvVarGuard::unset`` matches verbatim).
      - negatives: compiled regexes the file(s) must NOT match (each
        a symbol-definition shape - ``struct X`` or ``fn X`` - so a
        bare mention of ``X`` in an unrelated position isn't itself a
        hit; the same deliberate simplicity as
        verify_existing_test_refs_resolve's whole-word search, no AST
        parsing and no comment stripping).

    Returns ([], []) when the wording matches none of the known
    patterns (see check_test_refactor_satisfied) - the caller treats
    that as inconclusive. Never raises; an unparseable clause simply
    contributes nothing rather than failing the whole parse.
    """
    positives: list[str] = []
    negatives: list[re.Pattern] = []

    # "imports `X` and `Y` from `Z`" -> file must contain `Z` and each
    # of the imported names (X, Y, ...). Non-greedy up to the nearest
    # "from `...`", so a criterion with a single import clause parses
    # exactly that clause; multiple clauses (rare) each add their own.
    for m in _RECHECK_IMPORTS_RE.finditer(criterion_visible):
        names_span = m.group(1)
        source = m.group(2).strip()
        positives.append(source)
        for name in BACKTICK_TOKEN_RE.findall(names_span):
            name = name.strip()
            if name:
                positives.append(name)

    # "no local `X` struct" -> file must NOT define `struct X`.
    for m in _RECHECK_NO_LOCAL_STRUCT_RE.finditer(criterion_visible):
        name = m.group(1).strip()
        negatives.append(re.compile(r"\bstruct\s+" + re.escape(name) + r"\b"))

    # "no local `X()` or `Y()` helper" -> for each backtick `name()`
    # token in the span, file must NOT define `fn name`. Tokens without
    # a trailing `()` (e.g. a struct name caught in the same span when a
    # criterion bundles a struct clause and a helper clause with "and")
    # are skipped - the struct clause has its own pattern above.
    for m in _RECHECK_NO_LOCAL_HELPER_RE.finditer(criterion_visible):
        clause = m.group(1)
        for tok in BACKTICK_TOKEN_RE.findall(clause):
            tok = tok.strip()
            if tok.endswith("()"):
                name = tok[:-2].strip()
                if name:
                    negatives.append(re.compile(r"\bfn\s+" + re.escape(name) + r"\b"))

    # "uses `X::method(...)`" -> file must contain the token's leading
    # identifier-path (matches "EnvVarGuard::unset" out of
    # "EnvVarGuard::unset(\"XERO_API_BASE_URL\")"), so quote style and
    # spacing in the call's arguments don't break the match.
    for m in _RECHECK_USES_RE.finditer(criterion_visible):
        token = m.group(1).strip()
        leading = _RECHECK_USES_LEADING_RE.match(token)
        if leading:
            positives.append(leading.group(0))
        elif token:
            positives.append(token)

    return positives, negatives


def check_test_refactor_satisfied(
    criterion: str, existing_test_refs: list[str]
) -> bool:
    """
    Mechanical (no-AI) check: is this test-refactor criterion already
    satisfied by the current codebase? Returns True only when every
    structural claim the criterion makes can be verified by reading
    the named file(s). Returns False (inconclusive / not satisfied) if
    any check can't be mechanically confirmed - the caller falls
    through to the normal WRITE_TEST path in that case, same as today.

    Works by:
      1. Extracting the named file(s) from the criterion's backtick-
         quoted paths (extract_referenced_paths, same convention as
         extract_plan_context_for_criterion's backtick tokens) - a
         criterion naming a file that doesn't exist returns [] here, so
         this returns False (nothing to read, can't confirm).
      2. Parsing the criterion's visible text (before its trailing
         HTML comment) into positive ("imports X from Z", "uses
         X::method") and negative ("contains no local X struct", "no
         local X() helper") assertions - see
         _parse_test_refactor_assertions. A criterion whose wording
         matches no known pattern yields zero assertions, so this
         returns False (inconclusive -> fall through), never vacuously
         True.
      3. Reading each named file and checking every assertion against
         every file (a single-file criterion - the common case - just
         checks that one; a multi-file criterion requires the claim to
         hold in every named file, the conservative direction: a False
         here only falls through to WRITE_TEST, never pops wrongly).
         Simple text search for positives, regex for negative
         symbol-definition shapes - same deliberate simplicity as
         verify_existing_test_refs_resolve and check_symbol_grounding,
         no AST parsing.

    existing_test_refs is accepted for symmetry with the frame's other
    mechanical checks (and because the caller already has it in hand)
    but isn't itself part of the satisfaction check: a test-refactor
    criterion's existing_test refs name the test(s) being refactored,
    whose *existence* is already verified by grounding
    (verify_existing_test_refs_resolve) and whose *satisfaction* is a
    question about the production/test code the criterion describes,
    not about the refs themselves.
    """
    visible = criterion.split(" <!--", 1)[0]
    positives, negatives = _parse_test_refactor_assertions(visible)
    if not positives and not negatives:
        # No known structural claim to verify - inconclusive, same as a
        # criterion with wording this parser doesn't recognize.
        return False

    paths = extract_referenced_paths(visible)
    if not paths:
        # No file to read (either none named, or none named exists) -
        # can't confirm anything, same conservative fallback.
        return False

    for path_str in paths:
        try:
            content = Path(path_str).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for needle in positives:
            if needle not in content:
                return False
        for pattern in negatives:
            if pattern.search(content):
                return False
    return True


FINDINGS_SECTION_RE = re.compile(
    r"^## Findings\s*\n(.*?)(?:\n## |\Z)", re.DOTALL | re.MULTILINE
)
FINDING_BULLET_TEXT_RE = re.compile(r"^[-*]\s*(?:\[[ xX]?\]\s*)?(.+)$")
FALLBACK_FINDING_RE = re.compile(r"^-\s*\*\*(.+?)\*\*:\s*(.+)$", re.MULTILINE)


def extract_review_findings(review_text: str) -> list[str]:
    """
    Parse a CHANGES REQUESTED review into a list of finding strings, one
    per actionable bullet - mechanical text extraction only, no AI call.

    Primary path: a '## Findings' section (see the Final Answer format
    added to review-singlepass.prompt.md) with one '- [ ] ...' bullet
    per finding. Fallback, for review output that predates or otherwise
    doesn't produce that section: '- **Category**: description' bullets,
    the shape review-singlepass.prompt.md's Step 3 has always produced
    for its findings list.

    Returns [] if neither shape is found - the caller (next_step.py)
    must die_with_log on an empty result rather than silently pushing
    zero frames for a CHANGES REQUESTED verdict.
    """
    match = FINDINGS_SECTION_RE.search(review_text)
    if match:
        findings = []
        for line in match.group(1).splitlines():
            line = line.strip()
            if not LIST_MARKER_RE.match(line):
                continue
            bullet_match = FINDING_BULLET_TEXT_RE.match(line)
            if bullet_match:
                findings.append(bullet_match.group(1).strip())
        if findings:
            return findings

    return [
        f"{category.strip()}: {description.strip()}"
        for category, description in FALLBACK_FINDING_RE.findall(review_text)
    ]


def run_scoped_test(
    qualified_test_name: str, commands: dict, label: str, quiet: bool = False
) -> subprocess.CompletedProcess:
    clean_name = qualified_test_name.strip().strip("`")
    command_str = commands["test_filter_cmd"].format(filter=clean_name)
    return run_command(command_str, label, quiet=quiet)


def run_scoped_tests(
    qualified_test_names: list[str], commands: dict, label: str, quiet: bool = False
) -> list[subprocess.CompletedProcess]:
    """
    Like run_scoped_test, but for a criterion tracking more than one
    test: loops run_scoped_test once per name, in order, so callers can
    zip(qualified_test_names, results) to know which specific test(s)
    are red/green. No toolchain's test_filter_cmd is asked to take more
    than one name at once - N separate subprocess calls is simple and
    correct; a composite OR-filter per toolchain would be a real
    optimization but isn't needed for what should be a small N in
    practice. A single-element list behaves identically to calling
    run_scoped_test directly - this is the general form, not a special
    multi-test path.
    """
    return [
        run_scoped_test(name, commands, f"{label} ({name})", quiet=quiet)
        for name in qualified_test_names
    ]


def run_lint_gate(commands: dict) -> None:
    """
    Runs lint/style checks once, after every criterion is implemented
    and passing - not as acceptance-criteria evidence (see
    the toolchain's evidence_subcommands, which deliberately excludes these; lint
    is a code-quality signal, not a behavioral one).

    Attempts the mechanical fix first: clippy --fix for whatever it can
    auto-apply, then fmt to normalize formatting (including whatever
    clippy --fix just rewrote). This isn't a retry - it's deterministic
    tooling with no judgment call involved, not a second attempt at
    anything an LLM step already tried, so it doesn't conflict with this
    pipeline's single-shot philosophy. Runs over the whole project, test
    files included: cargo fmt/clippy --fix are mechanical tools, not
    agentic writes, so there's no risk of them weakening a test's
    assertions the way an LLM implementer might - the write-protection
    that guards implement steps doesn't apply here.

    Whatever the auto-fix can't resolve (most semantic clippy warnings)
    hits the hard gate below, with no further attempt: dies on failure.
    """
    run_command(commands["clippy_fix_cmd"], "clippy auto-fix")
    run_command(commands["fmt_fix_cmd"], "fmt auto-fix")

    result = run_command(commands["fmt_check_cmd"], "fmt check")
    if result.returncode != 0:
        die_with_log(
            "lint",
            f"cargo fmt --check failed (exit {result.returncode}) even after an "
            f"auto-fix attempt. See output above.",
        )
    result = run_command(commands["clippy_cmd"], "clippy")
    if result.returncode != 0:
        die_with_log(
            "lint",
            f"cargo clippy failed (exit {result.returncode}) even after an "
            f"auto-fix attempt. See output above.",
        )


# ---------------------------------------------------------------------------
# next_step.py's TICKET_VALIDATE phase support - implementation here is
# manual (a human, not tool calls), so there's no automated agent
# tracking written_paths the way every other run_review_gate caller
# does, and an optional smoke-test gate no toolchain default should have
# to define.
# ---------------------------------------------------------------------------


# This pipeline's own scaffolding (ticket/plan/gap-plan/log/stack) ends
# up as real filesystem changes too (write_file_block/save_stack write
# them like any other file) but is never itself "the implementation" -
# excluded from git_changed_files so TICKET_VALIDATE's review gate
# judges the human's actual work, not the artifacts this pipeline wrote
# along the way.
_SCAFFOLDING_PATHS = frozenset(
    str(p)
    for p in (
        TICKET_FILE,
        PLAN_FILE,
        UPDATED_PLAN_FILE,
        GAP_PLAN_FILE,
        PIPELINE_LOG_FILE,
        CRITERIA_STACK_FILE,
        DECLINED_CRITERIA_FILE,
        GIT_STATE_FILE,
    )
)


def git_changed_files() -> list[str]:
    """
    The only source of truth for "what did the human touch" when there's
    no automated agent tracking writes: tracked changes against HEAD
    (staged or not) plus new untracked files, deduped, minus this
    pipeline's own scaffolding files (see _SCAFFOLDING_PATHS). Trusted
    host tooling, not ticket-derived input - run as an argv list same as
    run_command, no shell.
    """
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=False,
    )
    files = tracked.stdout.splitlines() + untracked.stdout.splitlines()
    seen: set[str] = set()
    deduped = []
    for f in files:
        f = f.strip()
        if f and f not in seen and f not in _SCAFFOLDING_PATHS:
            seen.add(f)
            deduped.append(f)
    return deduped


def git_diff_for_file(path: str) -> str:
    """
    Best-effort `git diff -- <path>` output for one file, used to hand
    the test-quality reviewer real before/after evidence when Tester
    modified an existing test rather than writing a new one (see
    run_test_quality_review). Returns "" on any failure (not a tracked
    file yet, git error, etc.) rather than raising - this is advisory
    input to the test-quality reviewer, never worth failing a run over.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--", path], capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return result.stdout if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Git-native workflow (opt-in via .dev-pipeline.toml's git_workflow = true)
# ---------------------------------------------------------------------------
#
# Adds git as a second source of truth alongside the stack: push-ticket
# creates a ticket/<id> branch; next-step commits each criterion on POP;
# TICKET_VALIDATE merges (or opens a PR) after approval; reset-criterion
# rolls one criterion back via `git reset --hard`. All git operations
# live here in pipeline code (never handed to the model as a shell) -
# the same "no shell for the model" rule every other subprocess call in
# this module already follows. Opt-in and off by default, so projects
# with no git repo or no interest in per-criterion commits are entirely
# unaffected - every helper below no-ops or refuses cleanly when
# git_workflow is off.
#
# Two tiers of post-validation handoff: Tier 1 (local merge) needs only
# git. Tier 2 (push + GitHub PR) additionally requires the `gh` CLI,
# installed and authenticated via `gh auth login`; with `gh` absent it
# degrades to a pushed-but-un-PR'd branch with a warning (see
# create_github_pr). Leave pr_on_validate false (the default) to skip
# Tier 2 entirely.
#
# Pipeline state files (.criteria-stack.json, .pipeline-git-state.json,
# ...) are gitignored (see ensure_gitignore_entries) so a `git reset
# --hard` from reset-criterion never destroys the stack - the two
# sources of truth stay orthogonal: the stack is the work *queue*, git
# is the work *done*.


@dataclass
class GitConfig:
    """All keys read from .dev-pipeline.toml's top level (no table). Off
    by default; the whole git-native workflow stays dormant unless
    `git_workflow = true`.

    Tier 1 (local merge on validate) needs nothing beyond git itself.
    Tier 2 (push + open a GitHub PR) additionally requires the `gh` CLI
    to be installed and authenticated (`gh auth login`) - see
    create_github_pr. With `gh` absent, the branch is still pushed and a
    warning is logged; the PR must be opened manually. Set
    `pr_on_validate = false` (the default) to skip Tier 2 entirely.
    """

    git_workflow: bool = False
    base_branch: str | None = None  # merge/PR target; defaults to
    # the branch push-ticket ran on
    git_merge_on_validate: bool = True  # Tier 1: local merge after
    # validation approves. Only
    # meaningful when git_workflow.
    forge: str = "none"  # "none" | "github" (Tier 2).
    # "github" enables PR creation
    # (requires the `gh` CLI).
    forge_remote: str = "origin"  # remote to push the ticket
    # branch to for a PR
    pr_on_validate: bool = False  # Tier 2: push + open a PR
    # instead of a local merge.
    # Requires forge = "github"
    # and the `gh` CLI.
    branch_prefix: str = "ticket/"  # ticket/<id> by default


def load_git_config(config_path: Path) -> GitConfig:
    """
    Reads the git-workflow keys from .dev-pipeline.toml. Returns the
    all-default GitConfig (git_workflow = False) if the file is absent,
    so callers can unconditionally ask `cfg.git_workflow` and branch on
    it without guarding the load itself. Unknown keys are still
    load_pipeline_config's problem (it rejects them before any caller
    reaches here); this only shapes the ones it allows through.
    """
    if not config_path.exists():
        return GitConfig()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    kwargs: dict = {}
    for key in GitConfig.__annotations__:
        if key in data:
            kwargs[key] = data[key]
    try:
        return GitConfig(**kwargs)
    except TypeError as e:
        die(f"{config_path}: invalid git-workflow key: {e}")


def ticket_branch_name(cfg: GitConfig, ticket_id: str) -> str:
    """ticket/<id> by default, or <branch_prefix><id> when configured."""
    return f"{cfg.branch_prefix}{ticket_id}"


# --- pipeline-git-state sidecar (.pipeline-git-state.json) ---------------
# Maps ticket_id -> base_branch, written by push-ticket when it creates
# the ticket branch, read by TICKET_VALIDATE's merge/PR path. A sidecar
# (not a frame field) because base_branch is per-ticket and the sentinel
# frame that would carry it is popped *before* the merge runs.


def load_git_state() -> dict[str, str]:
    if not GIT_STATE_FILE.is_file():
        return {}
    text = GIT_STATE_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def save_git_state(state: dict[str, str]) -> None:
    SCAFFOLD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SCAFFOLD_TEMP_DIR / (GIT_STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, GIT_STATE_FILE)


def record_git_base_branch(ticket_id: str, base_branch: str) -> None:
    state = load_git_state()
    state[ticket_id] = base_branch
    save_git_state(state)


def lookup_git_base_branch(ticket_id: str) -> str | None:
    return load_git_state().get(ticket_id)


def clear_git_base_branch(ticket_id: str) -> None:
    state = load_git_state()
    state.pop(ticket_id, None)
    if state:
        save_git_state(state)
    elif GIT_STATE_FILE.is_file():
        GIT_STATE_FILE.unlink()


# --- low-level git helpers (all argv, no shell) --------------------------


class GitError(Exception):
    """Raised by the git helpers for a non-zero exit or a missing repo.
    Caught by the calling CLI script, which turns it into a die() with
    context - the helpers themselves stay generic and reusable."""


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, cwd=cwd
    )


def git_is_repo() -> bool:
    r = _git("rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def git_current_branch() -> str:
    r = _git("symbolic-ref", "--short", "HEAD")
    if r.returncode != 0:
        # Detached HEAD - fall back to the short SHA.
        return git_current_head()
    return r.stdout.strip()


def git_current_head() -> str:
    r = _git("rev-parse", "HEAD")
    if r.returncode != 0:
        raise GitError(
            f"git rev-parse HEAD failed: {r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout.strip()


def git_status_porcelain() -> str:
    r = _git("status", "--porcelain")
    if r.returncode != 0:
        raise GitError(f"git status failed: {r.stderr.strip()}")
    return r.stdout


def git_is_dirty() -> bool:
    return bool(git_status_porcelain().strip())


# Paths the pipeline itself may create/modify during a run and that must
# NOT read as "uncommitted user work" in a dirty-tree guard: the
# scaffolding state files (git_changed_files already excludes these
# for the review gate) plus .gitignore, which ensure_gitignore_entries
# may append to as part of enabling the very workflow that guard serves.
# A user's real code/test changes still trip the guard; only the
# pipeline's own bookkeeping files are tolerated.
_PIPELINE_MANAGED_PATHS = frozenset(_SCAFFOLDING_PATHS) | {".gitignore"}


def git_user_is_dirty() -> bool:
    """True if the worktree has changes outside the pipeline's own
    managed files (state files + .gitignore). Used by dirty-tree guards
    (push-ticket's branch creation, reset-workflow's revert) so the
    pipeline's own .criteria-stack.json / .gitignore touches don't block
    a reset, while genuine uncommitted user code/test work still does."""
    porcelain = git_status_porcelain()
    if not porcelain.strip():
        return False
    for line in porcelain.splitlines():
        # Porcelain format: "XY <path>" (2 status chars, space, path);
        # untracked is "?? <path>". Renames ("R ...") are rare here and
        # still have a path at [3:] we don't want to misclassify.
        path = line[3:].strip().strip('"')
        if path and path not in _PIPELINE_MANAGED_PATHS:
            return True
    return False


def git_branch_exists(name: str) -> bool:
    r = _git("rev-parse", "--verify", "--quiet", name)
    return r.returncode == 0


def git_create_branch(name: str) -> None:
    r = _git("checkout", "-b", name)
    if r.returncode != 0:
        raise GitError(f"git checkout -b {name} failed: {r.stderr.strip()}")


def git_checkout(name: str) -> None:
    r = _git("checkout", name)
    if r.returncode != 0:
        raise GitError(f"git checkout {name} failed: {r.stderr.strip()}")


def git_add_all() -> None:
    # Stage tracked changes and new files, respecting .gitignore - the
    # pipeline's own state files are gitignored (ensure_gitignore_entries)
    # so they're never staged into a criterion commit.
    r = _git("add", "--all")
    if r.returncode != 0:
        raise GitError(f"git add --all failed: {r.stderr.strip()}")


def git_has_staged_changes() -> bool:
    r = _git("diff", "--cached", "--quiet")
    return r.returncode != 0  # exit 1 means there are staged changes


def git_commit(message: str) -> str | None:
    """Stage all changes and commit. Returns the new commit SHA, or None
    if there was nothing to stage (an empty-diff POP - e.g. a sibling
    criterion's commit already covered this one's changes). Never
    raises on an empty stage; a real git error does raise."""
    git_add_all()
    if not git_has_staged_changes():
        return None
    r = _git("commit", "-m", message)
    if r.returncode != 0:
        raise GitError(f"git commit failed: {r.stderr.strip()}")
    return git_current_head()


def git_reset_hard(sha: str) -> None:
    r = _git("reset", "--hard", sha)
    if r.returncode != 0:
        raise GitError(f"git reset --hard {sha} failed: {r.stderr.strip()}")


def git_merge_no_ff(branch: str) -> None:
    r = _git("merge", "--no-ff", branch, "-m", f"Merge {branch}")
    if r.returncode != 0:
        raise GitError(f"git merge --no-ff {branch} failed: {r.stderr.strip()}")


def git_branch_delete(name: str) -> None:
    r = _git("branch", "-d", name)
    if r.returncode != 0:
        # -d refuses an unmerged branch; -D would force. A failed delete
        # is non-fatal - the branch just sticks around.
        log.warning(
            "-- git branch -d %s failed (non-fatal): %s", name, r.stderr.strip()
        )


def git_push(remote: str, branch: str, force: bool = False) -> None:
    args = ["push", remote, branch]
    if force:
        args.insert(1, "--force")
    r = _git(*args)
    if r.returncode != 0:
        raise GitError(f"git push {remote} {branch} failed: {r.stderr.strip()}")


# --- git-native workflow orchestration (called by next_step.py) -----------


def criterion_commit_message(cfg: GitConfig, ticket_id: str, criterion: str) -> str:
    """Build the per-criterion commit message: `ticket/<id>: <summary>`.
    The summary is the criterion text trimmed to a readable width; the
    leading `- [ ]` checkbox marker is stripped so the subject line
    reads as prose, not a todo bullet."""
    summary = criterion.strip()
    for prefix in ("- [ ]", "- [x]", "-"):
        if summary.startswith(prefix):
            summary = summary[len(prefix) :].strip()
            break
    if len(summary) > 72:
        summary = summary[:69] + "..."
    return f"{cfg.branch_prefix}{ticket_id}: {summary}"


def commit_criterion(cfg: GitConfig, ticket_id: str, criterion: str) -> str | None:
    """Layer 2: stage all changes and commit one criterion's worth of
    work. Returns the new commit SHA, or None if there was nothing to
    stage (an empty-diff POP - e.g. a sibling criterion's commit already
    covered this one's changes, or the human committed manually). Never
    blocks a POP: a None is logged as a skip, not an error, since the
    criterion is already verified green - that's the gate, not the
    commit. Raises GitError only on a real git failure."""
    message = criterion_commit_message(cfg, ticket_id, criterion)
    sha = git_commit(message)
    if sha is None:
        log.info("-- git_workflow: no changes to commit for this criterion (skipped).")
    else:
        log.info("-- git_workflow: committed criterion as %s.", sha[:8])
    return sha


def _gh_available() -> bool:
    r = subprocess.run(["gh", "--version"], capture_output=True, text=True, check=False)
    return r.returncode == 0


def create_github_pr(
    cfg: GitConfig,
    ticket_id: str,
    branch: str,
    base: str,
    title: str | None,
    body: str | None,
) -> None:
    """Layer 3 Tier 2: push the ticket branch and open a GitHub PR via
    the `gh` CLI - the simplest robust path, reusing whatever auth `gh`
    already has (run `gh auth login` once) rather than managing a token
    here.

    Prerequisite: the `gh` CLI installed and authenticated. With `gh`
    absent the branch is *still pushed* (the `git push` runs before the
    `gh` check) and a warning is logged prompting a manual PR - a
    non-fatal degradation, never a crash. To avoid the push-without-PR
    state entirely, either install `gh` or leave `pr_on_validate`
    false (the default) and rely on Tier 1's local merge.
    """
    git_push(cfg.forge_remote, branch)
    render.print_line(f"-- git_workflow: pushed {branch} to {cfg.forge_remote}.")
    if not _gh_available():
        log.warning(
            "-- git_workflow: `gh` CLI not found - branch pushed but no PR "
            "created. Install gh or open the PR manually."
        )
        return
    args = ["pr", "create", "--base", base, "--head", branch]
    if title:
        args += ["--title", title]
    if body:
        args += ["--body", body]
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        log.warning(
            "-- git_workflow: gh pr create failed (non-fatal): %s. Branch "
            "is pushed; open the PR manually.",
            (r.stderr or r.stdout).strip(),
        )
    else:
        render.print_line(f"-- git_workflow: opened PR - {r.stdout.strip()}")


def post_validate_git(
    cfg: GitConfig,
    ticket_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
) -> None:
    """Layer 3: after TICKET_VALIDATE approves, merge the ticket branch
    back to its base (Tier 1) or push + open a PR (Tier 2). No-op when
    git_workflow is off. Leaves the working tree on the base branch on a
    local merge, or on the ticket branch after a PR (review happens on
    the remote). Clears the per-ticket base_branch sidecar once the
    branch is merged locally - a PR keeps it so a later local merge of
    the same branch still knows where to land.

    Tier 1 (git_merge_on_validate, default) needs only git. Tier 2
    (pr_on_validate = true + forge = "github") needs the `gh` CLI; with
    `gh` missing it degrades to a pushed branch + warning (see
    create_github_pr), never a crash."""
    if not cfg.git_workflow:
        return
    branch = ticket_branch_name(cfg, ticket_id)
    if not git_branch_exists(branch):
        log.info(
            "-- git_workflow: ticket branch %s not found - nothing to merge.", branch
        )
        return
    base = lookup_git_base_branch(ticket_id) or cfg.base_branch or git_current_branch()

    if cfg.pr_on_validate and cfg.forge == "github":
        try:
            create_github_pr(cfg, ticket_id, branch, base, title, body)
        except GitError as e:
            log.warning("-- git_workflow: PR creation failed (non-fatal): %s", e)
        return

    if not cfg.git_merge_on_validate:
        log.info(
            "-- git_workflow: git_merge_on_validate is off - leaving %s "
            "unmerged. Merge it manually into %s when ready.",
            branch,
            base,
        )
        return

    try:
        git_checkout(base)
        git_merge_no_ff(branch)
        render.print_line(f"-- git_workflow: merged {branch} into {base}.")
        git_branch_delete(branch)
        clear_git_base_branch(ticket_id)
    except GitError as e:
        # A merge conflict or checkout failure is non-fatal to the
        # validation verdict itself (the work is done and approved) -
        # surface it loudly and leave the branch for a manual merge.
        log.warning(
            "-- git_workflow: post-validate merge failed (non-fatal): %s. "
            "Branch %s is intact; merge it into %s manually.",
            e,
            branch,
            base,
        )


# --- .gitignore management -------------------------------------------------
# Ensures the pipeline's own state files are gitignored so a `git reset
# --hard` (reset-criterion) or `git add --all` (commit-on-POP) never
# touches or captures them. Idempotent: only appends entries that aren't
# already present, and creates .gitignore if it doesn't exist.

_GITIGNORE_ENTRIES = (
    ".scaffold/",
    str(CRITERIA_STACK_FILE),
    str(GIT_STATE_FILE),
    str(DECLINED_CRITERIA_FILE),
    str(PIPELINE_LOG_FILE),
    str(TICKET_FILE),
    str(PLAN_FILE),
    str(UPDATED_PLAN_FILE),
    str(GAP_PLAN_FILE),
)


def ensure_gitignore_entries() -> None:
    """Append any missing pipeline-state entries to .gitignore. Creates
    the file with a header if it doesn't exist. No-op when not in a git
    repo (no .git directory in cwd) - nothing to ignore from."""
    if not Path(".git").exists():
        return
    gitignore = Path(".gitignore")
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing]
    if not missing:
        return
    block = (
        "\n# --- ticket-pipeline state (git-native workflow) ---------\n"
        + "\n".join(missing)
        + "\n"
    )
    with gitignore.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(block)
    log.info("-- Added %d pipeline-state entr(y/ies) to .gitignore.", len(missing))


def load_smoke_cmd(config_path: Path) -> str | None:
    """
    Peeks for an optional 'smoke_cmd' key in the project-local pipeline
    config, outside load_pipeline_config's strict per-toolchain schema -
    no toolchain defines a smoke command (see toolchains.py), and none
    should have to just to support an optional gate. Returns None if the
    config file doesn't exist or doesn't set this key.
    """
    if not config_path.exists():
        return None
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    value = data.get("smoke_cmd")
    return value if isinstance(value, str) and value.strip() else None


def load_step_models(config_path: Path) -> dict[str, str]:
    """
    Per-step model overrides from .dev-pipeline.toml's [step_models]
    table. Keys are step names (review, plan, narrow); values are model
    IDs. Missing keys fall back to the --model default. Only used when
    --model is NOT passed on the command line — a CLI model override
    takes precedence over this table for all steps.
    """
    if not config_path.exists():
        return {}
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    table = data.get("step_models", {})
    if not isinstance(table, dict):
        return {}
    return {k: v for k, v in table.items() if isinstance(v, str) and v.strip()}


def run_smoke_gate(smoke_cmd: str | None) -> None:
    """
    Stub: no real process-lifecycle smoke testing yet (starting a
    server, probing a health endpoint, tearing it down is qualitatively
    different work than a single command's exit code). Skips cleanly if
    unset; if a project does set smoke_cmd, runs and gates on it the
    same shape as every other gate in this module.
    """
    if smoke_cmd is None:
        log.info("-- Smoke test: skipped (no smoke_cmd configured)")
        return
    result = run_command(smoke_cmd, "smoke test")
    if result.returncode != 0:
        die_with_log(
            "smoke", f"Smoke test failed (exit {result.returncode}). See output above."
        )


# ---------------------------------------------------------------------------
# Per-criterion interactive explore step (--explore in push_ticket).
# run_explore_for_criterion runs one interactive session for a single frame,
# producing a context string to append to that frame's plan_context.
# run_explore_for_frames iterates the full list, calling
# run_explore_for_criterion for each, mutating plan_context in place before
# the stack is written. Both are called only when the human passed --explore
# to push_ticket; every other push path never touches these functions.
# ---------------------------------------------------------------------------

EXPLORE_CONTEXT_HEADING = "### Context From Exploration & Discussion"
_EXPLORE_CONTEXT_RE = re.compile(
    r"^### Context From Exploration & Discussion\s*\n(.*?)(?:\n### |\Z)",
    re.DOTALL | re.MULTILINE,
)
_EXPLORE_GAP_RE = re.compile(
    r"^### Spec Gaps Noticed\s*\n(.+?)(?:\n### |\Z)",
    re.DOTALL | re.MULTILINE,
)

# Same dedup-key convention used by the explore step in push_ticket - the ticket was
# embedded in the prompt already; a read_file call for it returns the
# short "you already have this" note instead of re-sending the content.
_EXPLORE_TICKET_DEDUP_KEY = ".ticket.md"


def build_explore_criterion_prompt(criterion: str, plan_context: str) -> str:
    """
    Build the prompt for an interactive per-criterion context-scaffolding
    session. The existing plan_context (extracted from the gap plan at frame-
    build time) is given as a starting point the model will extend, not
    replace.
    """
    instructions = load_prompt_body(EXPLORE_CRITERION_PROMPT_FILE)
    prefetch_block, _ = prefetch_referenced_files(criterion + "\n" + plan_context)
    prefetch_section = f"\n\n{prefetch_block}" if prefetch_block else ""
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the acceptance criterion to scaffold context for:\n\n"
        f"{criterion}\n\n"
        f"Here is the existing plan context for this criterion - already "
        f"complete and current, treat it as a starting point:\n\n"
        f"{plan_context}{prefetch_section}\n\n"
        f"Explore the codebase with read_file/list_dir/search_files and "
        f"ask the human targeted questions with ask_user_question - one "
        f"at a time, waiting for each real answer - until you have enough "
        f"implementation context for this criterion. Produce the result "
        f"in the exact format from Step 5 above. Your final response (no "
        f"further tool calls) must be exactly that output - no chat "
        f"header, no preamble or trailing commentary."
    )


def run_explore_for_criterion(criterion: str, plan_context: str, model: str) -> str:
    """
    Run one interactive context-scaffolding session for a single criterion.
    Returns the context string to append to the frame's plan_context: the
    full '### Context From Exploration & Discussion' block (and any
    '### Spec Gap Noticed' block) from the model's response, ready to
    concatenate with a blank line separator. Returns an empty string if
    the model produced a valid response but found nothing to add.

    Propagates AIError and PipelineAbort to the caller (push_ticket.py),
    which decides whether to abort or continue with the remaining frames.
    """
    from . import ai_client as _ai_client

    result = run_ai_step_with_retry(
        lambda: _ai_client.run_with_tools(
            build_explore_criterion_prompt(criterion, plan_context),
            tools.EXPLORE_TOOLS,
            tools.make_executor(
                allow_write=False,
                interactive=True,
                preloaded_paths={_EXPLORE_TICKET_DEDUP_KEY},
            ),
            "explore-criterion",
            model=model,
            summarize_call=tools.summarize_tool_call,
        ),
        "explore-criterion",
    )
    if EXPLORE_CONTEXT_HEADING not in result.text:
        render.print_line(
            f"-- explore-criterion: response had no context heading - "
            f"skipping criterion (raw output above)."
        )
        render_step_output(result.text, level=1)
        return ""

    # Extract both the context block and any spec-gap notice to append.
    parts: list[str] = []
    ctx_match = _EXPLORE_CONTEXT_RE.search(result.text)
    if ctx_match:
        body = ctx_match.group(1).strip()
        if body:
            parts.append(f"{EXPLORE_CONTEXT_HEADING}\n\n{body}")
        else:
            parts.append(EXPLORE_CONTEXT_HEADING)
    gap_match = _EXPLORE_GAP_RE.search(result.text)
    if gap_match:
        parts.append(f"### Spec Gaps Noticed\n{gap_match.group(1).strip()}")
    return "\n\n".join(parts)


def run_explore_for_frames(frames: "list[CriterionFrame]", model: str) -> None:
    """
    Run an interactive context-scaffolding session for each frame in
    `frames`, appending the returned context to each frame's plan_context
    in place. Frames are explored in stack order (index 0 first). A
    failure on one frame is logged and skipped; the remaining frames are
    still explored so that a single bad response doesn't silently discard
    context gathered for all subsequent criteria.

    Called by push_ticket.py when --explore is passed, after frame-building
    but before the stack is written.
    """
    total = len(frames)
    for i, frame in enumerate(frames):
        render.print_line()
        render.print_line(
            f"-- explore ({i + 1}/{total}): {frame.criterion[:80]}"
            + ("..." if len(frame.criterion) > 80 else "")
        )
        try:
            extra = run_explore_for_criterion(
                frame.criterion, frame.plan_context, model
            )
        except (ai_client.AIError, tools.PipelineAbort) as e:
            render.print_line(
                f"-- explore-criterion: skipping criterion after error: {e}"
            )
            continue
        if extra:
            frame.plan_context = frame.plan_context.rstrip() + "\n\n" + extra


# ---------------------------------------------------------------------------
# Per-criterion test step, and the final review gate. run_test_for_criterion
# (and its compile-retry variant) are next_step.py's WRITE_TEST phase; there
# is no per-criterion implement step - next_step always pauses for a human
# to implement (see the module docstring). `plan_context` here is a frame's
# already-scoped Implementation Plan excerpt (see
# extract_plan_context_for_criterion below), not the full gap plan - the
# caller (push_ticket.py, at frame-build time) does the scoping once, so
# this layer doesn't need to strip anything out of a full plan itself.
#
# run_test_for_criterion_with_full_retry is the WRITE_TEST phase's current
# entry point: a single three-gate loop (compile -> red/green -> quality
# review) sharing one bounded attempt budget, with the quality review
# gating on both red and green tests and falling back to advisory on
# budget exhaustion. run_test_for_criterion_with_compile_retry is the
# older compile-gate-only variant (kept for bench, which calls the plain
# run_test_for_criterion instead).
# ---------------------------------------------------------------------------

TEST_WITNESS_RE = re.compile(r"TEST_WITNESS:\s*(.+?)\s*::\s*(.+)$", re.MULTILINE)


def _parse_test_witnesses(text: str) -> list[tuple[str, str]]:
    """
    Every "TEST_WITNESS: <file> :: <name>" line in the Tester's output,
    in the order written - almost always one, since almost every
    criterion needs exactly one test; more than one only when the
    criterion's own behavior spans call paths/subjects that couldn't
    share a single test function (test-criterion.prompt.md's Step 3).
    The line format itself is unchanged from the single-test era; the
    only change is capturing every match instead of just the first.
    """
    return [
        (file_path.strip().strip("`"), test_name.strip().strip("`"))
        for file_path, test_name in TEST_WITNESS_RE.findall(text)
    ]


def build_test_criterion_prompt(
    criterion: str,
    plan_context: str,
    existing_test_refs: list[str] | None = None,
    verification: str = "test",
) -> str:
    instructions = load_prompt_body(TEST_CRITERION_PROMPT_FILE)
    if verification == "test-refactor":
        existing_test_section = (
            f"\n\nThis criterion is about refactoring test code structure "
            f"- the test's assertions should remain functionally "
            f"identical. Rewrite the existing test(s) to match the "
            f"criterion's structural requirements without adding new "
            f"assertions or source-scanning checks. The test(s) to "
            f"rewrite: {', '.join(existing_test_refs)}."
            if existing_test_refs
            else ""
        )
        write_instruction = (
            "Rewrite the existing test(s) for exactly this one acceptance "
            "criterion - this is a test-refactoring criterion, not a "
            "behavior change. The test's assertions should remain "
            "functionally the same; change only the structural elements "
            "the criterion describes. The test should pass (GREEN) after "
            "the rewrite."
        )
    else:
        existing_test_section = (
            f"\n\nThis criterion is about changing behavior existing test(s) "
            f"already cover, not adding new coverage - modify {'that test' if len(existing_test_refs) == 1 else 'those tests'} "
            f"instead of writing a new one (see this prompt's own instructions "
            f"for exactly how). The test(s) to change: {', '.join(existing_test_refs)}."
            if existing_test_refs
            else ""
        )
        write_instruction = (
            "Write a failing test for exactly this one acceptance "
            "criterion, and only this one:"
        )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"{write_instruction}\n\n{criterion}"
        f"{existing_test_section}\n\n{_HOST_PLATFORM_NOTE}"
    )


def build_test_feedback_prompt(
    criterion: str,
    plan_context: str,
    feedback: str,
    previous_changed_files: list[str],
    existing_test_refs: list[str] | None = None,
    verification: str = "test",
) -> str:
    instructions = load_prompt_body(TEST_REFINE_PROMPT_FILE)
    changed_block = (
        "\n".join(f"- {p}" for p in previous_changed_files) or "- (none recorded)"
    )
    existing_tests_block = (
        "\n".join(f"- {ref}" for ref in (existing_test_refs or [])) or "- (none named)"
    )
    mode_note = (
        "This is a test-refactoring criterion: preserve the existing "
        "assertions' behavior and adjust only the structural elements the "
        "criterion describes."
        if verification == "test-refactor"
        else "This feedback applies to the test-writing step only. Preserve "
        "the criterion exactly; change only the tests needed to address "
        "the feedback."
    )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this criterion:\n\n"
        f"{plan_context}\n\n"
        f"Acceptance criterion (fixed; do not rewrite it):\n\n{criterion}\n\n"
        f"{mode_note}\n\n"
        f"Existing test refs for this criterion:\n{existing_tests_block}\n\n"
        f"Files changed in the previous attempt (read these first if they still exist):\n"
        f"{changed_block}\n\n"
        f"User feedback to address:\n\n{feedback}\n\n"
        f"Make the smallest targeted correction to the prior test-writing "
        f"attempt. Do not broaden scope beyond this one criterion.\n\n"
        f"{_HOST_PLATFORM_NOTE}"
    )


def run_test_for_criterion(
    criterion: str, plan_context: str, model: str, ticket_id: str | None = None
) -> tuple[list[str], list[str]]:
    test_files: list[str] = []

    def attempt():
        test_files.clear()
        return run_with_tools(
            build_test_criterion_prompt(criterion, plan_context),
            tools.READ_WRITE_TOOLS,
            tools.make_executor(written_paths=test_files),
            "test-criterion",
            model=model,
            summarize_call=tools.summarize_tool_call,
        )

    try:
        result = run_ai_step_with_retry(
            attempt, "test-criterion", criterion=criterion, ticket=ticket_id
        )
    except (AIError, tools.PipelineAbort) as e:
        die_with_log("test-criterion", str(e), criterion=criterion, ticket=ticket_id)
    render_step_output(result.text)
    if not test_files:
        die_with_log(
            "test-criterion",
            "Tester finished without writing any test files.",
            criterion=criterion,
            ticket=ticket_id,
        )
    witnesses = _parse_test_witnesses(result.text)
    if not witnesses:
        die_with_log(
            "test-criterion",
            "Tester's final answer did not include a TEST_WITNESS line (see output above).",
            criterion=criterion,
            ticket=ticket_id,
        )
    return [w[0] for w in witnesses], [w[1] for w in witnesses]


def build_test_criterion_fix_prompt(
    criterion: str, plan_context: str, file_paths: list[str], error_output: str
) -> str:
    instructions = load_prompt_body(TEST_CRITERION_PROMPT_FILE)
    file_list = ", ".join(file_paths)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"You already wrote (a) failing test(s) for exactly this one "
        f"acceptance criterion, and only this one:\n\n{criterion}\n\n"
        f"but the test suite does not compile. read_file {file_list} and "
        f"fix {'it' if len(file_paths) == 1 else 'them'} with write_file so "
        f"the suite compiles and every test you wrote still fails for the "
        f"right reason - missing or incorrect behaviour, not a syntax or "
        f"type error. Do not weaken, skip, or remove any test, and do not "
        f"implement the production behaviour they're testing for. The "
        f"criterion must remain covered exactly as before; only the "
        f"compile error should be fixed.\n\n"
        f"Compile error:\n\n```\n{error_output}\n```\n\n{_HOST_PLATFORM_NOTE}"
    )


def build_test_criterion_quality_fix_prompt(
    criterion: str,
    plan_context: str,
    file_paths: list[str],
    test_names: list[str],
    quality_concern: str,
    test_was_green: bool,
) -> str:
    instructions = load_prompt_body(TEST_CRITERION_PROMPT_FILE)
    file_list = ", ".join(file_paths)
    green_note = (
        "Your test passed immediately (green) when run against the current "
        "code, but the reviewer says it doesn't actually exercise the "
        "criterion. This could mean the test is tautological or trivially "
        "satisfiable, or that the criterion is genuinely already satisfied - "
        "either way, amend the test to genuinely verify the behaviour "
        "described. If the behaviour truly is already implemented, the "
        "amended test should still pass, but by actually exercising the real "
        "code path - not by asserting a value the test itself set."
        if test_was_green
        else "Your test is red (good - it detects a real gap), but the reviewer "
        "says it doesn't properly cover the criterion. Amend it to actually "
        "exercise the behaviour described."
    )
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"You already wrote (a) test(s) for exactly this one acceptance "
        f"criterion, and only this one:\n\n{criterion}\n\n"
        f"but an independent quality reviewer flagged "
        f"{'it' if len(file_paths) == 1 else 'them'} as not properly "
        f"covering the criterion's behaviour. {green_note}\n\n"
        f"Do not weaken, skip, or remove any test, and do not implement "
        f"the production behaviour. The criterion must remain covered "
        f"exactly; only the quality concern should be addressed.\n\n"
        f"read_file {file_list} and fix {'it' if len(file_paths) == 1 else 'them'} "
        f"with write_file so the suite still compiles and the criterion is "
        f"genuinely exercised.\n\n"
        f"Reviewer's concern:\n\n{quality_concern}\n\n{_HOST_PLATFORM_NOTE}"
    )


def run_test_for_criterion_with_compile_retry(
    criterion: str,
    plan_context: str,
    model: str,
    commands: dict,
    max_attempts: int = 3,
    ticket_id: str | None = None,
    existing_test_refs: list[str] | None = None,
) -> tuple[list[str], list[str], subprocess.CompletedProcess]:
    """
    Like run_test_for_criterion, but also gates the written test(s) on
    compilation and, if it fails to compile, feeds the compile error
    back to the Tester for a fix attempt instead of dying immediately -
    up to max_attempts attempts *total* (the initial write plus every
    fix retry counts against the same budget, not on top of it). This
    explicitly trades the pipeline's normal fail-fast-on-compile-error
    behaviour for a bounded self-correction loop: if max_attempts is
    exhausted with the suite still not compiling, the caller gets back
    the last (still-failing) CompletedProcess to report and die on,
    rather than this function retrying forever. next_step.py's
    WRITE_TEST phase uses this variant, not the plain one above, so a
    flaky first compile doesn't immediately abort the whole run.

    Returns parallel lists (file_paths, test_names), same order as the
    TEST_WITNESS lines Tester emitted - almost always length 1.

    existing_test_refs (frame.existing_test_refs, see
    extract_existing_test_refs): when non-empty, the initial prompt
    tells Tester to modify those specific existing tests rather than
    writing new ones - only affects the attempt-1 prompt. A
    compile-retry fix prompt already says "read_file {files} and fix
    them" regardless of whether they're brand-new files or modified
    existing ones, so it needs no separate wording for this case.
    """
    test_files: list[str] = []
    file_paths: list[str] = []
    test_names: list[str] = []
    last_error: str | None = None
    compile_result: subprocess.CompletedProcess | None = None

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            prompt = build_test_criterion_prompt(
                criterion, plan_context, existing_test_refs
            )
        else:
            log.warning(
                "-- Compile failed (attempt %d/%d). Feeding the compile error back to Tester to fix.",
                attempt - 1,
                max_attempts,
            )
            prompt = build_test_criterion_fix_prompt(
                criterion, plan_context, file_paths, last_error
            )

        def attempt_step():
            test_files.clear()
            return run_with_tools(
                prompt,
                tools.READ_WRITE_TOOLS,
                tools.make_executor(written_paths=test_files),
                "test-criterion",
                model=model,
                summarize_call=tools.summarize_tool_call,
            )

        try:
            result = run_ai_step_with_retry(
                attempt_step, "test-criterion", criterion=criterion, ticket=ticket_id
            )
        except (AIError, tools.PipelineAbort) as e:
            die_with_log(
                "test-criterion", str(e), criterion=criterion, ticket=ticket_id
            )
        render_step_output(result.text)
        if not test_files:
            die_with_log(
                "test-criterion",
                "Tester finished without writing any test files.",
                criterion=criterion,
                ticket=ticket_id,
            )
        witnesses = _parse_test_witnesses(result.text)
        if not witnesses:
            die_with_log(
                "test-criterion",
                "Tester's final answer did not include a TEST_WITNESS line (see output above).",
                criterion=criterion,
                ticket=ticket_id,
            )
        file_paths, test_names = [w[0] for w in witnesses], [w[1] for w in witnesses]

        compile_result = run_command(
            commands["test_compile_cmd"],
            f"test compile gate (attempt {attempt}/{max_attempts})",
        )
        if compile_result.returncode == 0:
            return file_paths, test_names, compile_result

        last_error = (compile_result.stdout or "") + (compile_result.stderr or "")
        # No token/cost delta here deliberately: this event is about the
        # compile gate (a subprocess, zero AI cost of its own) failing,
        # not about the Tester call above it - that call already got its
        # own "success" event (with its real cost) from run_ai_step_with_retry
        # regardless of whether the test it wrote went on to compile.
        log_event(
            "test-criterion",
            "retry",
            error=f"compile failed (attempt {attempt}/{max_attempts})",
            criterion=criterion,
            ticket=ticket_id,
        )

    return file_paths, test_names, compile_result


def _run_tester_step(
    prompt: str,
    model: str,
    ticket_id: str | None,
    criterion: str,
    no_files_is_fatal: bool = True,
) -> tuple[list[str] | None, list[str] | None]:
    """
    Shared "run the Tester once on this prompt, parse its TEST_WITNESS
    lines" core used by both run_test_for_criterion_with_compile_retry
    and run_test_for_criterion_with_full_retry. Returns parallel
    (file_paths, test_names) lists in witness order; dies on a tool/AI
    failure or a run with no witness line - same fail-fast contract
    every Tester call already had, factored out only to keep the two
    retry loops from duplicating ~25 lines.

    A run that writes no files is, by default, also fatal
    (die_with_log) - the original fail-fast behaviour, which
    run_test_for_criterion_with_compile_retry still relies on. But
    run_test_for_criterion_with_full_retry passes
    no_files_is_fatal=False: a Tester that writes nothing is a strong
    signal the criterion may already be satisfied (it re-read the
    code and found no gap to write a test for), so that variant needs
    the run to *return* a (None, None) sentinel the caller can recover
    from (see do_write_test/_handle_no_test_written) rather than taking
    the whole pipeline down. A run with files but no TEST_WITNESS line
    is a malformed response, not a satisfaction signal, so it stays
    fatal regardless of this flag.
    """
    test_files: list[str] = []

    def attempt_step():
        test_files.clear()
        return run_with_tools(
            prompt,
            tools.READ_WRITE_TOOLS,
            tools.make_executor(written_paths=test_files),
            "test-criterion",
            model=model,
            summarize_call=tools.summarize_tool_call,
        )

    try:
        result = run_ai_step_with_retry(
            attempt_step, "test-criterion", criterion=criterion, ticket=ticket_id
        )
    except (AIError, tools.PipelineAbort) as e:
        die_with_log("test-criterion", str(e), criterion=criterion, ticket=ticket_id)
    render_step_output(result.text)
    if not test_files:
        if no_files_is_fatal:
            die_with_log(
                "test-criterion",
                "Tester finished without writing any test files.",
                criterion=criterion,
                ticket=ticket_id,
            )
        # Non-fatal path: the Tester judged the criterion already
        # satisfied (or otherwise saw nothing to write). Return the
        # sentinel so the caller's recovery path can decide whether to
        # pop or pause - never retry here, since a fix prompt presupposes
        # a file to fix and there's nothing to feed back.
        log_event(
            "test-criterion",
            "no-test-written",
            criterion=criterion,
            ticket=ticket_id,
        )
        return None, None
    witnesses = _parse_test_witnesses(result.text)
    if not witnesses:
        die_with_log(
            "test-criterion",
            "Tester's final answer did not include a TEST_WITNESS line (see output above).",
            criterion=criterion,
            ticket=ticket_id,
        )
    return [w[0] for w in witnesses], [w[1] for w in witnesses]


def run_test_for_criterion_with_full_retry(
    criterion: str,
    plan_context: str,
    model: str,
    commands: dict,
    max_attempts: int = 5,
    retry_policy: RetryPolicy | None = None,
    ticket_id: str | None = None,
    existing_test_refs: list[str] | None = None,
    verification: str = "test",
    feedback: str | None = None,
    previous_changed_files: list[str] | None = None,
) -> tuple[
    list[str] | None,
    list[str] | None,
    list[subprocess.CompletedProcess],
    subprocess.CompletedProcess | None,
    str | None,
]:
    """
    The WRITE_TEST phase's unified loop: gates a freshly-written test on
    three checks in sequence, sharing one bounded attempt budget across
    all of them:

      Gate 1 - compile (test_compile_cmd). On failure, feed the compile
               error back to the Tester via build_test_criterion_fix_prompt.
      Gate 2 - run the scoped tests (run_scoped_tests) to observe
               red/green. Observation only here; the caller
               (do_write_test) dispatches on the returned results.
      Gate 3 - quality review (run_test_quality_review), run on *both*
               red and green tests. On FLAGGED, feed the reviewer's
               concern back to the Tester via
               build_test_criterion_quality_fix_prompt (telling it
               whether the flagged test was red or green).

    A clean pass on all three exits the loop early. If the budget is
    exhausted, the outcome depends on which gate is still failing:
    compile still failing is fatal (the caller dies on the returned
    compile_result, same as run_test_for_criterion_with_compile_retry
    always did); quality still flagged falls back to advisory - the
    concern is returned to the caller to print and log, and the test is
    accepted. A failed quality-review AI call degrades to None inside
    run_test_quality_review, which this loop treats as "gate passed" -
    better to proceed than die on an infrastructure failure in a side
    channel.

    Returns (file_paths, test_names, test_results, compile_result,
    quality_concern):
      - file_paths/test_names: parallel lists in witness order. Both are
        None when the Tester wrote no files at all (see below) - the
        sentinel do_write_test dispatches to _handle_no_test_written
        for, rather than treating as a normal test-written result.
      - test_results: one CompletedProcess per test from the last
        iteration that reached Gate 2 (what do_write_test needs for
        red/green dispatch). Empty if compile never succeeded, or if
        the Tester wrote nothing (sentinel path).
      - compile_result: the last compile gate's CompletedProcess (None
        only if the loop never ran a compile gate, including the
        no-test-written sentinel path). Caller checks
        returncode != 0 for the fatal path.
      - quality_concern: None if the quality review passed (or was
        never reached due to compile failure or the no-test-written
        sentinel); the flagged concern string if the loop exhausted
        with quality still flagged (advisory fallback).

    The no-test-written sentinel (file_paths is None) is returned
    immediately - the loop does not retry on it. A Tester that writes
    nothing is signalling the criterion may already be satisfied (it
    re-read the code and found no gap); the compile/quality fix prompts
    presuppose a file to fix, so retrying them is pointless. The caller
    decides whether to pop (recovery path confirmed satisfaction) or
    pause for a human.

    existing_test_refs affects only the attempt-1 prompt (same as the
    compile-retry variant); fix prompts already say "read_file {files}
    and fix them" regardless of new vs. modified.
    """
    from .retry import FixedBudgetPolicy

    policy = retry_policy or FixedBudgetPolicy(max_attempts)
    limit_desc = policy.describe_limit()

    file_paths: list[str] = []
    test_names: list[str] = []
    failure_kind: str | None = None  # "compile" | "quality-red" | "quality-green"
    last_error: str | None = None  # compile error OR quality concern text
    compile_result: subprocess.CompletedProcess | None = None
    test_results: list[subprocess.CompletedProcess] = []
    quality_concern: str | None = None

    attempt = 0
    while True:
        attempt += 1
        # -- Select prompt ------------------------------------------------
        if attempt == 1:
            if feedback:
                prompt = build_test_feedback_prompt(
                    criterion,
                    plan_context,
                    feedback,
                    previous_changed_files or [],
                    existing_test_refs,
                    verification=verification,
                )
            else:
                prompt = build_test_criterion_prompt(
                    criterion,
                    plan_context,
                    existing_test_refs,
                    verification=verification,
                )
        elif failure_kind == "compile":
            log.warning(
                "-- Compile failed (attempt %d, %s). Feeding the compile error back to Tester to fix.",
                attempt - 1,
                limit_desc,
            )
            prompt = build_test_criterion_fix_prompt(
                criterion, plan_context, file_paths, last_error
            )
        elif failure_kind in ("quality-red", "quality-green"):
            log.warning(
                "-- Test-quality review flagged (attempt %d, %s). Feeding the concern back to Tester.",
                attempt - 1,
                limit_desc,
            )
            prompt = build_test_criterion_quality_fix_prompt(
                criterion,
                plan_context,
                file_paths,
                test_names,
                last_error,
                test_was_green=(failure_kind == "quality-green"),
            )

        # -- Run Tester ---------------------------------------------------
        file_paths, test_names = _run_tester_step(
            prompt,
            model,
            ticket_id,
            criterion,
            no_files_is_fatal=False,
        )
        if file_paths is None:
            # Tester wrote nothing - a strong signal the criterion may
            # already be satisfied (it re-read the code and found no gap
            # to write a test for). Don't burn the rest of the attempt
            # budget retrying: the fix prompts presuppose a file to fix,
            # and there's nothing to feed back. Propagate the sentinel
            # (None file_paths) straight to the caller's recovery path
            # (do_write_test -> _handle_no_test_written).
            return None, None, [], None, None

        # -- Gate 1: compile ---------------------------------------------
        compile_result = run_command(
            commands["test_compile_cmd"],
            f"test compile gate (attempt {attempt}, {limit_desc})",
        )
        if compile_result.returncode != 0:
            failure_kind = "compile"
            last_error = (compile_result.stdout or "") + (compile_result.stderr or "")
            log_event(
                "test-criterion",
                "retry",
                error=f"compile failed (attempt {attempt}, {limit_desc})",
                criterion=criterion,
                ticket=ticket_id,
            )
            if not policy.should_continue(attempt, failure_kind, None, None):
                break
            continue

        # -- Gate 2: run scoped tests ------------------------------------
        test_results = run_scoped_tests(
            test_names,
            commands,
            f"red check (attempt {attempt}, {limit_desc})",
            quiet=True,
        )

        # -- Gate 3: quality review --------------------------------------
        test_red_green = [r.returncode != 0 for r in test_results]
        concern = run_test_quality_review(
            criterion,
            plan_context,
            file_paths,
            test_names,
            existing_test_refs or [],
            model,
            ticket_id=ticket_id,
            test_red_green=test_red_green,
            verification=verification,
        )
        if concern:
            any_red = any(test_red_green)
            failure_kind = "quality-red" if any_red else "quality-green"
            last_error = concern
            quality_concern = concern
            log_event(
                "review-test-quality",
                "flagged",
                error=concern,
                criterion=criterion,
                ticket=ticket_id,
            )
            render.print_line()
            render.print_line(
                f"-- Test-quality review flagged (attempt {attempt}, {limit_desc}, "
                f"test is {'red' if any_red else 'green'}):"
            )
            render.print_line(concern)
            if not policy.should_continue(attempt, failure_kind, None, None):
                break
            continue

        # -- All gates passed --------------------------------------------
        quality_concern = None
        return file_paths, test_names, test_results, compile_result, quality_concern

    # -- Budget exhausted -----------------------------------------------
    if compile_result is not None and compile_result.returncode != 0:
        # Compile never succeeded - fatal. Hand the caller the last
        # failing compile_result to die on; no test_results to dispatch.
        return file_paths, test_names, [], compile_result, quality_concern

    # Compile succeeded but quality still flagged - advisory fallback.
    # test_results holds the last iteration's results for dispatch.
    if quality_concern:
        log.warning(
            "-- Test-quality review still flagged after %s (advisory, proceeding): %s",
            limit_desc,
            quality_concern,
        )
    return file_paths, test_names, test_results, compile_result, quality_concern


def build_test_quality_review_prompt(
    criterion: str,
    plan_context: str,
    test_files: list[str],
    test_names: list[str],
    existing_test_refs: list[str],
    test_red_green: list[bool] | None = None,
    verification: str = "test",
) -> str:
    instructions = load_prompt_body(TEST_QUALITY_REVIEW_PROMPT_FILE)
    # Correlate each written/modified test against the hint list by
    # exact "file::name" match - no separate bookkeeping needed, the
    # witness output and the existing_test_refs hints already share the
    # same "file::name" shape.
    sections = []
    for idx, (test_file, test_name) in enumerate(zip(test_files, test_names)):
        ref_match = next(
            (ref for ref in existing_test_refs if ref == f"{test_file}::{test_name}"),
            None,
        )
        if ref_match:
            diff_text = git_diff_for_file(test_file)
            modification_note = (
                f"This is a modification of an existing test ({ref_match}), "
                f"not a new one - apply Step 3. Diff of {test_file} since "
                f"before this change:\n\n```diff\n{diff_text}\n```"
                if diff_text.strip()
                else f"This is a modification of an existing test ({ref_match}), "
                f"not a new one, but no diff is available (the file wasn't "
                f"tracked before this change) - skip Step 3's diff-based "
                f"check for this one and say so."
            )
        else:
            modification_note = "This is a newly-written test."
        result_note = ""
        if test_red_green is not None and idx < len(test_red_green):
            is_red = test_red_green[idx]
            if is_red:
                result_note = (
                    "\n  Actual result: RED (failed when run against current code)"
                )
            elif verification == "test-refactor":
                result_note = (
                    "\n  Actual result: GREEN (passed when run against current "
                    "code - this is expected for a test-refactoring criterion; "
                    "the test should pass after the rewrite. Verify the rewrite "
                    "preserved all original assertions and changed only the "
                    "structural elements the criterion describes.)"
                )
            else:
                result_note = (
                    "\n  Actual result: GREEN (passed when run against current code "
                    "- this test is not detecting any gap in the current implementation)"
                )
        sections.append(
            f"- {test_file} :: {test_name}\n  {modification_note}{result_note}"
        )
    tests_block = "\n".join(sections)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"Acceptance criterion this test is for:\n\n{criterion}\n\n"
        f"The test(s) to review:\n{tests_block}\n\n{_HOST_PLATFORM_NOTE}"
    )


def run_test_quality_review(
    criterion: str,
    plan_context: str,
    test_files: list[str],
    test_names: list[str],
    existing_test_refs: list[str],
    model: str,
    ticket_id: str | None = None,
    test_red_green: list[bool] | None = None,
    verification: str = "test",
) -> str | None:
    """
    Reviews the test(s) Tester just wrote or modified for this criterion
    (almost always one; see test-criterion.prompt.md's Step 3 for when
    it's more), returning a flagged-concern string, or None if the
    reviewer found no concerns across any of them. Unlike every other
    AI-calling step in this module, a failure here (AIError, an
    unparseable verdict) is NEVER fatal - this whole function is a side
    channel that must not take the pipeline down with it, so any failure
    degrades to a single logged warning and a None return (treated the
    same as "no concerns") rather than die_with_log. The caller
    (next_step.py's do_write_test, via
    run_test_for_criterion_with_full_retry) feeds a non-None result back
    to the Tester for a bounded amendment attempt; a None return means
    "quality gate passed, proceed," and a failed AI call degrading to
    None means the loop proceeds without a quality check for that
    attempt - better to proceed than to die on an infrastructure
    failure in a side channel.

    test_red_green (parallel to test_names; True = red, False = green)
    gives the reviewer each test's actual run result as grounded
    evidence rather than speculation; omitted (None) for backward compat
    with any caller that didn't run the tests first.
    """
    try:
        result = run_ai_step_with_retry(
            lambda: run_with_tools(
                build_test_quality_review_prompt(
                    criterion,
                    plan_context,
                    test_files,
                    test_names,
                    existing_test_refs,
                    test_red_green=test_red_green,
                    verification=verification,
                ),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(allow_write=False),
                "review-test-quality",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "review-test-quality",
            criterion=criterion,
            ticket=ticket_id,
        )
    except (AIError, tools.PipelineAbort) as e:
        log.warning(
            "-- Test-quality review failed to run (advisory, continuing): %s", e
        )
        return None
    render_step_output(result.text)
    verdict = find_verdict(result.text, ["FLAGGED", "NO CONCERNS"])
    if verdict != "FLAGGED":
        if verdict is None:
            log.warning(
                "-- Test-quality review's output had no recognizable verdict "
                "(advisory, continuing). See output above."
            )
        return None
    return result.text


def build_recheck_criterion_prompt(criterion: str, plan_context: str) -> str:
    instructions = load_prompt_body(RECHECK_CRITERION_PROMPT_FILE)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the relevant Implementation Plan context for this "
        f"criterion, extracted from the gap plan - already complete and "
        f"current, no need to read_file it again:\n\n{plan_context}\n\n"
        f"The single acceptance criterion to recheck (its visible text "
        f"before any trailing \u003c!-- comment is what it requires; the comment's "
        f"why:/verify:/existing_test: tags are stale hints from an earlier "
        f"narrow pass, not ground truth about the current state):\n\n"
        f"{criterion}\n\n"
        f"Use read_file/list_dir/search_files to verify whether this "
        f"criterion is satisfied by the codebase in its current state. "
        f"Your final line must be exactly one of: SATISFIED, NOT "
        f"SATISFIED, or UNKNOWN."
    )


def _find_recheck_verdict(text: str) -> str | None:
    """
    Parse a Rechecker response into exactly one of "SATISFIED",
    "NOT SATISFIED", "UNKNOWN", or None (unparseable). Prefers a verdict
    on its own final non-empty line (optionally after a "Verdict:" label
    and/or markdown emphasis), so reasoning prose that happens to
    mention one of the tokens earlier can't be misread as the verdict -
    the prompt instructs the model to put the verdict last, on its own.
    Falls back to scanning every line the same way, then to a
    priority-ordered whole-text search (NOT SATISFIED before SATISFIED,
    since the latter is a substring of the former). Returns None only
    when none of those find anything.
    """
    candidates = ("NOT SATISFIED", "SATISFIED", "UNKNOWN")

    def _clean(line: str) -> str:
        s = line.strip().lstrip("*#-> ").strip().strip("*").strip()
        s = (
            re.sub(r"^(?:verdict\s*[:\-]\s*)", "", s, flags=re.IGNORECASE)
            .strip()
            .strip("*")
            .strip()
        )
        return s

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and _clean(lines[-1]) in candidates:
        return _clean(lines[-1])
    for ln in lines:
        if _clean(ln) in candidates:
            return _clean(ln)
    return find_verdict(text, ["NOT SATISFIED", "SATISFIED", "UNKNOWN"])


def recheck_single_criterion(
    criterion: str,
    plan_context: str,
    model: str,
    ticket_id: str | None = None,
) -> str:
    """
    Focused, single-criterion re-narrow: runs the Rechecker AI step on
    exactly one criterion against the current codebase. Returns
    "SATISFIED", "NOT SATISFIED", or "UNKNOWN". Used by next_step.py's
    _handle_no_test_written as the second-opinion fallback after the
    mechanical check (check_test_refactor_satisfied) is inconclusive
    and the Tester wrote nothing - the case a text search can't verify
    but an AI reading the code can (a behavioral criterion incidentally
    satisfied by a sibling's implementation).

    Like run_test_quality_review, this is a side channel: a failure
    here (AIError, tools.PipelineAbort, or an unparseable verdict) is
    NEVER fatal. Any failure degrades to "UNKNOWN" (treated as
    "inconclusive -> pause for human") rather than die_with_log, so an
    infrastructure failure in this second-opinion path can't take the
    pipeline down or auto-pop a frame the check couldn't actually
    confirm.
    """
    try:
        result = run_ai_step_with_retry(
            lambda: run_with_tools(
                build_recheck_criterion_prompt(criterion, plan_context),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(allow_write=False),
                "recheck-criterion",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "recheck-criterion",
            criterion=criterion,
            ticket=ticket_id,
        )
    except (AIError, tools.PipelineAbort) as e:
        log.warning("-- Recheck failed to run (degrading to UNKNOWN): %s", e)
        return "UNKNOWN"
    render_step_output(result.text)
    verdict = _find_recheck_verdict(result.text)
    if verdict is None:
        log.warning(
            "-- Recheck output had no recognizable verdict (degrading to "
            "UNKNOWN). See output above."
        )
        return "UNKNOWN"
    return verdict


def build_review_prompt(changed_files: list[str], plan_text: str) -> str:
    instructions = load_prompt_body(REVIEW_PROMPT_FILE)
    file_list = "\n".join(f"- {p}" for p in changed_files)
    return (
        f"{instructions}\n\n---\n\n"
        f"Here is the TDD plan ({PLAN_FILE}) - already complete and "
        f"current, no need to read_file it again:\n\n{plan_text}\n\n"
        f"The following files were changed or created:\n{file_list}\n\n"
        f"Review these per the steps and rules in your instructions."
    )


def run_review_gate(
    changed_files: list[str], plan_text: str, model: str, ticket_id: str | None = None
) -> tuple[str, str]:
    """
    Returns (verdict, raw_review_text) for any recognized verdict,
    including APPROVED - next_step.py's TICKET_VALIDATE phase branches
    on the verdict itself (APPROVED -> done; CHANGES REQUESTED -> pass
    raw_review_text straight to extract_review_findings), rather than
    this function dying on a non-APPROVED result the way it used to when
    "review failed" and "review passed" were this function's only two
    outcomes. die_with_log still fires on a genuine tool/AI failure or an
    unparseable verdict - those aren't a normal CHANGES REQUESTED result
    for the caller to act on.

    Routed through run_ai_step_with_retry like every other AI-calling
    step here (this used to call run_with_tools directly, with no retry
    on a transient AIError and no token/cost accounting) - both for
    consistency and so a review's cost shows up in .pipeline-log.jsonl
    same as every other block's.
    """
    try:
        review_result = run_ai_step_with_retry(
            lambda: run_with_tools(
                build_review_prompt(changed_files, plan_text),
                tools.READ_ONLY_TOOLS,
                tools.make_executor(allow_write=False),
                "review",
                model=model,
                summarize_call=tools.summarize_tool_call,
            ),
            "review",
            ticket=ticket_id,
        )
    except (AIError, tools.PipelineAbort) as e:
        die_with_log("review", str(e), ticket=ticket_id)
    render_step_output(review_result.text)
    verdict = find_verdict(review_result.text, ["CHANGES REQUESTED", "APPROVED"])
    if verdict is None:
        die_with_log(
            "review",
            "Reviewer's output did not contain a recognizable verdict (see output above).",
            ticket=ticket_id,
        )
    return verdict, review_result.text
