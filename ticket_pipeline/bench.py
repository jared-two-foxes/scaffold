#!/usr/bin/env python3
"""
bench - run a pipeline_lib block (plan, narrow) N times per model,
concurrently, each trial isolated in its own git worktree, and report a
pass-rate/cost/duration table per model.

Why worktrees: pipeline_lib writes fixed-name state files (.ticket.md,
.tdd-plan.md, .gap-plan.md) into the cwd, and the model's own file-read
tool calls resolve paths relative to cwd too. Two trials sharing a cwd
would clobber each other's state and could even read each other's
half-written files mid-run. A git worktree gives each trial its own
real checkout of the target repo - concurrent trials are then exactly
as safe as two people working in two separate clones, which is also
the same mechanism you'd want for running unrelated tickets in parallel
in real (non-benchmark) use.

Why fixtures instead of chaining live steps: testing "narrow" in
isolation should measure narrow's own competence, not whatever plan
happened to come before it. Each trial gets a fixed ticket fixture and
(for narrow) a fixed plan fixture - either the known-good plan or the
known-bad (file-split) plan - so you get two independent answers: does
this model narrow correctly from a clean plan, and does it catch/fix a
bad one.

Usage (not a console-script command - deliberately excluded from the
standard install as situational; run via `python -m`):
    python -m ticket_pipeline.bench --block plan --models deepseek-v4-pro,glm-5 --trials 3
    python -m ticket_pipeline.bench --block narrow --models glm-5,gpt-5.1 --trials 3 --plan-fixture both

Each trial calls bench_block.py as a subprocess with cwd set to its
worktree - see that file for the actual block invocation + grading.
"""

import argparse
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# `git worktree add`/`remove` race against each other when run
# concurrently on the same repo (git's own .git/worktrees bookkeeping
# isn't safe for parallel invocations) - serialize just these two calls.
# The worktrees themselves still run fully in parallel once created.
_WORKTREE_LOCK = threading.Lock()

SCRIPT_DIR = Path(__file__).resolve().parent
# fixtures/ is a dev-only benchmark asset directory, kept at the project
# root (scaffold/) rather than inside the installed
# ticket_pipeline package - one level up from this module.
PROJECT_DIR = SCRIPT_DIR.parent
DEFAULT_REPO = Path.home() / "code" / "own" / "VirtualAssistant"

# test-criterion trials actually invoke cargo (compile + scoped run) -
# each worktree is a fresh checkout with no target/ directory, so a
# fully cold CARGO_TARGET_DIR per trial would mean every trial rebuilds
# the entire dependency tree from scratch. A *single* CARGO_TARGET_DIR
# shared across concurrent trials looked tempting (cargo's lock file
# supposedly serializes access) but empirically corrupted results - a
# clean 3/3 pass rate for one model run alone dropped to 0/15 once 4
# trials ran concurrently against the same target dir, almost certainly
# from incremental-compilation cache entries leaking between worktrees
# building the same crate with different (model-written) test source.
# Fix: a fixed pool of N "lanes", each with its own target dir - a lane
# is only ever used by one trial at a time (checked out from the queue,
# returned when done), so compiles stay warm within a lane but two
# trials never touch the same target dir simultaneously.
_CARGO_LANES_BASE = Path(tempfile.gettempdir()) / "bench-cargo-target"
_cargo_lane_pool: "queue.Queue[Path]" = queue.Queue()
_cargo_lane_pool_size = 0


def _ensure_cargo_lane_pool(n: int) -> None:
    global _cargo_lane_pool_size
    if _cargo_lane_pool_size >= n:
        return
    for i in range(_cargo_lane_pool_size, n):
        _cargo_lane_pool.put(_CARGO_LANES_BASE / f"lane-{i}")
    _cargo_lane_pool_size = n


# Blocks that actually invoke cargo (compile + scoped run), as opposed
# to plan/narrow which are pure model calls - these need a cargo lane,
# a private database.db copy, and the concurrency safety cap below.
# implement-criterion used to be in this set too, before the
# criteria-stack rewrite retired AI-driven per-criterion implementation
# (see bench_block.py's module docstring) - next_step.py always pauses
# for a human to implement, so there's no implement block left to bench.
CARGO_BLOCKS = {"test-criterion"}

# plan/narrow trials are pure model calls (tens of seconds to a few
# minutes); the cargo blocks additionally compile and run cargo, which
# is much slower, especially before a lane's target dir is warm.
TRIAL_TIMEOUT_S = {
    "plan": 900,
    "narrow": 900,
    "plan-narrow": 900,
    "test-criterion": 2400,
}

