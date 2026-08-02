# Remove implicit TDD assumptions from shared planning and execution infrastructure

## Summary

Audit and refactor Scaffold so that Test-Driven Development is treated as one explicit implementation strategy rather than the pipeline’s implicit default methodology.

Scaffold was originally designed around a TDD workflow. Although the repository now supports multiple planning and implementation strategies, shared models, prompts, artifact names, parsing behavior, and orchestration may still assume that criteria will be implemented by first writing a failing test.

The pipeline must become methodology-independent at its shared layers:

- planning must explicitly select an implementation strategy;
- verification must remain independent from implementation methodology;
- shared orchestration must dispatch to the selected strategy without requiring TDD-specific state;
- acceptance must judge the produced outcome rather than whether a TDD process was followed;
- TDD-specific behavior must remain contained within the TDD strategy.

## Problem

The current implementation contains several remaining TDD assumptions.

Examples include:

- `PlannedCriterion` defaults `verification` to `test`;
- `PlannedCriterion` defaults `implementation_strategy` to `tdd`;
- missing strategy metadata may therefore silently select TDD;
- `narrow-plan.prompt.md` describes its input as a TDD plan and assumes unresolved behavior should normally be addressed by writing a failing test;
- legacy artifacts use names such as `.tdd-plan.md`;
- planning guidance describes automated tests as the normal verification path and direct implementation primarily as an exception;
- verification concepts such as `test-refactor` and `refactor` overlap with implementation strategy concepts;
- shared parsing and orchestration may contain fallback behavior that resolves missing metadata to `test` or `tdd`;
- shared state or validation logic may still expect test files, red tests, or TDD-specific statuses.

This creates several risks:

1. New strategies can appear supported while still being forced through TDD-shaped assumptions.
2. Planner omissions can silently become TDD decisions.
3. A criterion verified by tests may be incorrectly assumed to require test-first implementation.
4. Direct or refactor strategies may depend on TDD-specific state or artifacts.
5. Benchmark comparisons may measure conformity to the legacy TDD flow rather than correctness of the resulting outcome.

## Goal

Make Scaffold’s shared planning, orchestration, parsing, state, and acceptance layers independent of any particular implementation methodology.

The intended architecture is:

```text
Planning explicitly selects a strategy
                ↓
Shared orchestration dispatches that strategy
                ↓
The selected strategy owns its process and internal state
                ↓
Independent acceptance checks judge the resulting outcome
```

TDD remains a fully supported strategy, but only TDD-owned code should require concepts such as:

- failing tests;
- red/green phases;
- `test-written`;
- `green-unconfirmed`;
- test-first implementation;
- generated test references.

## Non-goals

- Removing the TDD strategy.
- Reducing the quality or rigor of TDD execution.
- Removing automated tests as an acceptance mechanism.
- Requiring all implementation strategies to behave identically.
- Renaming every historical TDD-related test or fixture where the reference is legitimately strategy-specific.
- Redesigning the complete benchmark framework as part of this ticket.
- Replacing all current file formats with a new persistence system.

## Architectural principles

### Explicit strategy selection

Every executable criterion must declare an implementation strategy.

A missing implementation strategy is invalid input and must not default silently to `tdd`.

### Verification and methodology are independent

Verification describes how Scaffold determines whether an outcome is acceptable.

Implementation strategy describes how the change is produced.

For example, this must be valid:

```text
verification: automated test
implementation strategy: direct
```

The existence of an automated acceptance test must not imply that Scaffold should generate a failing test before implementation.

### Strategy-local state

TDD-specific phases belong inside the TDD strategy.

Shared orchestration must not require every strategy to understand statuses such as:

```text
test-written
green-unconfirmed
nothing-written
```

### Outcome-based acceptance

A strategy succeeds because its result passes independent acceptance checks, not because it followed a preferred methodology or declared itself successful.

### No inferred TDD behavior

Shared code must not infer `tdd` from:

- a missing field;
- a test-based verification mode;
- a criterion that changes behavior;
- a legacy artifact name;
- the presence or absence of test references;
- historical pipeline conventions.

## Scope

### Planning models

Audit and update:

```text
src/ticket_pipeline/planning/models.py
```

Remove implicit defaults from `PlannedCriterion`.

The effective model should require explicit values:

```python
@dataclass(frozen=True)
class PlannedCriterion:
    criterion: str
    plan_context: str
    verification: str
    implementation_strategy: str
    existing_test_refs: tuple[str, ...] = field(default_factory=tuple)
```

