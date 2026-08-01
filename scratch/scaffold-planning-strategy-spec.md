# Extract Planning into Interchangeable Planning Strategies

## Status

Proposed

## Summary

Refactor the ticket-planning portion of Scaffold into an interchangeable strategy abstraction.

The current planning process must become the first implementation, named `MechanicalPlanningStrategy`. A second implementation, `AgentPlanningStrategy`, will later support a more autonomous LLM-driven planning workflow.

Both implementations must produce the same structured planning result so that the downstream criteria-stack pipeline remains independent of how the plan was generated.

The intended architecture is:

```text
Ticket content + repository context
                │
                ▼
        PlanningStrategy
                │
                ▼
         PlanningResult
                │
                ▼
    Mechanical grounding checks
                │
                ▼
      CriterionFrame creation
                │
                ▼
     Existing criteria-stack flow
```

The refactor must preserve the current behaviour by default.

---

# 1. Background

The current ticket initialization flow is primarily implemented in:

```text
src/ticket_pipeline/push_ticket.py
src/ticket_pipeline/lib/pipeline_lib.py
```

`push_ticket.resolve_ticket_frames()` currently performs several responsibilities:

1. Loads or fetches the ticket.
2. Removes transient planning artifacts.
3. Runs the planning pipeline.
4. Reads the resulting gap plan.
5. Extracts remaining acceptance criteria.
6. Extracts per-criterion metadata.
7. Constructs `CriterionFrame` instances.
8. Applies mechanical grounding checks.
9. Returns grounded frames for insertion into the criteria stack.

The planning pipeline itself is currently represented as a fixed sequence of blocks:

```text
fetch_ticket
    ↓
planner
    ↓
narrower
```

Each block is file-oriented and uses a filesystem postcondition to determine whether it has already completed:

```text
.ticket.md
.tdd-plan.md
.gap-plan.md
```

The same block construction is also reused by the final ticket-validation flow.

Although the planner and narrower use LLM calls, the orchestration is mechanical:

- application code controls the exact sequence;
- each model invocation has a bounded, predefined purpose;
- intermediate responses must conform to markdown formats;
- markdown is parsed deterministically;
- the resulting criteria are passed into the criteria-stack workflow.

The future agent-driven planner will differ in orchestration. It may:

- inspect the repository iteratively;
- identify and resolve uncertainty;
- invoke pseudo-tools such as `ask_user_input`;
- revise earlier conclusions;
- challenge its own proposed plan;
- produce structured criteria directly.

The downstream pipeline should not need to know which planning process produced the result.

---

# 2. Goals

## 2.1 Primary goals

1. Introduce a `PlanningStrategy` abstraction.
2. Preserve the current planning behaviour as `MechanicalPlanningStrategy`.
3. Define a structured planning-domain result shared by all implementations.
4. Separate planning from:
   - planning artifact persistence;
   - `CriterionFrame` construction;
   - criteria-stack state;
   - mechanical grounding checks.
5. Make planning implementations selectable through configuration and optionally the command line.
6. Leave the existing mechanical strategy as the default.
7. Create a stable extension point for a future `AgentPlanningStrategy`.
8. Preserve compatibility with existing stack files and downstream execution logic.

## 2.2 Secondary goals

1. Make the planning flow independently testable.
2. Reduce the responsibilities of `resolve_ticket_frames()`.
3. Avoid requiring future planning implementations to generate and reparse markdown metadata.
4. Preserve current diagnostic artifacts where useful.
5. Allow future planning implementations to provide additional diagnostics without changing criteria-stack behaviour.

---

# 3. Non-goals

This change must not:

1. Implement the complete autonomous agent planner unless explicitly included in a follow-up ticket.
2. Change the execution behaviour of:
   - `tdd`;
   - `direct`;
   - `manual`;
   - `refactor`.
3. Rename or alter `CriterionFrame.strategy`.
4. Remove `.ticket.md`, `.tdd-plan.md`, or `.gap-plan.md` from the mechanical implementation.
5. Replace the final mechanical ticket-validation gate.
6. Redesign `run_with_tools`.
7. Change the criteria-stack persistence format.
8. Change the current grounding rules.
9. Change how tickets are fetched from Linear.
10. Change the existing planner or narrower prompts.
11. Change the current planning outputs for the default strategy, except where required to remove accidental coupling.
12. Introduce asynchronous or background planning.

---

# 4. Terminology

## Planning strategy

The mechanism used to turn a ticket and repository context into a structured set of planned criteria.

Examples:

```text
MechanicalPlanningStrategy
AgentPlanningStrategy
```

## Implementation strategy

The method used to implement an individual criterion.

Existing values include:

```text
tdd
direct
manual
refactor
```

This is currently stored in `CriterionFrame.strategy`.

Planning strategy and implementation strategy are separate concepts and must not share configuration names or command-line options.

## Planned criterion

A planning-domain representation of one actionable acceptance criterion.

It contains information required to later construct a `CriterionFrame`, but does not contain execution state.

## Planning result

