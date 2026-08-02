# Agent-First Planning Strategy

## Status

Proposed

## Summary

Implement an `AgentPlanningStrategy` for Scaffold’s interchangeable planning-strategy architecture.

The strategy uses a single repository-aware LLM agent to understand the ticket, inspect the repository, assess the current implementation, identify already-satisfied requirements, resolve or surface material ambiguities, design the remaining changes, classify verification and implementation strategies, and submit a structured planning result.

Unlike `MechanicalPlanningStrategy`, the agent-first strategy does not execute separate planner and narrower sessions. Planning and gap analysis occur within one continuous agent context.

```text
PlanningRequest
        │
        ▼
AgentPlanningStrategy
        │
        ▼
PlanningResult
        │
        ▼
CriterionFrame factory
        │
        ▼
Mechanical grounding
        │
        ▼
Existing criteria-stack pipeline
```

The agent must submit its result through a terminal pseudo-tool rather than relying on free-form final text.

---

# 1. Background

Scaffold supports interchangeable planning implementations through a `PlanningStrategy` abstraction.

The current `MechanicalPlanningStrategy` preserves the original fixed workflow:

```text
ticket
  ↓
planner LLM session
  ↓
.tdd-plan.md
  ↓
narrower LLM session
  ↓
.gap-plan.md
  ↓
markdown parsing
  ↓
PlanningResult
```

This flow is predictable and inspectable, but the planner and narrower use separate model contexts, repository discoveries are not naturally retained, metadata is serialized into markdown and reparsed, and self-correction is constrained to predefined passes.

The agent-first strategy instead treats planning as one exploratory task:

```text
ticket + repository
        ↓
single planning agent
        ├── inspect
        ├── search
        ├── assess
        ├── clarify
        ├── reconsider
        ├── plan
        └── submit structured result
```

The downstream criteria-stack pipeline remains unchanged.

---

# 2. Goals

## 2.1 Primary goals

1. Implement `AgentPlanningStrategy` using the existing `PlanningStrategy` contract.
2. Perform planning and gap analysis in one continuous agent session.
3. Give the agent read-only repository exploration tools.
4. Require a structured terminal result.
5. Assess every ticket acceptance criterion against the current repository.
6. Return only remaining work as `PlannedCriterion` values.
7. Preserve satisfied-criterion assessments in human-readable artifacts.
8. Support material ambiguity handling through `ask_user_input`.
9. Keep deterministic validation and grounding outside the agent.
10. Remain interchangeable with `MechanicalPlanningStrategy`.
11. Preserve the existing criteria-stack execution flow.
12. Produce `.tdd-plan.md` and `.gap-plan.md` without making markdown authoritative.

## 2.2 Secondary goals

- Reduce duplicated repository exploration.
- Make assumptions and evidence auditable.
- Allow the agent to revise its understanding before submission.
- Collect diagnostics for benchmarking.
- Make failure states explicit.
- Support interactive and non-interactive planning modes.
- Enable comparison between mechanical and agent-first planning.

---

# 3. Non-goals

This change must not:

- replace `MechanicalPlanningStrategy`;
- change `CriterionFrame`;
- change criterion execution strategies;
- allow repository edits or arbitrary shell commands;
- implement ticket execution;
- bypass mechanical grounding;
- replace final ticket validation;
- make markdown authoritative;
- silently fall back to mechanical planning;
- modify the source ticket or create Linear issues;
- make planning asynchronous;
- remove the generic tool-loop safety ceiling globally;
- require human input for every ambiguity.

---

# 4. Terminology

## Agent-first planning

One agent owns repository exploration, requirement assessment, gap analysis, and implementation-plan construction. Application code still enforces structural and mechanical rules.

## Planning session

One invocation of the planning agent, including all model turns and tool calls until a terminal pseudo-tool is invoked.

## Terminal pseudo-tool

A tool call that ends planning with an explicit outcome.

Required terminal tools:

```text
submit_plan
planning_failed
```

`ask_user_input` is non-terminal when interactive input is available.

## Criterion disposition

Each criterion is classified as:

```text
remaining
satisfied
not_applicable
blocked
```

Only `remaining` criteria normally become `PlannedCriterion` values.

## Evidence

Repository-specific observations supporting an assessment. Evidence should identify repository paths whenever practical.

---

# 5. High-Level Architecture

```text
PlanningRequest
    │
    ▼
AgentPlanningStrategy
    ├── build initial context
    ├── create read-only executor
    ├── expose terminal pseudo-tools
    ├── run continuous agent loop
    └── receive AgentPlanSubmission
    │
    ▼
Submission validator
    ├── structure
    ├── criterion coverage
    ├── enums
    ├── evidence
    └── duplicate detection
    │
    ▼
Result adapter
    ├── remaining → PlannedCriterion[]
    ├── assumptions → diagnostics
    ├── all assessments → full plan
    └── remaining → gap plan
    │
    ▼
PlanningResult
    │
    ▼
Existing frame construction and grounding
```

---

# 6. Strategy Behaviour

## 6.1 Single-session requirement

The same model context must own ticket interpretation, repository inspection, current-state assessment, implementation planning, verification classification, and final submission.

The strategy must not internally reproduce separate agent planner and agent narrower sessions.

## 6.2 Initial context

The initial prompt should include:

1. complete ticket content;
2. repository orientation;
3. toolchain information;
4. cheaply prefetched referenced files;
5. structured submission contract;
6. tool-use rules;
7. criterion-assessment rules;
8. ambiguity rules;
9. prohibition on edits;
10. instruction that `submit_plan` is the only successful completion mechanism.

The prompt must not preload the whole repository.

## 6.3 Repository exploration

The agent may search for symbols, paths, routes, modules, types, tests, configuration, analogous implementations, and project conventions.

It should prefer targeted searches over broad traversal and stop once sufficient evidence exists.

## 6.4 Criterion dispositions

### `remaining`

The criterion is not satisfied and requires repository changes.

### `satisfied`

The repository already satisfies the criterion. Concrete evidence is required.

### `not_applicable`

The criterion does not apply or is invalidated by repository-specific facts. This should be rare and strongly justified.

### `blocked`

A safe plan cannot be produced because material information is unavailable. The agent should normally call `ask_user_input` or `planning_failed`.

---

# 7. Public Strategy Interface

```python
class AgentPlanningStrategy:
    def plan(self, request: PlanningRequest) -> PlanningResult:
        ...
```

Agent-specific types must remain internal.

Suggested module structure:

```text
src/ticket_pipeline/planning/
├── strategies/
│   ├── mechanical.py
│   └── agent.py
├── agent_models.py
├── agent_prompt.py
├── agent_runner.py
├── agent_tools.py
├── agent_validation.py
└── agent_rendering.py
```

---

# 8. Agent Submission Model

The agent should submit a richer internal model than `PlanningResult`.

```python
from dataclasses import dataclass
from typing import Literal

CriterionDisposition = Literal[
    "remaining",
    "satisfied",
    "not_applicable",
    "blocked",
]

@dataclass(frozen=True)
class AgentEvidence:
    path: str | None
    observation: str

@dataclass(frozen=True)
class AgentAssumption:
    question: str
    answer: str
    basis: str

@dataclass(frozen=True)
class PlannedChange:
    path: str
    description: str
    symbols: tuple[str, ...] = ()

@dataclass(frozen=True)
class AgentCriterionAssessment:
    criterion_id: str
    source_criterion: str
    disposition: CriterionDisposition
    rationale: str
    evidence: tuple[AgentEvidence, ...]
    planned_changes: tuple[PlannedChange, ...] = ()
    verification: str | None = None
    implementation_strategy: str | None = None
    existing_test_refs: tuple[str, ...] = ()
    plan_context: str | None = None
    blocker: str | None = None

@dataclass(frozen=True)
class AgentPlanSubmission:
    ticket_summary: str
    approach_summary: str
    assumptions: tuple[AgentAssumption, ...]
    repository_findings: tuple[AgentEvidence, ...]
    criteria: tuple[AgentCriterionAssessment, ...]
    cross_cutting_changes: tuple[PlannedChange, ...] = ()
    risks: tuple[str, ...] = ()
    validation_notes: tuple[str, ...] = ()
```