# Blocks that need a fixed gap-plan fixture instead of plan/narrow's
# own fixture handling - test-criterion has no "good/bad upstream plan"
# axis, just one fixed gap plan plus the one criterion under test.
DEFAULT_CRITERIA = {
    "sa452": "- [ ] `Debug` output redacts the secret values",
    "sa500": "- [ ] `WEBHOOK_RETRY_RATE_LIMIT` env var parsed into `RateLimitConfig.webhook_retry_rate_limit`",
}


@dataclass
class Job:
    model: str
    block: str
    ticket_name: str
    ticket_file: Path
    plan_fixture: str | None  # "good" / "bad" / None (plan block has no upstream plan)
    plan_file: Path | None
    trial_index: int
    criterion: str | None = None


@dataclass
class TrialResult:
    job: Job
    success: bool = False
    reason: str = ""
    duration_s: float = 0.0
    cost_usd: float = 0.0
    tokens_total: int = 0
    worktree_setup_s: float = 0.0
    crash: str | None = None


def resolve_fixture_base_ref(fixtures_dir: Path) -> str:
    """
    Fixtures (ticket text, plans, captured test/implement outputs) encode
    assumptions about the target repo's exact state at the time they were
    captured - which fields exist on a struct, which files are where,
    even line numbers in error messages a grader might match against. The
    target repo (DEFAULT_REPO) is someone's live, moving codebase, not a
    frozen fixture store - if every run defaulted to 'HEAD', the same
    fixture would silently drift out of sync with the code it was written
    against as that repo's main branch moves, and a bench failure later
    could mean "the model got it wrong" or "the fixture no longer matches
    reality" with no way to tell which from the numbers alone.

    fixture.json (next to the fixture's ticket.md etc.) pins the exact
    commit a fixture was authored/validated against, so re-running it
    next month reproduces the same comparison instead of a moving one.
    Falls back to 'HEAD' with a warning for fixtures that haven't been
    pinned yet (or never will be, e.g. a throwaway one-off fixture) -
    pinning is strongly recommended, not enforced.
    """
    meta_path = fixtures_dir / "fixture.json"
    if not meta_path.is_file():
        print(
            f"-- warning: no {meta_path} pin found - using 'HEAD', which moves as the "
            f"target repo's main branch moves. Results from this run may not be "
            f"reproducible later. See resolve_fixture_base_ref's docstring.",
            flush=True,
        )
        return "HEAD"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    base_ref = meta.get("base_ref")
    if not base_ref:
        print(
            f"-- warning: {meta_path} exists but has no base_ref key - using 'HEAD'.",
            flush=True,
        )
        return "HEAD"
    print(f"-- Using fixture-pinned base_ref {base_ref} (from {meta_path})", flush=True)
    return base_ref


def create_worktree(repo: Path, base_ref: str) -> Path:
    wt_path = Path(tempfile.gettempdir()) / "bench-worktrees" / uuid.uuid4().hex[:12]
    with _WORKTREE_LOCK:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                str(wt_path),
                base_ref,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return wt_path


def remove_worktree(repo: Path, wt_path: Path) -> None:
    with _WORKTREE_LOCK:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)],
            check=False,
            capture_output=True,
            text=True,
        )


def parse_bench_result(stdout: str) -> dict:
    marker = "===BENCH_RESULT==="
    if marker not in stdout:
        raise ValueError(f"no result marker in subprocess output:\n{stdout[-2000:]}")
    json_line = stdout.rsplit(marker, 1)[1].strip().splitlines()[0]
    return json.loads(json_line)


