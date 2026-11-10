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
deleted; current task operations use the ledger under `.scaffold/features/` and
`.scaffold/.tasks.lock` (see Ledger storage).

## Ledger storage

Feature state is an append-only, hash-chained **ledger** per feature *instance*, stored
under `.scaffold/features/<feature-id>.<instance-id>/`:

```
ledger.jsonl         one JSON entry per line; each names the hash of the previous entry
artifacts/<sha256>   content-addressed blobs (receipts, approvals, suspensions) cited by entries
```

Each entry records the action, its validated payload and the exact state change (a patch)
plus the digest of the state it produces. State is the fold of the ledger and every read
verifies the whole chain, so an edited, reordered or removed-from-the-middle entry is
detected and blocks every operation without being repaired. The chain does **not** detect
removal of the newest whole entries, cannot stop a process with write access from appending a
forged entry, and cannot judge whether supplied evidence is true. Appends happen under the
`.scaffold/.tasks.lock` file lock; a failed append leaves the ledger as it was and a torn
final line is ignored. `.scaffold/` must stay ignored and untracked. Nothing is written to `.git`.

Schema version is 3. A legacy `.scaffold/tasks.json` (schema 2) is never read, migrated or
modified; re-initialize features instead. `scaffold init` creates a new instance directory
with a fresh `workflow.instance_id`, so an approval or receipt of an earlier instance can
never apply to it.

Evidence is referenced as `sha256:<64 hex>` naming an artifact in the same ledger. Store one
with `scaffold artifact-put --feature F --input -` (returns `{reference}`) and read it back
with `artifact-get`. Actions that cite a reference (`attempt-record`, `history-foreign`,
`history-rewrite`, `issue-resume`, `validation-record`, `failure-record`, a suspension
reference in `design-reopen`) reject one that is not stored.

Rules Scaffold enforces on the ledger:

- `direction-approve --input -` takes `{scope_digest, capabilities?}`. Scaffold computes the
  digest of the feature definition and its instance (returned as `direction_scope`) and rejects
  a mismatch. `start` is refused until the approval matches.
- `attempt-record --task T --input -` takes `{attempt, status, receipt, base, tree}` for the
  active task. Attempts are sequential and idempotent. `complete` must cite the `receipt` of a
  recorded `PASSED` attempt of that task.
- `checkpoint-record --task T --input -` takes `{commit, parent, tree, reference}`. `parent`
  must equal the ledger's recorded history `head`; checkpoints therefore form one chain from the
  original baseline.
- `history-foreign` records a user-approved range of commits that are **not** checkpoints
  (`{from, to, source, commits, approval}`), between tasks only. They are recorded, never
  verified, and remain in review scope.
- `history-rewrite` replaces the recorded history after a rebase
  (`{old_head, new_head, new_base, history, verification}`), keeping the previous history (and its
  base) in `epochs`. `original_baseline` never changes; `base` is where the *current* history starts
  (the fork point until a rewrite moves it onto the commit that was rebased onto).
- `failure-record --input -` (and optionally `--task T`) takes `{code, message, tool, owner}` plus
  optional `stage`, `cause`, `evidence` (a stored artifact reference) and `next` (up to five short
  steps). `owner` is `agent` (fix and retry), `user` (a decision is needed) or `bug` (the pipeline
  failed unexpectedly). It is a log entry: accepted in any phase for an open feature, it never
  changes the work. The full record is the ledger entry (every one is appended, even a repeat);
  `list` shows the last 20 as `failures` and the total as `failure_count`.
- `validation-start` requires the validated `head` to equal the ledger's recorded head, so the
  ledger accounts for every commit, and the validated `baseline` to equal the ledger's `base`.

Scaffold cannot see Git. Pi confirms every commit, tree and ancestry before appending.
`scaffold features` lists open feature instances (`id`, `instance_id`, `phase`, `validation_status`,
`tasks`) for callers that do not know feature ids.

## Closing a feature

`scaffold clean` **no longer deletes anything**. It appends a `feature-close` entry to the
ledger of each selected open feature instance. A closed instance accepts no more work and is
reported as unknown; `init` of the same feature id then starts a fresh instance, and the closed
ledger stays on disk for audit. It does not undo implementation, Git commits, branches or
worktrees, and it does not authorize continuing any Pi workflow.

```sh
scaffold clean --feature SA-123 --dry-run  # preview selected feature and task counts
scaffold clean --feature SA-123            # interactive: type "yes" to confirm
scaffold clean --feature SA-123 --yes      # explicit unattended authorization
scaffold clean --all --dry-run             # preview every open feature
scaffold clean --all                       # interactive: close every open feature
scaffold clean --all --yes                 # explicit unattended close
```

Exactly one target (`--feature ID` or `--all`) is required. Dry-run never changes anything.
Closing without `--yes` requires a terminal and the exact response `yes`. If a ledger changes
after the preview, the command aborts and requires a new preview. Each close is its own append,
so an interruption during `--all` leaves some instances closed; run it again to close the rest.
Older `.criteria-stack.*` files and `.scaffold/tasks.json` are not touched.

## Recursive issue design and implementation

`scaffold init` always creates the root issue using the feature ID from the agreed
ticket. Supply `baseline` (full original Git SHA) and `branch` (feature branch),
alongside the objective, original acceptance criteria and immutable design reference.
There is no workflow selector or alternate task lifecycle. All task state and
responses use schema version 3; legacy state is ignored without modification.
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
checkpoints. `checkpoint-record --task ID --input -` records
`{commit, parent, tree, reference}` after Pi authenticates the actual checkpoint. It is
bookkeeping that keeps the history chain consistent, not Git or proof of verification. Selection/start pauses until every completed issue has a recorded
checkpoint. Parents become eligible after children and require their own evidence
and commit, including verification-only parents. Final feature validation remains
mandatory. Scaffold does not execute source, run Git, invoke review or retry work.

Discoveries return the affected scope to design. Native Pi `suspend_issue` first
parks active source/staging/untracked files and authenticates its latest attempt;
it then calls `design-reopen` with `{reason, reference: "sha256:<stored suspension>"}`.
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

Journal initialization can import a receipt head and consumed sequence, including five or
more attempts; initialization never replaces an existing journal. Stale revisions, duplicate in-flight
reservations and mismatched terminal associations fail under the existing file lock.
No finite issue-attempt allowance is imposed by this storage protocol.

On the first-ever `start`, Scaffold marks an issue as using the versioned delivery
policy. A legacy pending issue can acquire this marker only if its history has never
entered implementation. Retrying a previously started legacy issue does not mark it
pristine; Pi requires its retained latest receipt rather than assuming zero attempts.