Equivalent validated models are acceptable.

The richer model preserves satisfied criteria, evidence, assumptions, blockers, cross-cutting changes, risks, and rationale without polluting `CriterionFrame`.

---

# 9. Terminal Pseudo-Tools

## 9.1 `submit_plan`

`submit_plan` is the only successful completion mechanism.

It must:

1. deserialize arguments;
2. validate the submission;
3. terminate the loop;
4. return the typed submission.

It must not write files itself.

## 9.2 `ask_user_input`

Use only for material product or implementation decisions that cannot be resolved from the ticket, repository, or established conventions.

Suggested arguments:

```text
question
why_needed
options
recommended_option
```

Each option should include implications.

Do not use it for low-risk internal implementation choices.

## 9.3 `planning_failed`

Suggested arguments:

```text
reason
category
recoverable
suggested_action
```

Supported categories:

```text
insufficient_ticket
repository_unavailable
unsupported_repository
conflicting_requirements
tool_failure
other
```

This tool terminates the loop and raises `PlanningError`.

---

# 10. Agent Loop Semantics

## 10.1 Required completion

Plain text without `submit_plan` or `planning_failed` is a protocol violation.

## 10.2 Recommended implementation

Add a specialized wrapper:

```python
def run_agent_until_terminal(
    *,
    prompt: str,
    tools: list[dict],
    executor: ToolExecutor,
    terminal_tools: set[str],
    model: str,
    max_turns: int,
) -> TerminalToolResult:
    ...
```

Alternatively, add opt-in terminal-tool semantics to `run_with_tools()` while preserving all existing defaults.

## 10.3 Tool result categories

```text
ordinary tool
    execute and continue

interactive pseudo-tool
    obtain input and continue

successful terminal tool
    validate and return

failure terminal tool
    raise structured error

plain text
    protocol violation
```

## 10.4 Turn ceiling

Retain a generous safety ceiling, initially 40 turns. Terminal pseudo-tools define semantic completion; the ceiling protects against pathological loops.

## 10.5 Cost ceiling

Continue applying the existing process-level cost ceiling. Budget exhaustion is non-retryable and must not restart planning.

## 10.6 Submission repair

An invalid `submit_plan` payload may be returned to the same model context with concise validation errors.

Recommended maximum:

```text
2 invalid submission attempts
```

Do not restart the whole planning session.

---

# 11. User Input Modes

Recommended configuration:

```toml
[planning_agent]
user_input = "interactive"
```

Supported values:

```text
interactive
infer
fail
```

## `interactive`

Render the question and return the user response to the same model context.

## `infer`

Tell the agent to choose its recommended option, record it as an assumption, and continue.

## `fail`

Raise a dedicated `PlanningInputRequired` error for external handling.

An optional `auto` mode may map interactive stdin to `interactive` and non-interactive stdin to `infer`.

---

# 12. Read-Only Tool Set

Required:

```text
read_file
list_dir
search_files
```

Optional read-only tools:

```text
file_exists
read_git_diff
git_status
find_symbol
```

Forbidden:

```text
write_file
edit_file
delete_file
run_command
apply_patch
git_commit
git_checkout
```

Reuse preloaded-path deduplication and optionally cache identical read-only calls.

---

# 13. Prompt Requirements

Create:

```text
src/ticket_pipeline/prompts/agent-plan.prompt.md
```

The prompt must define:

- role and objective;
- operating principles;
- exploration process;
- criterion dispositions;
- ambiguity rules;
- evidence requirements;
- verification classification;
- implementation-strategy classification;
- terminal-tool protocol;
- prohibited behaviour.

Suggested core instruction:

```text
You are the planning agent for Scaffold.

Determine the smallest complete repository-grounded implementation plan
for the supplied ticket. You may inspect the repository but must not
modify it.

You own both planning and current-state gap analysis. Do not produce a
greenfield plan. First determine what already exists, then plan only the
remaining work.
```