def run_trial(job: Job, repo: Path, base_ref: str) -> TrialResult:
    result = TrialResult(job=job)
    setup_start = time.monotonic()
    try:
        wt_path = create_worktree(repo, base_ref)
    except subprocess.CalledProcessError as e:
        result.crash = f"worktree setup failed: {e.stderr}"
        return result
    result.worktree_setup_s = round(time.monotonic() - setup_start, 2)

    timeout_s = TRIAL_TIMEOUT_S.get(job.block, 900)
    try:
        cmd = [
            sys.executable,
            "-m",
            "ticket_pipeline.bench_block",
            "--block",
            job.block,
            "--ticket-name",
            job.ticket_name,
            "--ticket-file",
            str(job.ticket_file),
            "--model",
            job.model,
        ]
        if job.plan_file:
            cmd += ["--plan-file", str(job.plan_file)]
        if job.criterion:
            cmd += ["--criterion", job.criterion]

        env = os.environ.copy()
        cargo_lane = None
        if job.block in CARGO_BLOCKS:
            cargo_lane = _cargo_lane_pool.get()
            env["CARGO_TARGET_DIR"] = str(cargo_lane)
            # sqlx's compile-time query! macros need DATABASE_URL to
            # introspect schema - the repo's .env + database.db that
            # normally provide it are both gitignored, so a fresh
            # worktree has neither. Pointing every trial at the *same*
            # database.db file looked safe (read-only schema
            # introspection) but wasn't: concurrent cargo processes
            # opening the same SQLite file caused intermittent lock
            # contention that surfaced as bogus "type annotations
            # needed" compile errors - the same failure mode sqlx
            # produces when it can't query the DB at all. Copying the DB
            # into each worktree gives every trial its own file, so
            # there's nothing left to contend over.
            wt_db_path = wt_path / "database.db"
            shutil.copyfile(repo / "database.db", wt_db_path)
            env["DATABASE_URL"] = f"sqlite:{wt_db_path.as_posix()}"

        proc = subprocess.run(
            cmd,
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if proc.returncode != 0 and "===BENCH_RESULT===" not in proc.stdout:
            result.crash = (
                f"subprocess exit {proc.returncode}, no result line. "
                f"stderr tail: {proc.stderr[-1500:]}"
            )
            return result
        parsed = parse_bench_result(proc.stdout)
        result.success = parsed["success"]
        result.reason = parsed["reason"]
        result.duration_s = parsed["duration_s"]
        result.cost_usd = parsed["cost_usd"]
        result.tokens_total = parsed["tokens_total"]
    except subprocess.TimeoutExpired:
        result.crash = f"trial timed out after {timeout_s}s"
    except Exception as e:  # noqa: BLE001
        result.crash = f"{type(e).__name__}: {e}"
    finally:
        remove_worktree(repo, wt_path)
        if cargo_lane is not None:
            _cargo_lane_pool.put(cargo_lane)

    return result


def build_jobs(args) -> list[Job]:
    fixtures_dir = Path(args.fixtures_dir)
    ticket_file = fixtures_dir / "ticket.md"
    if not ticket_file.is_file():
        raise SystemExit(f"missing ticket fixture: {ticket_file}")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    jobs: list[Job] = []

    if args.block == "plan":
        for model in models:
            for trial in range(args.trials):
                jobs.append(
                    Job(
                        model=model,
                        block="plan",
                        ticket_name=args.ticket_name,
                        ticket_file=ticket_file,
                        plan_fixture=None,
                        plan_file=None,
                        trial_index=trial,
                    )
                )
    elif args.block == "plan-narrow":
        for model in models:
            for trial in range(args.trials):
                jobs.append(
                    Job(
                        model=model,
                        block="plan-narrow",
                        ticket_name=args.ticket_name,
                        ticket_file=ticket_file,
                        plan_fixture=None,
                        plan_file=None,
                        trial_index=trial,
                    )
                )
    elif args.block == "narrow":
        variants = (
            ["good", "bad"] if args.plan_fixture == "both" else [args.plan_fixture]
        )
        for variant in variants:
            plan_file = fixtures_dir / f"plan-{variant}.md"
            if not plan_file.is_file():
                raise SystemExit(f"missing plan fixture: {plan_file}")
            for model in models:
                for trial in range(args.trials):
                    jobs.append(
                        Job(
                            model=model,
                            block="narrow",
                            ticket_name=args.ticket_name,
                            ticket_file=ticket_file,
                            plan_fixture=variant,
                            plan_file=plan_file,
                            trial_index=trial,
                        )
                    )
    else:  # test-criterion
        gap_plan_file = fixtures_dir / "gapplan-good.md"
        if not gap_plan_file.is_file():
            raise SystemExit(f"missing gap-plan fixture: {gap_plan_file}")
        criterion = args.criterion or DEFAULT_CRITERIA.get(args.ticket_name)
        if not criterion:
            raise SystemExit(
                f"no --criterion given and no default for ticket '{args.ticket_name}'"
            )
        for model in models:
            for trial in range(args.trials):
                jobs.append(
                    Job(
                        model=model,
                        block="test-criterion",
                        ticket_name=args.ticket_name,
                        ticket_file=ticket_file,
                        plan_fixture=None,
                        plan_file=gap_plan_file,
                        trial_index=trial,
                        criterion=criterion,
                    )
                )
    return jobs


def print_summary(results: list[TrialResult]) -> None:
    groups: dict[tuple[str, str | None], list[TrialResult]] = {}
    for r in results:
        key = (r.job.model, r.job.plan_fixture)
        groups.setdefault(key, []).append(r)

    header = f"{'model':<20} {'fixture':<8} {'trials':<7} {'pass':<6} {'avg_s':<8} {'avg_$':<8} {'total_$':<8}"
    print(header)
    print("-" * len(header))
    for (model, fixture), group in sorted(groups.items()):
        n = len(group)
        ok = [r for r in group if r.crash is None]
        passed = sum(1 for r in ok if r.success)
        crashed = n - len(ok)
        avg_s = statistics.mean(r.duration_s for r in ok) if ok else 0.0
        avg_cost = statistics.mean(r.cost_usd for r in ok) if ok else 0.0
        total_cost = sum(r.cost_usd for r in ok)
        fixture_label = fixture or "-"
        crash_note = f" ({crashed} crashed)" if crashed else ""
        print(
            f"{model:<20} {fixture_label:<8} {n:<7} {passed}/{len(ok)}{crash_note:<6} "
            f"{avg_s:<8.1f} {avg_cost:<8.4f} {total_cost:<8.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--block",
        required=True,
        choices=["plan", "narrow", "plan-narrow", "test-criterion"],
    )
    parser.add_argument("--models", required=True, help="Comma-separated model IDs")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument(
        "--base-ref",
        default=None,
        help="Commit/ref to create each trial's worktree from. Default: read "
        "fixtures/<ticket-name>/fixture.json's pinned base_ref, so results stay "
        "reproducible as the target repo's main branch moves; falls back to 'HEAD' "
        "(today's moving target) with a warning if the fixture has no pin yet. Pass "
        "this explicitly to intentionally re-validate a fixture against a newer commit.",
    )
    parser.add_argument("--ticket-name", default="sa452")
    parser.add_argument(
        "--fixtures-dir",
        default=None,
        help="Default: fixtures/<ticket-name>/ - only pass this to override.",
    )
    parser.add_argument(
        "--plan-fixture",
        default="both",
        choices=["good", "bad", "both"],
        help="Only used for --block narrow",
    )
    parser.add_argument(
        "--criterion",
        default=None,
        help="Only used for --block test-criterion "
        "(default: DEFAULT_CRITERIA[ticket-name] or the fixture's own criterion)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Path to write results.jsonl (default: bench-<block>-<timestamp>.jsonl)",
    )
    parser.add_argument(
        "--allow-concurrent-cargo",
        action="store_true",
        help="Allow --concurrency > 1 for the test-criterion cargo block. "
        "Off by default: running multiple full `cargo test --no-run`/`cargo build` workspace "
        "compiles at once exhausted this machine's pagefile and produced corrupted builds "
        "(linker STATUS_STACK_BUFFER_OVERRUN, bogus 'crate required in rlib format' errors) - "
        "not a logic bug, a real resource ceiling. Only pass this if you've confirmed your "
        "machine has the RAM/pagefile for it.",
    )
    args = parser.parse_args()
    if args.fixtures_dir is None:
        args.fixtures_dir = str(PROJECT_DIR / "fixtures" / args.ticket_name)

    if args.base_ref is None:
        args.base_ref = resolve_fixture_base_ref(Path(args.fixtures_dir))

    if (
        args.block in CARGO_BLOCKS
        and args.concurrency > 1
        and not args.allow_concurrent_cargo
    ):
        print(
            f"-- --block {args.block} forces --concurrency 1 by default (was {args.concurrency}): "
            f"concurrent cargo compiles corrupted builds on this machine (pagefile exhaustion). "
            f"Pass --allow-concurrent-cargo to override.",
            flush=True,
        )
        args.concurrency = 1

    out_path = (
        Path(args.out)
        if args.out
        else Path(f"bench-{args.block}-{int(time.time())}.jsonl")
    )

    jobs = build_jobs(args)
    if args.block in CARGO_BLOCKS:
        _ensure_cargo_lane_pool(args.concurrency)
    print(
        f"-- Running {len(jobs)} trials across {args.concurrency} workers ...",
        flush=True,
    )

    results: list[TrialResult] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool, out_path.open(
        "w", encoding="utf-8"
    ) as out_f:
        futures = {
            pool.submit(run_trial, job, args.repo, args.base_ref): job for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            r = future.result()
            results.append(r)
            status = "CRASH" if r.crash else ("PASS" if r.success else "FAIL")
            print(
                f"   [{status}] model={job.model} block={job.block} "
                f"fixture={job.plan_fixture or '-'} trial={job.trial_index} "
                f"{r.duration_s:.1f}s ${r.cost_usd:.4f} - {r.crash or r.reason}",
                flush=True,
            )
            out_f.write(
                json.dumps(
                    {
                        "model": job.model,
                        "block": job.block,
                        "fixture": job.plan_fixture,
                        "trial": job.trial_index,
                        "success": r.success,
                        "reason": r.reason,
                        "duration_s": r.duration_s,
                        "cost_usd": r.cost_usd,
                        "tokens_total": r.tokens_total,
                        "crash": r.crash,
                    }
                )
                + "\n"
            )
            out_f.flush()

    print(f"\n-- Results written to {out_path}\n", flush=True)
    print_summary(results)


if __name__ == "__main__":
    main()