A transitional nullable representation is acceptable only if unresolved values are rejected before a criterion enters execution.

### Planning parsers

Audit:

```text
src/ticket_pipeline/planning/parsing.py
src/ticket_pipeline/lib/pipeline_lib.py
```

In particular, inspect:

```text
extract_verification_mode
extract_strategy
extract_existing_test_refs
parse_gap_plan
planning_result_from_gap_plan
```

Missing or invalid strategy metadata must produce a clear planning or parsing error.

The parser must not use `test` or `tdd` as an implicit fallback.

### Planning prompts

Audit and revise all shared planning prompts, including:

```text
src/ticket_pipeline/prompts/agent-plan.prompt.md
src/ticket_pipeline/prompts/narrow-plan.prompt.md
```

The prompts must:

1. determine the required outcome;
2. determine how the outcome can be independently accepted;
3. determine which implementation strategies are valid;
4. explicitly select the most appropriate implementation strategy.

Prompts must state that test-based verification does not require TDD implementation.

The narrower must be reframed as a repository-state or gap assessor rather than a TDD-plan narrower.

### Artifact naming

Audit references to:

```text
.tdd-plan.md
TDD plan
tdd plan
```

Introduce methodology-neutral terminology for shared artifacts.

Preferred naming:

```text
.implementation-plan.md
```

or:

```text
.criteria-plan.md
```

Backward compatibility may be retained by reading `.tdd-plan.md` as a legacy fallback and emitting a deprecation warning.

The final neutral name should be chosen consistently across:

- constants;
- prompts;
- error messages;
- CLI output;
- documentation;
- fixtures;
- tests.

### Shared orchestration

Audit:

```text
src/ticket_pipeline/push_ticket.py
src/ticket_pipeline/next_step.py
src/ticket_pipeline/lib/implement.py
src/ticket_pipeline/lib/pipeline_lib.py
src/ticket_pipeline/status.py
```

Identify shared assumptions involving:

```text
test-written
red
green
failing test
test files
test names
existing test references
write test before implementation
```

Move any strategy-specific behavior into the relevant strategy module.

The shared orchestration layer should be responsible for:

- loading the criterion;
- validating its declared strategy;
- resolving the strategy handler;
- invoking the handler;
- recording generic lifecycle state;
- invoking independent acceptance checks;
- recording accepted, rejected, blocked, or failed outcomes.

### Strategy handlers

Confirm that methodology-specific behavior remains contained in:

```text
src/ticket_pipeline/strategies/tdd.py
src/ticket_pipeline/strategies/direct.py
src/ticket_pipeline/strategies/manual.py
src/ticket_pipeline/strategies/refactor.py
```

TDD-specific states and red/green behavior may remain in `tdd.py`.

Direct, manual, and refactor strategies must not be required to create or interpret TDD-specific state.

### Documentation and user-facing terminology

Audit the README, CLI help, log messages, comments, and error messages for language that describes Scaffold itself as necessarily TDD-driven.

Scaffold may describe TDD as a supported strategy, but shared documentation should describe the tool as criteria-driven or outcome-driven.

## Proposed verification model

As part of the audit, clarify the distinction between verification and implementation strategy.

The exact final schema may remain compatible with the current model, but shared code and prompts must treat them as separate axes.

Conceptually:

```text
Verification:
- automated test
- build
- lint or static analysis
- repository-state check
- manual inspection
- composite checks

Implementation strategy:
- tdd
- direct
- refactor
- manual
```

It is acceptable to retain current verification values temporarily, provided the implementation does not infer a strategy from them.

A larger redesign of acceptance-check schemas may be handled in a later ticket.

## Acceptance criteria

- [ ] `PlannedCriterion` no longer silently defaults `verification` to `test` or `implementation_strategy` to `tdd`.

- [ ] Creating or parsing an executable criterion without an explicit implementation strategy fails with a clear error.

- [ ] Shared parsing functions do not fall back to `tdd` when strategy metadata is missing or malformed.

- [ ] Shared planning prompts explicitly separate verification selection from implementation-strategy selection.

- [ ] Shared planning prompts state that automated test verification does not imply TDD implementation.

- [ ] `narrow-plan.prompt.md` no longer describes every input as a TDD plan or assumes unresolved criteria must be addressed by writing a failing test.

- [ ] Planning prompts can legitimately select `direct` for a behavior-changing criterion that will be verified by automated tests.

- [ ] Planning prompts can legitimately select `refactor` for behavior-preserving structural work covered by existing tests.

- [ ] Planning prompts can legitimately select `manual` when implementation or verification requires human action.