The complete result returned by a planning strategy.

It contains planned criteria and optional human-readable planning artifacts.

## Mechanical planning

The existing fixed `fetch → plan → narrow → parse` workflow.

“Mechanical” describes the orchestration, not the absence of LLM calls.

## Agent planning

A future workflow in which an LLM agent controls a more flexible planning process and may perform repeated exploration, clarification, critique, and revision.

---

# 5. Existing Behaviour to Preserve

The default path must continue to behave as follows:

```text
push-ticket <ticket-id>
    ↓
fetch ticket or read --ticket-file-in
    ↓
write .ticket.md
    ↓
generate .tdd-plan.md
    ↓
generate .gap-plan.md
    ↓
extract remaining criteria
    ↓
extract verify/strategy/existing_test metadata
    ↓
construct candidate CriterionFrames
    ↓
mechanically ground frames
    ↓
write criteria stack
```

The current planner:

- receives the ticket content directly;
- receives repository-orientation context;
- may inspect additional repository files with read-only tools;
- writes a plan containing `## Acceptance Criteria`;
- persists the result to `.tdd-plan.md`.

The narrower:

- receives the ticket;
- receives the generated plan;
- receives current content for files named by the implementation plan;
- checks which criteria are already satisfied;
- produces a reduced plan;
- persists the result to `.gap-plan.md`.

The gap plan currently carries metadata through tags parsed by:

```text
extract_verification_mode()
extract_strategy()
extract_existing_test_refs()
```

The current implementation-strategy values are:

```text
tdd
direct
manual
refactor
```

These values remain part of each criterion and must not be confused with the planning-strategy selection.

---

# 6. Proposed Architecture

Introduce a planning package:

```text
src/ticket_pipeline/planning/
├── __init__.py
├── models.py
├── strategy.py
├── factory.py
├── artifacts.py
├── parsing.py
└── strategies/
    ├── __init__.py
    ├── mechanical.py
    └── agent.py
```

The initial implementation may omit `agent.py` or include only a clearly unsupported placeholder.

## 6.1 Responsibility boundaries

### `planning/models.py`

Owns planning-domain models:

```text
PlanningRequest
PlanningResult
PlannedCriterion
PlanningDiagnostic
```

### `planning/strategy.py`

Owns the `PlanningStrategy` protocol or abstract base class.

### `planning/strategies/mechanical.py`

Owns the current mechanical planning implementation.

It may initially delegate to existing functions in `pipeline_lib.py`.

### `planning/parsing.py`

Owns conversion from mechanical gap-plan markdown into structured `PlannedCriterion` values.

### `planning/artifacts.py`

Owns optional persistence of human-readable planning outputs.

This module should be introduced only if it improves the first extraction. It is acceptable for the mechanical implementation to continue using the current file-writing functions during the initial migration.

### `planning/factory.py`

Resolves configuration into a concrete strategy.

### `push_ticket.py`

Owns CLI behaviour, stack guards, ticket loading, frame grounding, and stack seeding.

It must no longer know the internal steps used by the selected planning strategy.

### `pipeline_lib.py`

Continues to own shared pipeline functionality during the migration.

Planning-specific functions may be moved gradually rather than all at once.

---

# 7. Data Model

## 7.1 `PlanningRequest`

Create an immutable request model.

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlanningRequest:
    ticket_id: str
    ticket_content: str
    project_root: Path
    model: str
    step_models: dict[str, str]
```

### Required fields

#### `ticket_id`

The ticket identifier, for example:

```text
SA-453
```

Used for logging, diagnostics, and artifact attribution.

#### `ticket_content`

The complete rendered ticket markdown.

Ticket retrieval must happen outside the planning strategy.

This ensures all strategies receive the same ticket representation and prevents strategy implementations from becoming coupled to Linear.

#### `project_root`

The repository root against which planning is performed.

The initial implementation may use `Path.cwd()` if the application already assumes execution from the repository root.

#### `model`

The fallback model resolved by the existing model configuration system.

#### `step_models`

Existing per-step model overrides.

The mechanical implementation will use values such as:

```text
plan
narrow
```

A future agent implementation may use:

```text
planning_agent
```

or its own documented configuration key.

## 7.2 `PlannedCriterion`

Create an immutable planning-domain criterion.

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlannedCriterion:
    criterion: str
    plan_context: str
    verification: str = "test"
    implementation_strategy: str = "tdd"
    existing_test_refs: tuple[str, ...] = field(default_factory=tuple)
```

### Field definitions

#### `criterion`

The complete acceptance-criterion text.

For mechanical planning, this may retain the original markdown bullet and metadata comment for compatibility.

Future structured implementations should not be required to encode metadata inside the string.

#### `plan_context`

The implementation-plan context relevant to this criterion.

This must contain enough information for downstream test-writing, implementation, and recheck steps.

#### `verification`

One of:

```text
test
test-refactor
refactor
manual
```

Validation of supported values should happen at the planning boundary or frame-factory boundary.

#### `implementation_strategy`

One of:

```text
tdd
direct
manual
refactor
```

This maps to `CriterionFrame.strategy`.

#### `existing_test_refs`

Zero or more references in the existing format:

```text
path/to/test_file.py::qualified_test_name
```

Use an immutable tuple in the planning model. Convert it to a list when constructing `CriterionFrame` if required by the current frame schema.

## 7.3 `PlanningDiagnostic`

Introduce an optional structured diagnostic model.

```python
from dataclasses import dataclass
from typing import Literal


DiagnosticLevel = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class PlanningDiagnostic:
    level: DiagnosticLevel
    message: str
    code: str | None = None
```

Diagnostics may describe:

- inferred assumptions;
- unsupported ticket structure;
- ambiguities resolved automatically;
- incomplete repository evidence;
- agent warnings;
- fallbacks used.

Diagnostics must not be used as criteria-stack state.

## 7.4 `PlanningResult`

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlanningResult:
    criteria: tuple[PlannedCriterion, ...]
    plan_text: str | None = None
    narrowed_plan_text: str | None = None
    diagnostics: tuple[PlanningDiagnostic, ...] = field(default_factory=tuple)
```

### `criteria`

The authoritative structured output.

Downstream frame construction must use this field rather than reparsing `plan_text` or `narrowed_plan_text`.

### `plan_text`

Optional human-readable full plan.

For the mechanical strategy, this should contain the current `.tdd-plan.md` content.

### `narrowed_plan_text`

Optional human-readable narrowed or gap plan.

For the mechanical strategy, this should contain the current `.gap-plan.md` content.

An agent strategy may leave this as `None` or provide a synthesized summary.

### `diagnostics`

Optional structured planning information.

---

# 8. Strategy Interface

Define a protocol:

```python
from typing import Protocol


class PlanningStrategy(Protocol):
    def plan(self, request: PlanningRequest) -> PlanningResult:
        ...
```

The interface must:

1. Accept all required ticket and model context through `PlanningRequest`.
2. Return a complete `PlanningResult`.
3. Avoid direct knowledge of:
   - stack persistence;
   - stack guards;
   - `CriterionFrame` execution state;
   - final ticket validation;
   - git commit state.
4. Be synchronous.
5. Raise explicit planning errors instead of calling `sys.exit()` where practical.

## 8.1 Planning-specific error

Introduce:

```python
class PlanningError(RuntimeError):
    pass
```

Where existing planning functions call `die()`, the first extraction may continue to use existing behaviour internally. However, the strategy-facing contract should move toward raising `PlanningError`.

`push_ticket.py` should convert an uncaught `PlanningError` into the current user-facing pipeline failure behaviour.

---

# 9. Mechanical Planning Strategy

## 9.1 Purpose

`MechanicalPlanningStrategy` preserves the current fixed planning workflow.

Its internal sequence is:

```text
Prepare planning scratch state
    ↓
Run mechanical planning blocks
    ↓
Read full plan
    ↓
Read narrowed plan
    ↓
Parse narrowed plan into PlannedCriterion objects
    ↓
Return PlanningResult
```

## 9.2 Initial implementation

The first implementation should wrap existing behaviour rather than rewrite it.

Illustrative shape:

```python
class MechanicalPlanningStrategy:
    def plan(self, request: PlanningRequest) -> PlanningResult:
        remove_scratch_files(
            (
                TICKET_FILE,
                PLAN_FILE,
                GAP_PLAN_FILE,
            )
        )

        TICKET_FILE.write_text(
            request.ticket_content,
            encoding="utf-8",
        )

        walk(
            build_planning_blocks(
                ticket_id=request.ticket_id,
                model=request.model,
                step_models=request.step_models,
                ticket_file_in=TICKET_FILE,
            )
        )

        plan_text = PLAN_FILE.read_text(encoding="utf-8")
        narrowed_plan_text = GAP_PLAN_FILE.read_text(encoding="utf-8")

        criteria = parse_gap_plan(narrowed_plan_text)

        return PlanningResult(
            criteria=tuple(criteria),
            plan_text=plan_text,
            narrowed_plan_text=narrowed_plan_text,
        )
```

The exact implementation may avoid rewriting `.ticket.md` twice by splitting ticket persistence from `build_planning_blocks()`.

The refactor should prefer behavioural preservation over immediate elegance.

## 9.3 Required preserved behaviour

The mechanical strategy must preserve:

1. Current planner prompt.
2. Current narrower prompt.
3. Current model resolution:
   - `plan`;
   - `narrow`;
   - fallback model.
4. Existing read-only tool access.
5. Existing retries.
6. Existing logging and token accounting.
7. Existing validation that planner output contains `## Acceptance Criteria`.
8. Existing validation that narrower output contains `## Acceptance Criteria`.
9. Existing writing of:
   - `.ticket.md`;
   - `.tdd-plan.md`;
   - `.gap-plan.md`.
10. Existing re-entrant block semantics where applicable.
11. Existing gap-plan parsing semantics.
12. Existing no-gap behaviour.

## 9.4 Mechanical markdown adapter