Before submission, the agent must verify that every criterion is represented, satisfied claims have evidence, remaining criteria have actionable changes, paths are grounded, verification and implementation strategies are appropriate, existing tests are reused where applicable, and no unsupported requirement was invented.

The prompt should request concise assumptions, evidence, rationale, and structured conclusions—not visible chain-of-thought.

---

# 14. Criterion Coverage

Every explicit acceptance criterion must have exactly one assessment.

The request-preparation layer should deterministically extract criteria and assign stable IDs:

```text
AC-1
AC-2
AC-3
```

The validator must reject omitted, duplicated, or unknown IDs.

For tickets without explicit acceptance criteria, the agent may derive criteria, but each must be marked as derived and include a necessity rationale.

---

# 15. Submission Validation

## 15.1 Structural validation

Validate required fields, types, enums, non-empty strings, unique criterion IDs, and duplicate paths where inappropriate.

## 15.2 Disposition rules

### `remaining`

Requires rationale, actionable planned changes, verification mode, implementation strategy, and plan context.

### `satisfied`

Requires rationale and concrete evidence. It must not contain planned changes.

### `not_applicable`

Requires strong rationale and no planned changes.

### `blocked`

Requires a blocker description, rationale, attempted evidence gathering, and no fabricated plan.

## 15.3 Supported verification values

```text
test
test-refactor
refactor
manual
```

## 15.4 Supported implementation strategies

```text
tdd
direct
manual
refactor
```

## 15.5 Path validation

Evidence paths should exist or be explicitly described as absent. Planned new paths must be grounded in the surrounding repository structure.

## 15.6 No-gap result

A submission where all criteria are satisfied or not applicable is valid and produces:

```python
PlanningResult(criteria=())
```

---

# 16. Conversion to `PlanningResult`

Only `remaining` assessments become `PlannedCriterion` values.

```python
def to_planning_result(
    submission: AgentPlanSubmission,
) -> PlanningResult:
    remaining = tuple(
        PlannedCriterion(
            criterion=assessment.source_criterion,
            plan_context=render_plan_context(assessment),
            verification=assessment.verification or "test",
            implementation_strategy=(
                assessment.implementation_strategy or "tdd"
            ),
            existing_test_refs=assessment.existing_test_refs,
        )
        for assessment in submission.criteria
        if assessment.disposition == "remaining"
    )

    return PlanningResult(
        criteria=remaining,
        plan_text=render_agent_full_plan(submission),
        narrowed_plan_text=render_agent_gap_plan(submission),
        diagnostics=build_agent_diagnostics(submission),
    )
```

`plan_context` should be deterministically rendered from rationale, files, symbols, expected changes, evidence, dependencies, verification, and risks.

No downstream component should parse plan context for metadata.

---

# 17. Planning Artifacts

## `.ticket.md`

Write the exact ticket snapshot.

## `.tdd-plan.md`

Deterministically render the complete agent planning report:

```markdown
# Agent Planning Report

## Ticket Summary
## Repository Findings
## Assumptions
## Criterion Assessments
## Cross-Cutting Changes
## Risks
## Implementation Plan
## Verification Plan
```

Include both satisfied and remaining criteria.

## `.gap-plan.md`

Render only remaining work:

```markdown
# Remaining Work

## Implementation Plan
## Acceptance Criteria
```

Compatibility with `--from-gap-plan` is desirable and should be preserved unless explicitly migrated.

The authoritative live result is the validated structured submission converted to `PlanningResult`.

---

# 18. Configuration

## Strategy

```toml
planning_strategy = "agent"
```

or:

```bash
scaffold push-ticket SA-453 --planning-strategy agent
```

## Model

```toml
[step_models]
agent_plan = "opencode:claude-sonnet-4-6"
```

Do not reuse separate `plan` and `narrow` model keys for the single agent session.

## Agent settings

```toml
[planning_agent]
user_input = "interactive"
max_turns = 40
max_invalid_submissions = 2
```

Unknown settings should fail clearly.

---

# 19. Existing CLI Modes

## `--from-gap-plan`

Bypasses the agent strategy.