- [ ] Shared orchestration dispatches criteria based only on their explicit implementation strategy.

- [ ] Direct-strategy execution does not require test files, test names, a red test, or a `test-written` state.

- [ ] Refactor-strategy execution does not require creation of a failing test.

- [ ] Manual-strategy execution does not invoke AI test generation or AI implementation unless explicitly defined by that strategy.

- [ ] TDD-specific lifecycle states are not required or interpreted by non-TDD strategies.

- [ ] Shared acceptance logic judges the resulting outcome independently of whether TDD was used.

- [ ] A criterion using automated-test verification with `strategy: direct` can execute and be accepted without entering the TDD flow.

- [ ] Missing strategy metadata is rejected rather than silently routed to TDD.

- [ ] Shared artifact naming and user-facing language no longer present TDD as the required methodology.

- [ ] Existing `.tdd-plan.md` artifacts remain readable during a documented compatibility period, or a migration path is provided.

- [ ] Existing TDD behavior continues to function through the explicitly selected `tdd` strategy.

- [ ] The full automated test suite passes.

## Required tests

Add focused tests covering at least the following cases.

```python
def test_planned_criterion_requires_explicit_verification():
    ...
```

```python
def test_planned_criterion_requires_explicit_implementation_strategy():
    ...
```

```python
def test_parser_rejects_missing_strategy_instead_of_defaulting_to_tdd():
    ...
```

```python
def test_parser_rejects_missing_verification_instead_of_defaulting_to_test():
    ...
```

```python
def test_test_verification_can_use_direct_implementation():
    ...
```

```python
def test_direct_strategy_does_not_require_test_references():
    ...
```

```python
def test_direct_strategy_never_enters_test_written_state():
    ...
```

```python
def test_refactor_strategy_does_not_generate_a_failing_test():
    ...
```

```python
def test_manual_strategy_does_not_invoke_ai_implementation():
    ...
```

```python
def test_tdd_strategy_retains_red_green_execution():
    ...
```

```python
def test_legacy_tdd_plan_filename_is_supported_during_migration():
    ...
```

Add at least one end-to-end strategy-dispatch test with this shape:

```text
criterion:
  changes observable behavior

verification:
  automated test

implementation strategy:
  direct
```

The test must prove that Scaffold:

1. does not invoke test generation;
2. does not require a newly failing test;
3. invokes the direct implementation strategy;
4. runs the configured acceptance checks afterward;
5. accepts or rejects the resulting outcome independently.

## Audit method

Search the repository for the following terms:

```text
tdd
TDD
.tdd-plan
red
green
test-written
green-unconfirmed
nothing-written
failing test
write a test
test first
strategy: tdd
verification = "test"
implementation_strategy = "tdd"
```

Classify each result as one of:

```text
strategy-local
shared-code coupling
prompt bias
legacy artifact naming
documentation
test fixture
historical comment
```

Do not remove legitimate TDD references from TDD-specific code, prompts, fixtures, or tests.

The audit should produce a short summary in the pull request description listing:

- shared assumptions removed;
- TDD-specific behavior intentionally retained;
- legacy compatibility retained;
- follow-up work deferred.

## Suggested implementation sequence

### Phase 1: Audit and tests

- Catalogue TDD references.
- Add failing independence tests.
- Identify parser and model defaults.
- Identify shared state-machine assumptions.

### Phase 2: Explicit planning metadata

- Remove implicit defaults.
- Add validation errors.
- Update parsers and fixtures.
- Update planning result construction.

### Phase 3: Prompt independence

- Rewrite the narrower prompt.
- Adjust the agent planner prompt.
- Separate verification from strategy guidance.
- Update examples to show multiple valid combinations.

### Phase 4: Orchestration independence

- Move TDD-specific state handling into the TDD strategy.
- Remove shared requirements for test files and red tests.
- Validate direct, refactor, and manual paths independently.

### Phase 5: Artifact and terminology migration

- Introduce the neutral plan filename.
- Add legacy fallback behavior.
- Update CLI output, documentation, tests, and fixtures.
- Document deprecation behavior.

## Definition of done

This work is complete when Scaffold can execute a criterion using each supported implementation strategy without shared infrastructure assuming a TDD lifecycle, while the TDD strategy continues to provide its existing test-first behavior when explicitly selected.

The clearest proof is a passing end-to-end case where:

```text
verification = automated test
implementation strategy = direct
```

and the criterion is implemented, independently verified, and accepted without generating or requiring a new failing test.