Move gap-plan parsing into a dedicated function:

```python
def parse_gap_plan(gap_plan_text: str) -> list[PlannedCriterion]:
    criteria = extract_acceptance_criteria(gap_plan_text)

    return [
        PlannedCriterion(
            criterion=criterion,
            plan_context=extract_plan_context_for_criterion(
                criterion,
                gap_plan_text,
            ),
            verification=extract_verification_mode(criterion),
            implementation_strategy=extract_strategy(criterion),
            existing_test_refs=tuple(
                extract_existing_test_refs(criterion)
            ),
        )
        for criterion in criteria
    ]
```

This adapter is specific to the mechanical markdown format.

No future strategy should be required to call it.

---

# 10. Agent Planning Strategy

The initial refactor does not need to fully implement this class, but the interface must support it without further changes to `push_ticket.py`.

## 10.1 Expected conceptual flow

```text
Receive PlanningRequest
    ↓
Inspect ticket
    ↓
Inspect repository
    ↓
Identify ambiguities and missing context
    ↓
Resolve mechanically inferable questions
    ↓
Optionally emit ask_user_input pseudo-tool call
    ↓
Revise assumptions
    ↓
Construct implementation plan
    ↓
Critique plan against repository state
    ↓
Classify remaining criteria
    ↓
Return structured PlanningResult
```

## 10.2 Structured output

The agent strategy should return `PlannedCriterion` values directly.

It should not be required to:

1. produce HTML metadata comments;
2. write `.gap-plan.md`;
3. serialize criteria to markdown;
4. invoke `extract_strategy()`;
5. invoke `extract_verification_mode()`;
6. reparse its own final response.

## 10.3 Pseudo-tool behaviour

A future implementation may use pseudo-tool calls such as:

```text
ask_user_input
planning_complete
planning_failed
```

These calls should be internal to the agent runner.

The `PlanningStrategy` contract remains:

```python
PlanningRequest -> PlanningResult
```

A pseudo-tool requiring user input may result in:

- an interactive planning session;
- a resumable planning state;
- a `PlanningError`;
- a higher-level “input required” result in a future extension.

This specification does not require a resumable interactive protocol.

## 10.4 Placeholder behaviour

If an `AgentPlanningStrategy` class or configuration value is introduced before the implementation exists, it must fail clearly:

```text
Planning strategy 'agent' is not implemented.
Use planning_strategy = "mechanical".
```

It must not silently fall back to mechanical planning after the user explicitly selects `agent`.

---

# 11. Frame Construction

Move conversion from planning-domain data to execution frames into a dedicated function or factory.

Suggested location:

```text
src/ticket_pipeline/planning/frame_factory.py
```

or, if preferred:

```text
src/ticket_pipeline/lib/frame_factory.py
```

Suggested API:

```python
def build_ticket_frames(
    *,
    ticket_id: str,
    ticket_content: str,
    planning_result: PlanningResult,
    strategy_override: str | None = None,
) -> list[CriterionFrame]:
    ...
```

Implementation:

```python
def build_ticket_frames(
    *,
    ticket_id: str,
    ticket_content: str,
    planning_result: PlanningResult,
    strategy_override: str | None = None,
) -> list[CriterionFrame]:
    return [
        CriterionFrame(
            ticket=ticket_id,
            criterion=item.criterion,
            plan_context=item.plan_context,
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification=item.verification,
            strategy=(
                strategy_override
                or item.implementation_strategy
            ),
            existing_test_refs=list(item.existing_test_refs),
            ticket_snapshot=ticket_content,
        )
        for item in planning_result.criteria
    ]
```

## 11.1 Important naming rule

The existing CLI option:

```text
--strategy
```

currently overrides the implementation strategy assigned to frames.

This option must retain that meaning.

Internally, rename ambiguous variables where practical:

```python
strategy_override
```

becomes:

```python
implementation_strategy_override
```

This can be an internal rename without changing the CLI.

---

# 12. Mechanical Grounding

Mechanical grounding must remain independent of the selected planning strategy.

Initial flow:

```python
result = planning_strategy.plan(request)

candidate_frames = build_ticket_frames(
    ticket_id=ticket_id,
    ticket_content=ticket_content,
    planning_result=result,
    strategy_override=implementation_strategy_override,
)

frames, newly_declined, skipped_count = filter_grounded_frames(
    candidate_frames
)
```

This ensures that:

- mechanical plans;
- agent-generated plans;
- future imported plans;

all pass through the same grounding checks.

## 12.1 Initial scope

For the first refactor, keep `filter_grounded_frames()` operating on `CriterionFrame`.

## 12.2 Future improvement

A later refactor may introduce:

```python
class CriterionGroundingPolicy:
    def filter(
        self,
        criteria: Sequence[PlannedCriterion],
    ) -> GroundingResult:
        ...
```

That is not required for this ticket.

---

# 13. Ticket Loading

Ticket loading must remain outside the planning strategy.

Extract or retain a helper with behaviour equivalent to:

```python
def load_ticket_content(
    ticket_id: str,
    ticket_file_in: Path | None,
) -> str:
    if ticket_file_in is not None:
        if not ticket_file_in.is_file():
            raise PipelineError(
                f"--ticket-file-in {ticket_file_in} not found."
            )
        return ticket_file_in.read_text(encoding="utf-8")

    return fetch_ticket_text(ticket_id)
```

Benefits:

1. Strategies do not depend on Linear.
2. Tests can pass ticket content directly.
3. Agent and mechanical implementations receive identical source material.
4. `ticket_snapshot` remains consistent with the content used for planning.
5. Future planning strategies can be tested without network access.

---

# 14. Revised `resolve_ticket_frames()`

The target responsibility of `resolve_ticket_frames()` is orchestration only.

Illustrative final shape:

```python
def resolve_ticket_frames(
    ticket_id: str,
    model: str,
    step_models: dict[str, str],
    ticket_file_in: Path | None,
    implementation_strategy_override: str | None = None,
    planning_strategy_name: str = "mechanical",
) -> list[lib.CriterionFrame]:
    ticket_content = load_ticket_content(
        ticket_id,
        ticket_file_in,
    )

    strategy = create_planning_strategy(
        planning_strategy_name,
    )

    request = PlanningRequest(
        ticket_id=ticket_id,
        ticket_content=ticket_content,
        project_root=Path.cwd(),
        model=model,
        step_models=step_models,
    )

    result = strategy.plan(request)

    for diagnostic in result.diagnostics:
        render_planning_diagnostic(diagnostic)

    if not result.criteria:
        render.print_line(
            f"-- {ticket_id}: no gap found. "
            "All acceptance criteria already satisfied."
        )
        return []

    candidate_frames = build_ticket_frames(
        ticket_id=ticket_id,
        ticket_content=ticket_content,
        planning_result=result,
        strategy_override=implementation_strategy_override,
    )

    frames, newly_declined, skipped_count = (
        lib.filter_grounded_frames(candidate_frames)
    )

    print_declined_criteria(newly_declined)

    if skipped_count:
        render.print_line(
            f"-- {ticket_id}: skipped {skipped_count} criteria "
            f"already in {lib.DECLINED_CRITERIA_FILE} "
            "(previously declined)."
        )

    return frames
```

The function must no longer:

1. know that mechanical planning contains a planner and narrower;
2. read `.gap-plan.md` directly;
3. parse criterion metadata;
4. construct frames inline.

---

# 15. Strategy Selection

## 15.1 Project configuration

Add a planning-specific configuration field:

```toml
planning_strategy = "mechanical"
```

Supported initial values:

```text
mechanical
agent
```

If the agent implementation is not included, `agent` should produce an explicit unsupported error.

## 15.2 User-level configuration

The same key may be supported in:

```text
~/.config/scaffold.toml
```

Precedence should follow the existing configuration conventions.

Recommended precedence:

```text
1. --planning-strategy CLI option
2. project .dev-pipeline.toml
3. user ~/.config/scaffold.toml
4. application default: mechanical
```

## 15.3 Command-line option

Add:

```text
--planning-strategy
```

Example:

```bash
push-ticket SA-453 --planning-strategy mechanical
```

Choices:

```text
mechanical
agent
```

Help text:

```text
Select how the ticket plan is generated.
'mechanical' runs the fixed plan-and-narrow pipeline.
'agent' uses the autonomous planning agent when available.
Default: configuration value or 'mechanical'.
```

## 15.4 Existing `--strategy`

Do not change:

```bash
--strategy tdd
--strategy direct
```

This continues to override the implementation strategy assigned to all seeded frames.

## 15.5 Configuration validation

Add `planning_strategy` to the allowed non-toolchain configuration keys.

Unknown planning strategies must fail with a message listing supported values.

---

# 16. Final Ticket Validation

`build_planning_blocks()` is currently reused during final ticket validation as well as initial planning.

The first implementation must not automatically replace the final validation flow with the selected planning strategy.

## 16.1 Required initial behaviour

```text
Initial ticket planning:
    selected PlanningStrategy

Final ticket validation:
    existing mechanical validation flow
```

Reasons:

1. Mechanical validation acts as an independent safety check.
2. The agent planner should not validate its own work through the same reasoning path.
3. Preserving current validation limits the refactor scope.
4. Existing retry, review, and missed-criterion behaviour remains unchanged.
5. The final validation gate has different semantics from initial planning.

## 16.2 Future abstraction

A later ticket may extract:

```python
class TicketGapEvaluator(Protocol):
    def evaluate(
        self,
        ticket_content: str,
        repository_context: RepositoryContext,
    ) -> GapEvaluation:
        ...
```

This specification does not require that abstraction.

---

# 17. Planning Artifacts

## 17.1 Mechanical strategy

The mechanical strategy must continue writing:

```text
.ticket.md
.tdd-plan.md
.gap-plan.md
```

These remain useful for:

- debugging;
- benchmarks;
- human inspection;
- compatibility with existing commands;
- current validation behaviour.