## `--validate-only`

Does not invoke planning.

## `--ticket-file-in`

Its content becomes `PlanningRequest.ticket_content`. The agent must not separately fetch Linear.

## `--explore`

Reject with the agent strategy because repository exploration is already built in:

```text
--explore is not compatible with the agent planning strategy because
repository exploration is already part of that strategy.
```

## `--strategy`

Continues to override the per-criterion implementation strategy after planning. It must not affect planning-strategy selection.

---

# 20. Mechanical Grounding

Agent-generated remaining criteria must use the existing frame factory and grounding checks:

```text
AgentPlanSubmission
    ↓
PlanningResult
    ↓
candidate CriterionFrames
    ↓
filter_grounded_frames()
    ↓
accepted stack frames
```

Do not automatically feed grounding failures back to the agent in the initial implementation.

---

# 21. Final Ticket Validation

Final validation remains mechanical:

```text
Initial planning:
    AgentPlanningStrategy

Criterion execution:
    existing strategy handlers

Final validation:
    existing re-narrow, lint, test, smoke, and review gates
```

This provides an independent check against planning omissions.

---

# 22. Error Handling

## Protocol violation

Plain-text completion without a terminal tool should produce a clear protocol error. One corrective prompt may be allowed.

## Invalid submission

Return concise errors to the same session, bounded by the invalid-submission limit.

## Input required

In fail mode, raise:

```python
class PlanningInputRequired(PlanningError):
    ...
```

## Explicit failure

Surface failure category, reason, recoverability, and suggested action.

## Tool failure

Return recoverable read/search failures to the agent. Repeated repository-wide failure terminates planning.

## No fallback

Agent failure must never silently invoke mechanical planning. The user may explicitly rerun with `--planning-strategy mechanical`.

---

# 23. Logging and Diagnostics

Record:

- selected strategy;
- session start;
- tool calls by type;
- user-input requests;
- submission attempts;
- validation failures;
- terminal outcome;
- turn count;
- token usage;
- estimated cost;
- remaining and satisfied counts.

Recommended summary:

```text
-- Agent planning complete:
   5 ticket criteria assessed
   3 remaining
   2 already satisfied
   9 repository files inspected
   14 tool turns
```

Do not log hidden model reasoning.

---

# 24. Benchmarking

Compare mechanical and agent-first planning using fixed ticket and repository snapshots.

Evaluate:

1. criterion coverage;
2. satisfied/remaining accuracy;
3. path accuracy;
4. plan completeness;
5. verification classification;
6. unnecessary changes;
7. hallucinated files or symbols;
8. tool calls;
9. model turns;
10. tokens and cost;
11. wall-clock duration;
12. grounding rejection rate;
13. final-validation missed-criterion rate.

Correctness and downstream success are more important than minimizing calls.

---

# 25. Testing Requirements

## Models

Test construction, enums, required fields, immutability, and allowed empty collections.

## Terminal tools

Test successful submission, explicit failure, user-input continuation, malformed arguments, duplicate terminal calls, and immediate termination after success.

## Agent runner

Test ordinary tools, terminal completion, plain-text violations, corrective prompting, turn and cost ceilings, same-context repair, invalid-submission limits, and provider retries.

## Criterion coverage

Test omitted, duplicate, unknown, and mismatched criteria, plus derived criteria for tickets without explicit acceptance criteria.

## Dispositions

Test required and prohibited fields for every disposition.

## Adapter

Test that only remaining criteria become frames, satisfied criteria remain in artifacts, verification and strategies are copied, existing tests are preserved, no-gap results are valid, and assumptions become diagnostics.

## Artifacts

Test deterministic `.tdd-plan.md` and `.gap-plan.md` rendering and `--from-gap-plan` compatibility where required.

## Integration

Use a fake transcript:

```text
assistant → search_files
tool → results
assistant → read_file
tool → content
assistant → submit_plan
```

Verify the complete path through `PlanningResult`, frame construction, and grounding without live model calls.

## CLI

Test agent selection, model resolution, ticket-file input, strategy override, explore incompatibility, input modes, and no silent fallback.

