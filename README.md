# Scaffold

Scaffold is a deterministic ticket and task-management CLI for:

- Accessing Linear tickets
- Maintaining the criteria stack
- Performing stack operations (push, pop, list, clear)
- Persisting dependent feature tasks and supplied verification records

## Installation

```bash
pip install -e .
```

## Usage

```bash
scaffold --help
scaffold fetch-ticket SA-123
scaffold status
scaffold stack list
scaffold stack push --ticket SA-123 --criterion "- [ ] Add API field"
scaffold stack pop
scaffold stack clear
```

## Recursive issue design and implementation

`tasks init` always creates the root issue using the feature ID from the agreed
ticket. Supply `baseline` (full original Git SHA) and `branch` (feature branch),
alongside the objective, original acceptance criteria and immutable design reference.
There is no workflow selector or alternate task lifecycle. All task state and
responses use schema version 2; older files are rejected without modification.
Definitions may include `origin: {kind: "ticket"|"design"|"review"|"discovery",
reference: "<issue or retained evidence>"}`.

Design proceeds downward. `tasks design-next --feature F` selects an undesigned
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

`tasks summary --feature F` returns identity, phase, status counts, revision and a
SHA-256 state digest. `tasks issue --feature F --task ID` returns the issue's current
contract/design, ancestors, direct and inherited prerequisites, immediate children
and completion references. It excludes full histories, sibling bodies and previous
design transcripts. `tasks select` and `tasks design-select` use exactly the same
selection rules as next/design-next, but return that scoped context or null plus
summary. All four are read-only and preserve state bytes; null never means verified
completion. Scope size follows the relevant contracts, not an arbitrary text cutoff.
Full list is retained for explicit diagnostics and native integrity checks.

Pi persists its issue handoffs in local Git metadata, binding these query results
to current graph/Git/receipt state. Scaffold neither authenticates those receipts nor
stores a second mutable execution state. Stale handoffs must be refreshed by Pi.