## 17.2 Agent strategy

An agent implementation may write:

```text
.ticket.md
.tdd-plan.md
.gap-plan.md
```

but is not required to use those files internally.

Recommended eventual behaviour:

```text
.ticket.md
    exact ticket snapshot

.tdd-plan.md
    human-readable agent planning summary

.gap-plan.md
    human-readable remaining-work summary
```

The authoritative output must still be `PlanningResult.criteria`.

## 17.3 Persistence boundary

Long term, artifact persistence should be separate from planning computation:

```python
result = strategy.plan(request)
artifact_writer.write(request, result)
```

This separation is desirable but not mandatory in the first extraction if it would introduce unnecessary risk.

---

# 18. Validation Rules

The planning boundary must validate the following before frame creation.

## 18.1 Criterion text

`criterion` must:

- be non-empty;
- contain meaningful text after trimming;
- not consist only of markdown list syntax.

## 18.2 Plan context

`plan_context` should be non-empty.

A mechanical result with missing plan context should preserve existing behaviour where possible, but the system should emit a warning.

## 18.3 Verification mode

Must be one of:

```text
test
test-refactor
refactor
manual
```

## 18.4 Implementation strategy

Must be one of:

```text
tdd
direct
manual
refactor
```

## 18.5 Existing test references

Each reference must be non-empty.

Validation of whether the referenced test exists remains part of later execution or grounding behaviour unless already enforced elsewhere.

## 18.6 Empty result

An empty criteria collection is valid and means:

```text
No remaining gap was found.
```

It must not be treated as a planning failure.

---

# 19. Error Handling

## 19.1 Invalid strategy name

Example:

```text
Unknown planning strategy 'autonomous-v2'.
Supported strategies: mechanical, agent.
```

## 19.2 Unsupported agent strategy

Example:

```text
Planning strategy 'agent' is not implemented in this version.
Use '--planning-strategy mechanical'.
```

## 19.3 Invalid planning result

Example:

```text
Planning strategy 'agent' returned criterion 2 with unsupported
verification mode 'integration-only'.
```

## 19.4 Mechanical planning failure

Existing user-facing errors should remain materially unchanged, including:

- invalid planner output;
- invalid narrower output;
- failed AI calls;
- retry exhaustion;
- missing prompt templates;
- failed file writes.

## 19.5 No silent fallback

If an explicitly selected strategy fails, the application must not silently rerun using another strategy.

---

# 20. Logging

Add strategy-level logging:

```text
-- Planning strategy: mechanical
```

or:

```text
-- Planning strategy: agent
```

Recommended diagnostic events:

```text
planning_strategy_selected
planning_started
planning_completed
planning_failed
```

Where the existing JSONL diagnostic log supports it, include:

```json
{
  "block": "planning",
  "strategy": "mechanical",
  "ticket": "SA-453",
  "status": "completed"
}
```

Adding a new `strategy` field to generic log events is optional if it would broaden the ticket unnecessarily.

Existing planner and narrower event names should remain intact for compatibility.

---

# 21. Testing Requirements

## 21.1 Planning model tests

Add tests for:

```text
PlanningRequest construction
PlanningResult construction
PlannedCriterion defaults
invalid verification rejection
invalid implementation-strategy rejection
```

## 21.2 Gap-plan parser tests

Test that `parse_gap_plan()`:

1. extracts all remaining acceptance criteria;
2. preserves criterion text;
3. extracts criterion-specific plan context;
4. parses `verify: test`;
5. parses `verify: test-refactor`;
6. parses `verify: refactor`;
7. parses `verify: manual`;
8. defaults verification to `test`;
9. parses explicit `strategy: direct`;
10. defaults strategy from verification mode;
11. parses repeated `existing_test:` tags;
12. returns an empty list when no criteria remain.

## 21.3 Frame-factory tests

Test that:

1. one frame is created per planned criterion;
2. ticket ID is copied;
3. ticket content is stored as `ticket_snapshot`;
4. `plan_context` is copied;
5. verification mode is copied;
6. implementation strategy is copied;
7. existing test references are copied;
8. status defaults to `pending`;
9. origin defaults to `ticket`;
10. test files and test names remain `None`;
11. CLI implementation-strategy override replaces per-criterion values;
12. planning-strategy selection does not affect `CriterionFrame.strategy`.

## 21.4 Mechanical strategy contract tests

Test that the mechanical strategy:

1. writes `.ticket.md`;
2. invokes the planner;
3. invokes the narrower;
4. writes `.tdd-plan.md`;
5. writes `.gap-plan.md`;
6. returns structured criteria;
7. preserves plan text in `PlanningResult.plan_text`;
8. preserves narrowed plan text in `PlanningResult.narrowed_plan_text`;
9. returns an empty criteria collection when all criteria are satisfied;
10. uses the configured plan model;
11. uses the configured narrow model;
12. propagates mechanical planning failures.

Use mocks or existing benchmark fixtures to avoid live model calls.

## 21.5 Strategy-selection tests

