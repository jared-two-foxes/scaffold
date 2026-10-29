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

## Persistent feature tasks (v1)

`scaffold tasks` adds deterministic task state alongside the existing criteria
stack. Pi owns design, decomposition, sequencing and recovery decisions. Scaffold
only stores and validates state; it never calls AI, runs code, invokes Conductor,
or retries implementation. The obsolete Pi `scaffold_run` extension still uses
retired commands and is intentionally unchanged in this increment.

### Schema and ordering

State is `.scaffold/tasks.json` in the working directory:

```json
{
  "schema_version": 1,
  "features": {
    "F1": {
      "id": "F1",
      "title": "Client activity",
      "objective": "Show client activity in the application",
      "design_ref": "docs/client-activity.md@approved-commit",
      "acceptance_criteria": ["Client activity can be recorded and retrieved"],
      "context": "Client service owns activity; existing repository adapter stores it",
      "constraints": ["Keep database types outside the domain"],
      "tasks": {
        "domain": {
          "id": "domain",
          "feature_id": "F1",
          "parent_id": null,
          "title": "Define activity",
          "objective": "Add the client activity domain type",
          "acceptance_criteria": ["Activity includes client id and timestamp"],
          "context": "Extend the client domain module",
          "constraints": ["No database dependencies"],
          "dependencies": [],
          "status": "pending",
          "history": []
        }
      }
    }
  }
}
```

Feature input requires `id`, `title`, `objective`, `design_ref` and nonempty
`acceptance_criteria`. Task input requires `id`, `title`, `objective` and nonempty
`acceptance_criteria`. Both accept `context` (string, default `""`) and
`constraints` (string array, default `[]`). Task input also accepts `dependencies`
(unique task ID array, default `[]`). Unknown input fields are rejected. Scaffold
assigns feature/parent IDs, status and history; callers cannot inject completion.
Keep sufficient implementation context in these definitions for a fresh agent
session. Use an immutable approved design reference that remains accessible.

Task IDs are unique within a feature and stable for its lifetime. Definitions,
completed tasks and history are retained; there is no deletion or implicit pop.
Dependencies must exist in the same feature, including tasks added in the same
atomic batch. Cycles, including deadlocks introduced by decomposition, are
rejected. Definition changes are intentionally not supported in v1.

`next` returns the first eligible task in insertion order, or `null`. It does not
claim work, resume interrupted tasks or imply the feature is finished. Check
`list` for active, blocked and failed work. Independent tasks may be ready at the
same time; Pi decides sequencing. Completed dependencies are mandatory at both
start and completion. Add explicit dependency chains for sequential work instead
of relying on LIFO ordering.

### CLI contract for the future Pi extension

All commands require `--feature ID`. Mutations of existing tasks also require
`--task ID`; no command changes a task based on an implicit current pointer.

| Command | Additional arguments | Result |
| --- | --- | --- |
| `tasks init` | `--input FILE` | Create a feature (fails if it exists) |
| `tasks add` | `--input FILE` | Add an array of one or more root tasks atomically |
| `tasks list` | none | Full feature, including every task and history |
| `tasks next` | none | Full feature plus selected task or `null` |
| `tasks start` | `--task ID` | Move a ready pending/decomposed task to `in_progress` |
| `tasks complete` | `--task ID --input FILE` | Record successful supplied verification and complete |
| `tasks block` | `--task ID --reason TEXT` | Block pending, in-progress or decomposed work |
| `tasks fail` | `--task ID --reason TEXT` | Record failure of in-progress work |
| `tasks retry` | `--task ID --reason TEXT` | Return interrupted/blocked/failed work to pending (or decomposed if it has children) |
| `tasks decompose` | `--task ID --input FILE` | Append an array of required child tasks |

`--input -` reads JSON from stdin. Successful commands print exactly one JSON
object to stdout and exit 0. Every response includes `schema_version: 1`.
Feature commands return `{schema_version, feature}`; task transitions return
`{schema_version, task}`; `next` returns `{schema_version, feature, task}`.
Data/state/I/O errors print `{schema_version, error}` to stderr and exit 1;
argument parsing errors exit 2 with argparse usage. Help remains human-readable.

The future extension can pass JSON over stdin with argument arrays, parse these
responses and retain explicit IDs. It must translate actual verification outcomes
into evidence; this CLI is not yet a native Conductor protocol consumer.

Completion evidence is a JSON object with these required fields:

```json
{
  "feature_id": "F1",
  "task_id": "domain",
  "result": "PASSED",
  "source": "conductor",
  "reference": "artifacts/domain-attempt-1.json",
  "summary": "Build, tests and independent review passed"
}
```

