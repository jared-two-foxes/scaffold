# Scaffold

Scaffold is a deterministic task-management CLI for:

- Persisting recursive, dependency-aware feature tasks and supplied verification records

## Installation

```bash
pip install -e .
```

## Usage

```bash
scaffold --help
scaffold summary --feature SA-123
scaffold issue --feature SA-123 --task SA-123
scaffold design-select --feature SA-123
scaffold select --feature SA-123
```

The task commands above inspect an already-initialized feature. Initialize a new
feature using `scaffold init --feature SA-123 --input -` with the complete
JSON contract described below. The retired `stack` and `status` CLI commands
are not supported. Historic `.scaffold/.criteria-stack.json` and
`.scaffold/.criteria-stack.lock` files are left untouched, not imported or
deleted; current task operations use `.scaffold/tasks.json` and
`.scaffold/.tasks.lock`.

## Administrative feature reset

`scaffold clean` resets persisted feature/task associations in the current
repository. It does **not** undo implementation, Git commits/branches/worktrees,
or external evidence. The legacy criteria-stack artifacts and active
`.scaffold/.tasks.lock` are preserved.

```sh
scaffold clean --feature SA-123 --dry-run  # preview selected feature and task counts
scaffold clean --feature SA-123            # interactive: type "yes" to confirm
scaffold clean --feature SA-123 --yes      # explicit unattended authorization
scaffold clean --all --dry-run             # preview every feature
scaffold clean --all                       # interactive full reset
scaffold clean --all --yes                 # explicit unattended full reset
```

Exactly one target (`--feature ID` or `--all`) is required. Dry-run never
changes `tasks.json`. A destructive reset without `--yes` requires a terminal
and the exact response `yes` (piped responses are rejected). Missing features
and empty stores are safe no-ops. If task state changes after the preview,
the command aborts and requires a new preview/confirmation rather than deleting
unanticipated state. Reset does not authorize continuing an existing Pi workflow.
Only `.scaffold/tasks.json` is rewritten when records are removed:
`--feature` removes the selected feature entry, and `--all` writes an
empty schema-v2 feature store. The `.scaffold/.tasks.lock` inode is retained;
older `.criteria-stack.*` files, Git commits, branches, worktrees, source
files and external Pi/Conductor receipts are not touched.

Each new `scaffold init` records a unique `workflow.instance_id`. Cleaning and
reinitializing the **same** ticket with an otherwise identical design, branch
and baseline creates a new identity: old Pi authorization and retained execution
receipts cannot authorize the new feature. Re-run feature preparation and obtain
a fresh, explicit feature-direction approval, then start new task attempts.
Do **not** use clean to discard failed attempts in an active feature or as an
automatic recovery tool. Existing schema-v2 records created before this field
was introduced remain readable; no implicit backfill or migration is performed.

## Recursive issue design and implementation

`scaffold init` always creates the root issue using the feature ID from the agreed
ticket. Supply `baseline` (full original Git SHA) and `branch` (feature branch),
alongside the objective, original acceptance criteria and immutable design reference.
There is no workflow selector or alternate task lifecycle. All task state and
responses use schema version 2; older files are rejected without modification.
Definitions may include `origin: {kind: "ticket"|"design"|"review"|"discovery",
reference: "<issue or retained evidence>"}`.

Design proceeds downward. `scaffold design-next --feature F` selects an undesigned
issue below an approved parent/prerequisite contract. Use `decompose` for immediate
children, then `design-record --feature F --task ID --input -` with
`{summary, verification, approval, disposition: "leaf"|"decomposed"}`. The record
binds its children and is content addressed. Callers must obtain actual user
confirmation: Scaffold validates structure, not consent. `design-finish` refuses
unapproved nodes and transitions the entire graph to implementation. No `start`
or `complete` is accepted in the design phase.

Implementation uses `next`, `start`, evidence-bound `complete`, and native Pi Git
checkpoints. `checkpoint-record --task ID --input -` records `{commit, reference}`
after Pi authenticates the actual checkpoint. It is bookkeeping, not Git or proof
of verification. Selection/start pauses until every completed issue has a recorded
checkpoint. Parents become eligible after children and require their own evidence
and commit, including verification-only parents. Final feature validation remains
mandatory. Scaffold does not execute source, run Git, invoke review or retry work.

Discoveries return the affected scope to design. Native Pi `suspend_issue` first
parks active source/staging/untracked files and authenticates its latest attempt;
it then calls `design-reopen` with `{reason, reference: "<suspension receipt>"}`.
An approved issue can also be reopened by ID during Design; its previous design
and affected unfinished designs are archived immediately. Failed, blocked or
retried issues with execution history still need suspension evidence before
reopening or decomposition. Without unfinished implementation a caller can reopen with just `{reason}` and
optional `--task`. Reopening preserves old designs and invalidates unfinished
descendant designs. Add and approve new issues, finish design, then implement new
leaves. Native Pi `resume_issue` authenticates prerequisite checkpoints and restores
saved source, then calls `issue-resume` with `{reference, handoff}`. The original
issue resumes using its retained attempt chain; design/retry/decomposition never
reset Conductor budgets. Completed nodes remain immutable; final-review corrections
are added as new dependent issues. All mutations invalidate current final-validation
applicability; historical results remain retained.

## Validation

Run `pip install -e . pytest` and `python -m pytest`. Scaffold owns deterministic
state transitions; Pi authenticates external Conductor receipts and Git checkpoints.

### Scoped queries for small model contexts

`scaffold summary --feature F` returns identity, phase, status counts, revision and a
SHA-256 state digest. `scaffold issue --feature F --task ID` returns the issue's current
contract/design, ancestors, direct and inherited prerequisites, immediate children
and completion references. It excludes full histories, sibling bodies and previous
design transcripts. `scaffold select` and `scaffold design-select` use exactly the same
selection rules as next/design-next, but return that scoped context or null plus
summary. All four are read-only and preserve state bytes; null never means verified
completion. Scope size follows the relevant contracts, not an arbitrary text cutoff.
Full list is retained for explicit diagnostics and native integrity checks.

Pi persists its issue handoffs in local Git metadata, binding these query results
to current graph/Git/receipt state. Scaffold neither authenticates those receipts nor
stores a second mutable execution state. Stale handoffs must be refreshed by Pi.

### Versioned delivery journal

The Pi boundary uses `scaffold delivery-record --feature F --task T --input -` for
internal compare-and-swap bookkeeping. Supply `expected_revision` and one event:
initialized, context, begun, finished, handoff or paused. Records retain the immutable
original feature contract, versioned context, every invocation sequence, current
receipt head and in-flight reservation. Context updates are additive; originals and
obligations cannot disappear. Material amendments retain the explicit approval
reference and replacement obligation. Bookkeeping never completes an issue or proves
an independent review. Native Pi code authenticates receipts and its Git audit anchor.

Existing task schema version 2 files remain readable. Journal initialization can
import a legacy receipt head and consumed sequence, including five or more attempts;
initialization never replaces an existing journal. Stale revisions, duplicate in-flight
reservations and mismatched terminal associations fail under the existing file lock.
No finite issue-attempt allowance is imposed by this storage protocol.

On the first-ever `start`, Scaffold marks an issue as using the versioned delivery
policy. A legacy pending issue can acquire this marker only if its history has never
entered implementation. Retrying a previously started legacy issue does not mark it
pristine; Pi requires its retained latest receipt rather than assuming zero attempts.