Test precedence:

1. CLI value overrides project configuration.
2. Project configuration overrides user configuration.
3. User configuration overrides the application default.
4. Default is `mechanical`.
5. Invalid names fail.
6. Explicit unsupported `agent` selection fails clearly.
7. Existing `--strategy` remains independent.

## 21.6 Integration tests

Add or update an integration-style test that verifies:

```text
ticket content
    ↓
fake PlanningStrategy
    ↓
PlanningResult
    ↓
CriterionFrame construction
    ↓
mechanical grounding
    ↓
returned frames
```

The fake strategy must not write `.gap-plan.md`.

This test proves that downstream planning no longer depends on mechanical artifacts.

## 21.7 Regression tests

Existing tests covering:

- `push-ticket`;
- `--ticket-file-in`;
- `--from-gap-plan`;
- `--validate-only`;
- `--force`;
- `--prepend`;
- `--explore`;
- `--strategy`;
- grounding;
- stack persistence;

must continue to pass.

---

# 22. Special CLI Paths

## 22.1 `--from-gap-plan`

This path bypasses the configured planning strategy.

It should:

1. read the existing `.gap-plan.md`;
2. parse it with the mechanical gap-plan adapter;
3. construct a `PlanningResult`;
4. continue through shared frame creation and grounding.

Suggested helper:

```python
def planning_result_from_gap_plan(
    gap_plan_text: str,
) -> PlanningResult:
    return PlanningResult(
        criteria=tuple(parse_gap_plan(gap_plan_text)),
        plan_text=None,
        narrowed_plan_text=gap_plan_text,
    )
```

The selected `planning_strategy` should not be invoked.

## 22.2 `--validate-only`

This path does not perform initial planning.

The selected planning strategy should not be instantiated or invoked.

## 22.3 `--explore`

The existing exploration step currently enriches frame `plan_context` after plan and narrow.

Preserve this order:

```text
selected planning strategy
    ↓
PlanningResult
    ↓
CriterionFrame construction
    ↓
optional per-criterion exploration
    ↓
grounding or stack persistence according to current behaviour
```

If current behaviour grounds before exploration, preserve the current order unless separately approved.

The exploration feature must work with criteria from any planning strategy.

It must not assume `.gap-plan.md` is the authoritative source once a `PlanningResult` exists.

## 22.4 `--ticket-file-in`

Ticket content must be loaded before invoking the planning strategy.

Every planning strategy receives the local file content through `PlanningRequest.ticket_content`.

---

# 23. Migration Plan

## Phase 1: Introduce planning-domain models

Add:

```text
PlanningRequest
PlanningResult
PlannedCriterion
PlanningDiagnostic
PlanningStrategy
PlanningError
```

No runtime behaviour changes.

## Phase 2: Extract gap-plan parsing

Move the existing criterion extraction and metadata parsing composition into:

```text
parse_gap_plan()
```

Keep the lower-level parsing helpers in their current module initially if moving them would cause excessive churn.

Update current callers to use the new parser.

## Phase 3: Extract frame creation

Move inline `CriterionFrame` construction from `resolve_ticket_frames()` into a shared frame factory.

Add focused unit tests.

## Phase 4: Introduce `MechanicalPlanningStrategy`

Wrap the current:

```text
build_planning_blocks()
walk()
PLAN_FILE
GAP_PLAN_FILE
```

flow.

Confirm identical outputs for existing fixtures.

## Phase 5: Refactor `resolve_ticket_frames()`

Change the function to:

1. load ticket content;
2. construct `PlanningRequest`;
3. invoke a `PlanningStrategy`;
4. build frames;
5. apply grounding;
6. return frames.

## Phase 6: Add configuration and factory

Add:

```text
planning_strategy = "mechanical"
--planning-strategy mechanical
```

Keep mechanical as the default.

## Phase 7: Adapt special modes

Ensure:

```text
--from-gap-plan
--validate-only
--explore
```

continue to behave correctly.

## Phase 8: Add agent placeholder or implementation

Either:

- add a placeholder that fails explicitly; or
- implement `AgentPlanningStrategy` in a follow-up ticket.

## Phase 9: Optional cleanup

After regression confidence:

- move more planning-specific functions from `pipeline_lib.py`;
- separate artifact writing from strategy computation;
- replace internal `die()` calls with `PlanningError`;
- reduce dependence on global file constants.

These cleanup steps are not required for acceptance unless explicitly included.

---

# 24. Backward Compatibility

## Required compatibility

1. Existing commands without new options use mechanical planning.
2. Existing `.dev-pipeline.toml` files continue to work.
3. Existing user configuration continues to work.
4. Existing criteria-stack JSON remains readable.
5. Existing `CriterionFrame.strategy` values remain unchanged.
6. Existing `.gap-plan.md` files remain usable with `--from-gap-plan`.
7. Existing planner and narrower prompts remain valid.
8. Existing benchmark harnesses remain operational.
9. Existing final-validation behaviour remains mechanical.
10. Existing stack resumption remains unaffected.