The source, reference and summary must be nonempty strings. Keep referenced
artifacts durable. Scaffold validates structure and task identity only: **it does
not independently verify, authenticate or read the supplied result or artifact**.
A caller can supply false evidence; trust remains with Pi and the independent
verification tooling. History timestamps are Scaffold's recording time, not proof
of execution. Completion requires `in_progress` and all prerequisites completed.
An identical completion replay is a no-op; different evidence for an already
completed task is rejected. No request can accidentally complete the next task.

### Recursive decomposition

Decomposition preserves the parent's objective, acceptance criteria and history,
then marks it `decomposed`. Pending, in-progress, blocked, failed or already
decomposed tasks can be split; completed tasks cannot. Repeated decomposition
appends new uniquely identified required children, preserving verified children.
A child can itself be decomposed using the same operation.

Children inherit ancestor dependencies for readiness. A parent must wait for all
its children, and downstream tasks continue to wait for the parent. After children
finish, the parent becomes eligible for `start`, followed by an explicit supplied
verification and `complete` against its original acceptance criteria. There is no
automatic parent completion. Blocking a parent pauses descendant eligibility;
explicitly retry the parent to resume. Decomposing blocked/failed work is itself
an explicit decision to resume through children. Any partially verified progress
remains in history/finished descendants; remaining unverified work belongs in the
new child definitions.

### Complete example

Run from the project root after approving `docs/client-activity.md`. These state
operations are executable; execution and verification between start/complete are
conceptual and remain Pi/Conductor's responsibility.

```bash
cat > feature.json <<'JSON'
{"id":"F1","title":"Client activity","objective":"Record client activity",
 "design_ref":"docs/client-activity.md@approved-commit",
 "acceptance_criteria":["Activity persists and can be retrieved"]}
JSON
scaffold tasks init --feature F1 --input feature.json

cat > tasks.json <<'JSON'
[
 {"id":"domain","title":"Activity type","objective":"Define activity domain type",
  "acceptance_criteria":["Activity identifies client and time"],
  "context":"Extend the client domain module","constraints":["No database types"]},
 {"id":"storage","title":"Store activity","objective":"Persist and retrieve activity",
  "acceptance_criteria":["Activity round trips through the repository adapter"],
  "dependencies":["domain"]}
]
JSON
scaffold tasks add --feature F1 --input tasks.json
scaffold tasks next --feature F1                     # domain
scaffold tasks start --feature F1 --task domain
# Pi supplies operations to Conductor; save its actual successful result.
cat > evidence.json <<'JSON'
{"feature_id":"F1","task_id":"domain","result":"PASSED","source":"conductor",
 "reference":"artifacts/domain.json","summary":"Domain checks and review passed"}
JSON
scaffold tasks complete --feature F1 --task domain --input evidence.json

# Pi finds storage too large and persists its remaining work as children.
cat > children.json <<'JSON'
[
 {"id":"write","title":"Write activity","objective":"Add adapter write operation",
  "acceptance_criteria":["Insert stores client and timestamp"]},
 {"id":"read","title":"Read activity","objective":"Add adapter query operation",
  "acceptance_criteria":["Query returns stored activity"],"dependencies":["write"]}
]
JSON
scaffold tasks decompose --feature F1 --task storage --input children.json
scaffold tasks next --feature F1                     # write
for id in write read storage; do
  scaffold tasks start --feature F1 --task "$id"
  # Pi executes and verifies this task independently with Conductor here.
  # Only after an actual PASSED result, record its corresponding evidence:
  cat > evidence.json <<JSON
{"feature_id":"F1","task_id":"$id","result":"PASSED","source":"conductor",
 "reference":"artifacts/$id.json","summary":"Task checks and review passed"}
JSON
  scaffold tasks complete --feature F1 --task "$id" --input evidence.json
done
scaffold tasks next --feature F1                     # task: null
scaffold tasks list --feature F1                     # all tasks completed; history retained
# Pi must still perform final feature-level validation against the approved design.
# Feature-level verification integration is a later phase.
```

If a check fails or a session stops:

```bash
scaffold tasks fail --feature F1 --task write --reason "Adapter test failed"
scaffold tasks retry --feature F1 --task write --reason "Corrective design prepared"
scaffold tasks start --feature F1 --task write
# Process/session exits before a result is recorded.
scaffold tasks list --feature F1                     # still in_progress
# Pi inspects working tree and any existing result before deciding whether to
# record completion, record failure, or explicitly retry. Retry runs no code:
scaffold tasks retry --feature F1 --task write --reason "Interrupted attempt reconciled"
scaffold tasks block --feature F1 --task write --reason "Awaiting schema decision"
scaffold tasks retry --feature F1 --task write --reason "Schema decision resolved"
```