All existing mechanical and criteria-stack tests must continue to pass.

---

# 26. Implementation Phases

1. Add agent-specific models and validation tests.
2. Add terminal-aware agent-runner semantics without changing existing defaults.
3. Define `submit_plan`, `ask_user_input`, and `planning_failed`.
4. Add `agent-plan.prompt.md`.
5. Implement submission validation.
6. Implement result adaptation and artifact rendering.
7. Implement and register `AgentPlanningStrategy`.
8. Add configuration and CLI integration.
9. Add integration and regression tests.
10. Add agent-versus-mechanical benchmarks.

---

# 27. Acceptance Criteria

## Strategy

- [ ] `AgentPlanningStrategy` implements `PlanningStrategy`.
- [ ] One continuous agent session performs planning and gap analysis.
- [ ] The agent has read-only repository access.
- [ ] The agent cannot edit files or run arbitrary commands.
- [ ] The strategy is selectable as `agent`.
- [ ] Agent failure never silently falls back.

## Protocol

- [ ] Success requires `submit_plan`.
- [ ] Explicit failure uses `planning_failed`.
- [ ] Material questions use `ask_user_input`.
- [ ] Plain text is a protocol violation.
- [ ] Existing `run_with_tools()` callers retain current behaviour.
- [ ] Turn and cost ceilings remain enforced.
- [ ] Invalid submissions can be corrected in the same context.
- [ ] Submission retries are bounded.

## Planning quality

- [ ] Every explicit criterion has exactly one assessment.
- [ ] Satisfied criteria require evidence.
- [ ] Remaining criteria have actionable changes.
- [ ] Remaining criteria specify verification and implementation strategy.
- [ ] Material assumptions and findings are recorded.
- [ ] The agent self-reviews before submission.
- [ ] Structured output is authoritative.

## Integration

- [ ] Only remaining assessments become `PlannedCriterion`.
- [ ] Satisfied assessments create no frames.
- [ ] Existing frame factory and grounding are used.
- [ ] `--strategy` overrides agent-selected implementation strategy.
- [ ] `--ticket-file-in` is respected.
- [ ] `--from-gap-plan` and `--validate-only` bypass the agent.
- [ ] `--explore` is explicitly rejected or handled.
- [ ] Final validation remains mechanical.

## Artifacts

- [ ] `.ticket.md` contains the ticket snapshot.
- [ ] `.tdd-plan.md` contains the full agent report.
- [ ] `.gap-plan.md` contains remaining work.
- [ ] Rendering is deterministic.
- [ ] Live downstream logic does not reparse agent markdown.
- [ ] Gap-plan compatibility is preserved or explicitly migrated.

## Tests

- [ ] Agent models, terminal tools, loop protocol, coverage, dispositions, adaptation, rendering, CLI modes, and a fake-model integration transcript are tested.
- [ ] Existing mechanical and criteria-stack tests pass.

---

# 28. Follow-up Opportunities

Keep these separate initially:

- feeding grounding failures back to the agent;
- resumable sessions;
- persisted unanswered questions;
- automatic ticket edits or splitting;
- multi-agent planning;
- dependency graphs;
- automatic strategy selection;
- confidence scoring;
- agent-based final validation;
- removal of markdown compatibility artifacts.

---

# 29. Design Rationale

The agent owns semantic exploration:

- relevant evidence;
- current satisfaction state;
- affected files and symbols;
- repository-conventional implementation approach;
- material ambiguities;
- remaining-work structure.

Application code owns deterministic control:

- available tools;
- completion protocol;
- submission validation;
- criterion coverage;
- enums and path checks;
- budgets;
- frame conversion;
- grounding;
- final validation.

```text
                    Semantic responsibility
                              │
                              ▼
                     Agent planning session
                  inspect → assess → plan → submit
                              │
                              ▼
                    Structured terminal result
                              │
                              ▼
                   Deterministic responsibility
                              │
              validate → adapt → ground → persist
                              │
                              ▼
                    Existing execution pipeline
```

This creates a genuinely agent-first planner without making the rest of Scaffold agent-dependent.