## New configuration default

Absence of `planning_strategy` is equivalent to:

```toml
planning_strategy = "mechanical"
```

No migration action should be required from users.

---

# 25. Acceptance Criteria

## Architecture

- [ ] A `PlanningStrategy` interface exists.
- [ ] A structured `PlanningRequest` exists.
- [ ] A structured `PlanningResult` exists.
- [ ] A structured `PlannedCriterion` exists.
- [ ] Planning-domain models do not contain criteria-stack execution state.
- [ ] The existing planning flow is implemented through `MechanicalPlanningStrategy`.
- [ ] `resolve_ticket_frames()` no longer directly runs planner and narrower blocks.
- [ ] `resolve_ticket_frames()` no longer directly parses `.gap-plan.md`.
- [ ] `resolve_ticket_frames()` no longer constructs `CriterionFrame` values inline.

## Behaviour

- [ ] Mechanical planning remains the default.
- [ ] The default workflow still generates `.ticket.md`.
- [ ] The default workflow still generates `.tdd-plan.md`.
- [ ] The default workflow still generates `.gap-plan.md`.
- [ ] Existing planner and narrower model overrides still work.
- [ ] Existing retry and logging behaviour remains materially unchanged.
- [ ] Empty planning results are treated as “no remaining gap.”
- [ ] All planning implementations pass through the same mechanical grounding checks.
- [ ] Final ticket validation remains mechanical.
- [ ] No explicitly selected planning strategy silently falls back to another.

## Configuration

- [ ] `.dev-pipeline.toml` supports `planning_strategy`.
- [ ] User configuration may support `planning_strategy`.
- [ ] `push-ticket` supports `--planning-strategy`.
- [ ] CLI configuration has the highest precedence.
- [ ] Invalid planning-strategy names fail clearly.
- [ ] Existing `--strategy` continues to control criterion implementation strategy.

## Compatibility

- [ ] Existing stack files remain compatible.
- [ ] Existing `--from-gap-plan` behaviour remains supported.
- [ ] Existing `--validate-only` behaviour remains supported.
- [ ] Existing `--ticket-file-in` behaviour remains supported.
- [ ] Existing `--explore` behaviour remains supported.
- [ ] Existing `--force` and `--prepend` behaviour remains supported.

## Tests

- [ ] Unit tests cover planning models.
- [ ] Unit tests cover gap-plan parsing.
- [ ] Unit tests cover frame construction.
- [ ] Unit tests cover strategy selection.
- [ ] Mechanical-strategy contract tests exist.
- [ ] An integration test proves a fake strategy can create frames without writing `.gap-plan.md`.
- [ ] Existing test suites pass.

---

# 26. Suggested Follow-up Ticket: Agent Planner

After this refactor is complete, create a separate ticket for `AgentPlanningStrategy`.

Suggested scope:

1. Define the agent planning system prompt.
2. Define allowed repository tools.
3. Define pseudo-tools:
   - `ask_user_input`;
   - `planning_complete`;
   - `planning_failed`.
4. Define structured completion schema.
5. Decide whether user-input requests are:
   - interactive;
   - fail-fast;
   - resumable.
6. Add agent-specific retry policy.
7. Add agent planning diagnostics.
8. Add agent planning benchmarks.
9. Compare agent plans with mechanical plans on a fixed ticket corpus.
10. Keep the mechanical final-validation gate as an independent check.

Suggested future configuration:

```toml
planning_strategy = "agent"

[step_models]
planning_agent = "opencode:claude-sonnet-4-6"
```

---

# 27. Design Rationale

The main design principle is that a planning strategy should be defined by the semantic result it produces, not by its internal sequence or artifacts.

The current implementation happens to use:

```text
planner markdown
    ↓
narrower markdown
    ↓
HTML metadata tags
    ↓
deterministic parsing
```

That is an implementation detail of the mechanical strategy.

The agent implementation may instead use:

```text
repository exploration
    ↓
iterative reasoning
    ↓
clarification
    ↓
self-review
    ↓
structured output
```

Both should converge on:

```text
PlanningResult
    └── PlannedCriterion[]
```

This boundary keeps the criteria-stack pipeline stable and allows planning approaches to evolve independently.

The resulting architecture should be:

```text
                    ┌────────────────────────────┐
                    │ MechanicalPlanningStrategy │
                    │                            │
Ticket + repo ─────▶│ plan → narrow → parse      │
                    └──────────────┬─────────────┘
                                   │
                                   ▼
                         PlanningResult
                                   ▲
                                   │
                    ┌──────────────┴─────────────┐
                    │ AgentPlanningStrategy      │
Ticket + repo ─────▶│                            │
                    │ explore → clarify → revise │
                    └────────────────────────────┘
                                   │
                                   ▼
                     CriterionFrame factory
                                   │
                                   ▼
                    Mechanical grounding checks
                                   │
                                   ▼
                       Existing criteria stack
```

This preserves the current reliable workflow while creating a narrow, explicit, testable extension point for future planning implementations.