The recovery fragment assumes an unfinished `write` task, not the completed state
at the end of the preceding example. Completed work cannot be retried or erased.

### Persistence, compatibility and tests

All task operations hold the existing cross-process file lock
(`.scaffold/.criteria-stack.lock`). Writes reuse the stack's fsynced temporary
file and atomic replacement, including directory fsync on Unix. After interruption
readers see the old or new complete document; stale `.tmp` files are ignored and
overwritten on the next write. OS locks release when the process exits. A crash
after replacement may have committed even if no response arrived: inspect `list`
before retrying. Unknown schema versions are rejected without migration/overwrite.
State is local to this working directory; sessions must use the same project root.

`fetch-ticket`, `status` and `stack push/pop/list/clear` keep their existing
behavior and file format. `status` still describes the criteria stack; use
`tasks list` for feature state. Clearing the old stack cannot erase task records.
No retired pipeline/TDD commands are restored. No Pi extension, Conductor,
Executor or Review changes are part of this increment.

```bash
pip install -e . pytest
python -m pytest
```

### Final feature validation lifecycle (Phase 5)

Task completion is distinct from feature verification. `tasks list` returns an
additive `task_revision` and `validation` view; schema version 1 files migrate
lazily without discarding task definitions or history. A completed nonempty task
graph is `awaiting_validation`, never automatically `verified`. Recursive parents
must have their own successful task completion evidence.

The native Pi feature gate uses three explicit JSON CLI transitions:

```sh
scaffold tasks validation-start --feature F1 --input intent.json
scaffold tasks validation-record --feature F1 --input retained-result.json
scaffold tasks validation-invalidate --feature F1 --input reason.json
```

An intent contains `attempt_id` and `binding`: `feature_id`, `design_ref`, original
`baseline`, final `head`, Git `tree`, `branch`, current `task_revision`, and SHA-256
`plan_digest`. Commit/tree fields require full lowercase SHAs. The first intent
pins `original_baseline`; subsequent attempts cannot replace it. Intents require
all tasks completed with task-bound Conductor evidence. Starting another attempt
preserves the interrupted attempt and makes it inapplicable.

A final record uses `schema: "pi.feature-validation/v1"`, matching `attempt_id`
and `binding`, `status` (`PASSED`, `NEEDS_DESIGN`, `BLOCKED`), durable
`reference` (`pi-feature-evidence:sha256:<64 hex>`), `checks`, `review`, and `message`.
PASSED requires meaningful successful full build/test checks and a completed
independent APPROVED review bound to the original baseline and final HEAD.
Only the matching active intent can finish. Identical recorded completions are
idempotent; conflicting results are rejected. History retains every started and
finished attempt, failure, blockage and explicit invalidation.

States are `implementing`, `awaiting_validation`, `validating`, `verified`,
`failed`, and `blocked`. Task mutations invalidate the current association and
advance `task_revision`; adding approved corrective tasks preserves the original
design, completed tasks, baseline and validation history. Invalidation payloads
contain a nonempty `reason`.

Scaffold validates associations and transitions, **not execution authenticity**.
CLI success-shaped JSON alone cannot establish that checks or review ran. Pi must
authenticate retained native evidence and checkpoint provenance before invoking
these transitions; they are deliberately absent from the generic `scaffold_tasks`
model-facing operation list. `validation.reference` is historical evidence, not
proof of applicability to the current checkout. On every resume, inspect Git HEAD,
tree, branch, task state and native evidence. Out-of-band repository changes make
the old result stale even when its historical status remains `verified`.
Never infer success from a `validating` intent or from `tasks next` returning null.

## Recursive issue design and implementation

`tasks init` accepts optional `"workflow": "recursive-issues/v1"`. It creates the
root issue using the feature ID from the agreed ticket; supply `baseline` (full
original Git SHA) and `branch` (feature branch) at init. the `tasks` API names stay
compatible. Existing features without this option retain their original behavior.
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
Without unfinished implementation a caller can reopen with just `{reason}` and
optional `--task`. Reopening preserves old designs and invalidates unfinished
descendant designs. Add and approve new issues, finish design, then implement new
leaves. Native Pi `resume_issue` authenticates prerequisite checkpoints and restores
saved source, then calls `issue-resume` with `{reference, handoff}`. The original
issue resumes using its retained attempt chain; design/retry/decomposition never
reset Conductor budgets. Completed nodes remain immutable; final-review corrections
are added as new dependent issues. All mutations invalidate current final-validation
applicability; historical results remain retained.
