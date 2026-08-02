# Scaffold Strategy Benchmarking Specification

**Status:** Proposed  
**Project:** `jared-two-foxes/scaffold`  
**Purpose:** Define a reproducible benchmark framework for comparing Scaffold planning and implementation strategies, with independently verified accepted outcomes as the primary measure of success.

---

## 1. Executive Summary

Scaffold supports multiple strategies across two distinct categories:

- **Planning strategies**, currently including `mechanical` and `agent`.
- **Implementation strategies**, currently including `tdd`, `direct`, `manual`, and `refactor`.

The benchmark system must compare strategies within their category while preventing upstream strategy quality from contaminating downstream results. A planning strategy should be evaluated against fixed repository and ticket fixtures. An implementation strategy should receive a fixed, accepted planning artifact or criterion frame.

The benchmark must not treat completion, low cost, or low latency as evidence of success. The primary result of every trial is whether the strategy produced an independently accepted outcome.

Each trial must therefore resolve to one of:

- `ACCEPTED`
- `REJECTED`
- `INDETERMINATE`

Efficiency metrics such as cost, duration, token usage, retries, and tool calls are secondary. They are most meaningful when reported per accepted outcome.

The benchmark framework should ultimately expose three suites:

1. **Planning strategy benchmark** — compares the quality of generated plans and criterion stacks.
2. **Implementation strategy benchmark** — compares the ability to produce correct code from identical accepted inputs.
3. **End-to-end pipeline benchmark** — compares complete planning and implementation configurations in realistic execution.

---

## 2. Goals

The framework must answer the following questions.

### 2.1 Planning

- Does the strategy produce a valid planning artifact?
- Does the plan cover all required ticket outcomes?
- Is the plan grounded in the actual repository state?
- Does it avoid invented, obsolete, or unnecessary work?
- Does it select appropriate criterion strategies and verification methods?
- Can the resulting plan successfully guide a downstream implementation?
- How much time and money does the strategy require to produce an accepted plan?

### 2.2 Implementation

- Does the strategy produce code that satisfies the criterion?
- Does the repository compile after the change?
- Do visible and hidden acceptance tests pass?
- Are regressions avoided?
- Does the implementation remain within appropriate scope?
- Does the strategy weaken, disable, or overfit tests?
- How much time and money does the strategy require to produce an accepted implementation?

### 2.3 General

- Are results reproducible against a pinned repository state?
- Can failures be attributed to a specific stage?
- Can strategy quality be compared independently from model quality?
- Can benchmark results be inspected and regraded later?

---

## 3. Non-Goals

The initial benchmark framework will not:

- Produce a single universal score across planning and implementation categories.
- Treat agent self-reported completion as acceptance.
- Rank `manual` directly against autonomous strategies solely by speed or cost.
- Assume that passing visible tests proves correctness.
- Allow planning output from the strategy under test to feed an isolated implementation benchmark.
- Allow implementation quality to influence the intrinsic grading of a planning artifact unless running the dedicated downstream-success evaluation.
- Guarantee that every planning decision can be mechanically graded without human review.

---

## 4. Benchmark Principles

### 4.1 Outcome before efficiency

A strategy that is fast or cheap but usually produces invalid outputs is not effective.

The primary metric is:

```text
accepted_outcome_rate = accepted_trials / total_trials
```

The primary efficiency metrics are:

```text
cost_per_accepted_outcome = total_trial_cost / accepted_trials

time_per_accepted_outcome = total_trial_duration / accepted_trials
```

Average cost or duration per attempt may still be reported, but must not be used as the headline measure.

### 4.2 Independent acceptance

The strategy being benchmarked must not decide whether its own output is correct.

Acceptance must be established by one or more independent mechanisms:

- Structured schema validation.
- Repository-state validation.
- Compilation and test execution.
- Hidden acceptance tests.
- Deterministic fixture-specific graders.
- Blinded human review.
- A fixed downstream executor, where appropriate.

### 4.3 Isolation between categories

Planning and implementation benchmarks must be independent.

- Planning trials receive a fixed ticket and repository state.
- Implementation trials receive a fixed, accepted criterion frame or planning artifact.
- End-to-end trials are explicitly separate and may combine both strategy categories.

### 4.4 Reproducibility

Every fixture must pin the repository commit against which it was authored and validated.

Every trial must record:

- Scaffold commit.
- Target repository commit.
- Fixture version.
- Strategy.
- Model and model settings.
- Tool permissions.
- Environment configuration.
- Trial seed, where supported.

### 4.5 Comparable conditions

A strategy comparison should hold the following constant:

- Model.
- Model parameters.
- Ticket fixture.
- Repository commit.
- Tool access.
- User-input policy.
- Trial count.
- Concurrency constraints.

Model comparison is a separate experimental dimension and should be stratified in reports.

---

## 5. Benchmark Categories

## 5.1 Planning Strategies

Initial strategies:

- `mechanical`
- `agent`

Planning benchmarks should invoke the real planning strategy abstraction used by the application rather than legacy block-specific wrappers.

The planning benchmark evaluates two related concepts:

### Artifact acceptance

Whether the produced plan or criterion stack is itself valid, complete, repository-grounded, and executable.

### Downstream outcome acceptance

Whether a fixed downstream executor can use the plan to produce a correct implementation.

Artifact acceptance is the primary isolated planning result. Downstream success is a stronger secondary signal and must be reported separately.

## 5.2 Implementation Strategies

Current implementation strategies:

- `tdd`
- `direct`
- `manual`
- `refactor`

These should be divided into comparison cohorts.

### Autonomous cohort

- `tdd`
- `direct`

These can be compared directly on suitable fixtures.

### Human-control cohort

- `manual`

This is a control or operational baseline. It should not be ranked against autonomous strategies solely by runtime or cost.

### Specialized cohort

- `refactor`

This should be benchmarked only against fixtures whose task shape genuinely requires refactoring or test refactoring.

---

## 6. Trial Outcomes

Every benchmark trial must produce one of the following verdicts.

### 6.1 `ACCEPTED`

The result satisfies all mandatory acceptance gates for that fixture and category.

### 6.2 `REJECTED`

The result clearly violates at least one mandatory acceptance gate.

Examples:

- Invalid schema.
- Missing required outcome.
- Forbidden repository change.
- Compilation failure.
- Hidden test failure.
- Regression.
- Duplicate or architecturally incorrect implementation.

### 6.3 `INDETERMINATE`

The automated grader cannot determine correctness with sufficient confidence.

Examples:

- Semantically plausible planning output that does not map cleanly to deterministic rules.
- Repository architecture judgment requiring human interpretation.
- Conflicting automated signals.
- Partial evidence where neither acceptance nor rejection is justified.

Indeterminate results must not be counted as accepted. They should be reported separately and optionally sent for blinded human review.

---

## 7. Planning Acceptance Contract

A planning trial is accepted only when all mandatory planning gates pass.

```text
planning_accepted =
    schema_valid
    AND required_outcomes_covered
    AND repository_grounded
    AND executable
    AND no_critical_false_work
```

## 7.1 Schema validity

The output must:

- Parse successfully.
- Contain one or more valid criteria when work remains.
- Use supported strategy names.
- Include required planning fields.
- Contain valid dependencies and references.
- Represent an already-satisfied ticket correctly when no work remains.

## 7.2 Required outcome coverage

Each fixture defines required observable outcomes.

Example:

```json
{
  "required_outcomes": [
    {
      "id": "secret-loaded",
      "description": "POSTMARK_SIGNING_SECRET is loaded into EmailConfig"
    },
    {
      "id": "secret-redacted",
      "description": "EmailConfig Debug output does not expose the secret"
    }
  ]
}
```

The generated plan need not use identical wording, but its criteria must semantically cover each required outcome.

Metrics:

- Required-outcome recall.
- Required-outcome precision.
- Critical omission count.

## 7.3 Repository grounding

The plan must agree with the pinned target repository.

Validation should include:

- Referenced existing files exist.
- Referenced existing symbols exist.
- New files are proposed only when expected or architecturally valid.
- Existing patterns and placement are respected.
- Already-implemented behavior is recognized.
- Stale or misleading ticket details are not blindly repeated.

## 7.4 Executability

The plan must provide sufficient information for execution.

It should identify, where applicable:

- The behavior to change.
- The expected observable result.
- Relevant files or symbols.
- Verification method.
- Dependencies between criteria.
- Appropriate implementation strategy.

An artifact may be schema-valid but rejected as non-executable if it is too vague to guide implementation without inventing material decisions.

## 7.5 Critical false work

Some false positives are severe enough to reject the plan immediately.

Examples:

- Creating duplicate domain types or configuration structures.
- Modifying the wrong subsystem.
- Reimplementing already-satisfied behavior.
- Proposing incompatible API changes without requirement.
- Selecting an inappropriate verification strategy for critical externally observable behavior.
- Omitting a security-sensitive adjacent obligation.

Minor unnecessary detail should reduce precision but does not necessarily require rejection. Each fixture should distinguish critical and non-critical false work.

## 7.6 Strategy classification

Where the planning artifact assigns criterion strategies, the benchmark should record whether the classification is appropriate.

Possible values include:

- `tdd`
- `direct`
- `manual`
- `refactor`

Incorrect classification may be either:

- A quality penalty.
- A mandatory rejection gate for high-risk fixtures.

The fixture defines which behavior applies.

## 7.7 Downstream plan success

A separate planning evaluation may run:

```text
planning strategy
    -> generated plan
    -> fixed downstream executor
    -> hidden acceptance suite
```

This produces a distinct result:

- `artifact_acceptance`
- `downstream_outcome_acceptance`

The two results must not be collapsed into one unexplained pass or failure.

---

## 8. Implementation Acceptance Contract

An implementation trial is accepted only when all mandatory implementation gates pass.

```text
implementation_accepted =
    build_passes
    AND required_tests_pass
    AND hidden_tests_pass
    AND no_regressions
    AND repository_invariants_hold
```

## 8.1 Build gate

The repository must compile or build using the fixture-defined command.

A compilation failure is a rejection, not an indeterminate result.

## 8.2 Required visible tests

Fixture-defined scoped tests must pass.

For TDD end-to-end trials, the framework must also confirm that generated tests were initially red for the intended reason.

## 8.3 Hidden acceptance tests

Hidden tests must verify the ticket outcome independently of tests created or observed by the implementation strategy.

This protects against:

- Visible-test overfitting.
- Weak generated tests.
- Assertions that do not exercise required behavior.
- Implementations that satisfy only a narrow example.

## 8.4 Regression gate

The fixture-defined regression suite must pass.

Depending on case size, this may be:

- A scoped module suite.
- A package suite.
- The full repository suite.

## 8.5 Repository invariants

Fixtures may define additional invariants:

- Allowed and forbidden changed files.
- Forbidden new dependencies.
- API compatibility.
- No disabled, deleted, or weakened tests.
- Formatting and lint requirements.
- No secret or credential leakage.
- No modification of generated or vendor files.

## 8.6 Test integrity

The benchmark should detect attempts or accidental changes that undermine the oracle.

Examples:

- Deleting a failing test.
- Adding ignore annotations.
- Weakening assertions.
- Changing fixture data to make a test pass.
- Replacing meaningful assertions with tautologies.

A test-integrity violation is a critical rejection.

---

## 9. Implementation Benchmark Modes

## 9.1 Fixed-red implementation benchmark

This is the primary fair comparison between `tdd` and `direct`.

Each strategy receives:

- The same accepted criterion.
- The same plan context.
- The same pinned repository state.
- A fixed, known-good failing test or hidden oracle.

The benchmark begins at the implementation stage.

```text
fixed accepted criterion
    -> implementation strategy
    -> build and acceptance grading
```

This isolates implementation quality from test-authoring quality.

## 9.2 End-to-end TDD benchmark

This evaluates the full TDD flow:

```text
write test
    -> confirm compilation
    -> confirm valid red state
    -> implement
    -> confirm green state
    -> run hidden acceptance suite
```

Failures must identify the stage:

- Test generation failed.
- Test did not compile.
- Test was false green.
- Test was red for an irrelevant reason.
- Implementation failed despite a valid test.
- Hidden acceptance failed after visible tests passed.

## 9.3 Direct implementation benchmark

The direct strategy receives the accepted criterion and plan context without requiring it to author a visible test first.

It is graded against the same hidden acceptance contract.

## 9.4 Refactor benchmark

Refactor fixtures should define preservation requirements:

- Behavior before and after must remain equivalent.
- Existing tests must continue to pass.
- Structural requirements must be satisfied.
- Public API compatibility must be preserved where required.

## 9.5 Manual control benchmark

Manual mode may record:

- Human elapsed time.
- Number of interventions.
- Acceptance outcome.
- Diff size.
- Review burden.

It should be used as an operational baseline rather than automatically ranked alongside autonomous strategies.

---

## 10. Fixture Design

Fixtures should be divided by category and suite.

```text
fixtures/benchmarks/
  planning/
    core/
      <case>/
  implementation/
    fixed-red/
      <case>/
    end-to-end-tdd/
      <case>/
    refactor/
      <case>/
```

## 10.1 Planning fixture structure

```text
fixtures/benchmarks/planning/core/<case>/
  fixture.json
  ticket.md
  expected.json
  reviewer-notes.md
```

`reviewer-notes.md` is optional and must not be exposed to the strategy under test.

Example `fixture.json`:

```json
{
  "fixture_version": 1,
  "category": "planning",
  "suite": "core",
  "case": "secret-debug-redaction",
  "target_repo": "jared-two-foxes/VirtualAssistant",
  "base_ref": "<commit-sha>",
  "case_type": "hidden-adjacent-obligation"
}
```

Example `expected.json`:

```json
{
  "required_outcomes": [
    {
      "id": "field-added",
      "critical": true,
      "description": "The configuration field is added and loaded"
    },
    {
      "id": "debug-redaction",
      "critical": true,
      "description": "Debug output redacts the secret"
    }
  ],
  "required_existing_paths": [
    "libs/example/src/email_config.rs"
  ],
  "forbidden_paths": [
    "libs/example/src/postmark_secret_config.rs"
  ],
  "already_satisfied_outcomes": [],
  "expected_strategy_by_outcome": {
    "field-added": "tdd",
    "debug-redaction": "tdd"
  }
}
```

## 10.2 Implementation fixture structure

```text
fixtures/benchmarks/implementation/fixed-red/<case>/
  fixture.json
  ticket.md
  criterion-frame.json
  grading.toml
  patch/
  hidden-tests/
```

Example `criterion-frame.json`:

```json
{
  "criterion": "Debug output redacts the secret values",
  "plan_context": "Update the existing hand-written Debug implementation in email_config.rs.",
  "verification": "test",
  "origin": "fixture",
  "existing_test_refs": [],
  "starting_status": "test-written"
}
```

Example `grading.toml`:

```toml
build_cmd = "cargo test --no-run"
required_test_cmd = "cargo test email_config"
hidden_test_cmd = "cargo test --test benchmark_secret_redaction"
regression_cmd = "cargo test -p virtual_assistant_api"

allowed_changed_paths = [
  "libs/virtual_assistant_api/src/email_config.rs",
  "libs/virtual_assistant_api/tests/"
]

forbidden_changed_paths = [
  "Cargo.lock"
]

forbid_test_deletion = true
forbid_ignored_tests = true
```

## 10.3 Fixture taxonomy

Planning fixtures should include:

- Straightforward extension.
- Misleading or stale ticket path.
- Hidden adjacent obligation.
- Already-implemented requirement.
- Ambiguous requirement.
- Cross-cutting change.
- Dependency ordering.
- Refactor-only criterion.
- Security-sensitive requirement.
- Ticket containing unnecessary prescribed implementation details.

Implementation fixtures should include:

- Local single-file change.
- Multi-file cross-cutting change.
- Existing pattern replication.
- Error handling requirement.
- Security-sensitive behavior.
- Concurrency or state transition behavior.
- Refactor with behavior preservation.
- False-green risk.
- Visible-test overfitting trap.
- Regression-prone adjacent behavior.

---

## 11. Grading Architecture

The grading system should combine generic gates with fixture-specific graders.

```text
generic schema and repository checks
    + fixture-specific semantic checks
    + build and test checks
    + optional human review
    = acceptance verdict
```

## 11.1 Generic graders

Planning:

- Schema validator.
- Strategy-name validator.
- Path existence validator.
- Symbol existence validator.
- Dependency graph validator.
- Criterion uniqueness validator.

Implementation:

- Build runner.
- Required-test runner.
- Hidden-test runner.
- Regression runner.
- Changed-path validator.
- Test-integrity validator.

## 11.2 Fixture-specific graders

Fixture-specific graders should describe domain-specific correctness that cannot be inferred generically.

They should return structured gate results rather than only free-text pass or fail.

Example:

```python
GateResult(
    gate="recognizes_existing_implementation",
    passed=False,
    critical=True,
    reason="The plan proposes duplicate validation logic for behavior already present."
)
```

## 11.3 Human review

Human review should be used only when automated grading remains genuinely uncertain.

The review process should be blinded:

- Remove strategy identity.
- Remove model identity.
- Present the ticket, repository context, output artifact, and automated signals.
- Require `ACCEPT`, `REJECT`, or `UNCERTAIN`.
- Require reason codes.
- Use a second reviewer for uncertain or disputed cases.
- Use a third reviewer to resolve disagreements where required.

Suggested planning review questions:

1. Does the plan cover all required behavior?
2. Is it consistent with the repository state?
3. Could an implementation agent execute it without inventing material decisions?
4. Does it introduce incorrect or unnecessary work?
5. Would this plan be approved for implementation?

---

## 12. Result Data Model

```python
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["accepted", "rejected", "indeterminate"]


@dataclass
class GateResult:
    gate: str
    passed: bool | None
    critical: bool
    reason: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass
class AcceptanceResult:
    verdict: Verdict
    gates: list[GateResult]
    reason_codes: list[str]
    explanation: str
    grader: str
    confidence: float | None = None


@dataclass
class BenchmarkResult:
    run_id: str
    category: str
    suite: str
    case: str
    strategy: str
    model: str
    repetition: int

    scaffold_ref: str
    target_repo_ref: str
    fixture_version: int

    acceptance: AcceptanceResult
    failure_stage: str | None

    duration_s: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    attempts: int
    tool_calls: int
    retries: int
    human_interventions: int

    changed_files: list[str] = field(default_factory=list)
    metrics: dict[str, int | float | bool | str] = field(default_factory=dict)
```

Each trial must be written to JSONL as the authoritative result format.

Console and HTML reports should be generated from the stored JSONL.

---

## 13. Metrics

## 13.1 Primary metrics

- Accepted outcome rate.
- Critical rejection rate.
- Indeterminate rate.
- Cost per accepted outcome.
- Time per accepted outcome.

## 13.2 Planning metrics

- Required-outcome recall.
- Required-outcome precision.
- Repository path accuracy.
- Symbol accuracy.
- Already-satisfied recognition rate.
- Strategy classification accuracy.
- Dependency-ordering accuracy.
- Invalid artifact rate.
- Downstream implementation success rate.
- User-input request rate.
- Invalid submission count.

## 13.3 Implementation metrics

- Build success rate.
- Visible-test success rate.
- Hidden-test success rate.
- Regression rate.
- First-attempt accepted rate.
- Mean attempts to acceptance.
- Scope violation rate.
- Test-integrity violation rate.
- False-green rate.
- Diff size.
- Changed-file count.
- Reversion or reset count.

## 13.4 Process metrics

- Duration.
- Cost.
- Input, output, and total tokens.
- Tool calls.
- Agent turns.
- Retries.
- Invalid submissions.
- Pseudo-tool exits.
- Human interventions.

---

## 14. Ranking and Reporting

Strategies should be ranked lexicographically rather than through a single blended score.

Recommended ordering:

1. Highest accepted-outcome rate.
2. Lowest critical-rejection rate.
3. Lowest indeterminate rate.
4. Lowest cost per accepted outcome.
5. Lowest time per accepted outcome.
6. Best secondary quality metrics.

This prevents a cheap but incorrect strategy from compensating for failures through a weighted efficiency score.

Example report:

```text
Planning strategy benchmark

strategy    accepted   rejected   indeterminate   cost/accepted   time/accepted
agent       18/20      1/20       1/20            $0.42           54s
mechanical  12/20      8/20       0/20            $0.31           29s
```

Example implementation report:

```text
Fixed-red implementation benchmark

strategy   accepted   hidden-pass   first-attempt   regressions   cost/accepted
tdd        19/20      19/20         16/20           0             $0.38
direct     15/20      15/20         12/20           2             $0.34
```

Reports should support grouping by:

- Strategy.
- Model.
- Fixture case type.
- Suite.
- Target repository.
- Failure stage.
- Verdict reason code.

---

## 15. CLI Specification

Recommended commands:

```bash
scaffold benchmark planning \
  --strategies mechanical,agent \
  --models gpt-5.6 \
  --suite core \
  --trials 5
```

```bash
scaffold benchmark implementation \
  --strategies tdd,direct \
  --models gpt-5.6 \
  --suite fixed-red \
  --trials 5
```

```bash
scaffold benchmark end-to-end \
  --planning-strategies mechanical,agent \
  --implementation-strategies tdd,direct \
  --models gpt-5.6 \
  --suite core \
  --trials 3
```

```bash
scaffold benchmark report \
  --input .scaffold/benchmarks/<run-id>/results.jsonl
```

Useful options:

```text
--cases <comma-separated cases>
--repo <path>
--base-ref <commit>
--max-concurrency <n>
--retain-worktrees-on-failure
--fail-fast
--output-dir <path>
--human-review-queue
--user-input-mode infer|fail|interactive
--seed <value>
```

Interactive user input should normally be disabled in automated benchmark runs. `infer` and `fail` should be benchmarked explicitly when user-input behavior is itself under comparison.

---

## 16. Proposed Code Organization

```text
src/ticket_pipeline/benchmark/
  cli.py
  models.py
  runner.py
  worktrees.py
  fixtures.py
  reporting.py
  acceptance.py

  planning/
    runner.py
    graders.py
    repository_validation.py
    downstream.py

  implementation/
    runner.py
    graders.py
    test_integrity.py
    hidden_tests.py

  end_to_end/
    runner.py
```

Existing benchmark capabilities should be reused:

- Worktree isolation.
- Fixture commit pinning.
- Concurrent trial execution.
- Cargo target lanes.
- Per-trial timeout handling.
- Token and cost accounting.
- Machine-readable result output.

The current block-oriented runner should be refactored into reusable infrastructure rather than discarded.

---

## 17. Execution Flow

## 17.1 Planning trial

```text
load fixture
    -> create isolated worktree
    -> checkout pinned target commit
    -> instantiate planning strategy
    -> execute strategy
    -> capture raw artifact and telemetry
    -> validate schema
    -> run repository-grounding checks
    -> run fixture-specific graders
    -> optionally queue human review
    -> emit acceptance result
```

## 17.2 Implementation trial

```text
load fixed accepted criterion frame
    -> create isolated worktree
    -> checkout pinned target commit
    -> apply fixture setup or fixed red test
    -> execute implementation strategy
    -> capture diff and telemetry
    -> validate test integrity
    -> build
    -> run required visible tests
    -> run hidden acceptance tests
    -> run regression suite
    -> validate repository invariants
    -> emit acceptance result
```

## 17.3 End-to-end trial

```text
load ticket fixture
    -> execute planning strategy
    -> grade plan artifact
    -> stop if plan rejected
    -> execute selected implementation strategy
    -> grade final repository outcome
    -> retain stage-specific acceptance results
```

The end-to-end result must preserve both planning and implementation failure attribution.

---

## 18. Failure Stages and Reason Codes

Recommended failure stages:

### Planning

- `planning_execution`
- `planning_schema`
- `planning_repository_grounding`
- `planning_required_outcomes`
- `planning_false_work`
- `planning_strategy_classification`
- `planning_human_review`
- `planning_downstream_execution`

### Implementation

- `implementation_execution`
- `test_generation`
- `test_compile`
- `test_red_validation`
- `implementation_build`
- `visible_tests`
- `hidden_tests`
- `regression_tests`
- `test_integrity`
- `scope_validation`
- `repository_invariants`

Suggested reason codes:

- `invalid_schema`
- `missing_required_outcome`
- `critical_false_positive`
- `stale_ticket_followed`
- `wrong_existing_path`
- `duplicate_implementation`
- `already_satisfied_not_recognized`
- `unsupported_strategy`
- `compile_failure`
- `false_green`
- `irrelevant_red_failure`
- `hidden_test_failure`
- `regression`
- `test_weakened`
- `forbidden_file_changed`
- `manual_acceptance_required`
- `grader_uncertain`

---

## 19. Statistical Guidance

Agent output is non-deterministic, so one trial is not sufficient.

Recommended defaults:

- Development smoke benchmark: 3 trials per case and strategy.
- Routine comparison: 10 trials per case and strategy.
- High-confidence release comparison: 20 or more trials per case and strategy.

Reports should include:

- Trial count.
- Accepted count and rate.
- Binomial confidence interval for acceptance rate.
- Median and percentile duration.
- Median and percentile cost.
- Distribution of failure stages.

Model and strategy effects should be reported separately. Avoid aggregating across models unless the report remains stratified and weighted deliberately.

---

## 20. Phased Implementation Plan

## Phase 1 — Result and acceptance foundation

1. Introduce `GateResult`, `AcceptanceResult`, and generalized `BenchmarkResult`.
2. Add `accepted`, `rejected`, and `indeterminate` verdicts.
3. Store authoritative JSONL output for every trial.
4. Add strategy and category fields to benchmark results.
5. Add cost and time per accepted outcome to reports.
6. Preserve existing block benchmarks through compatibility adapters.

**Exit criteria:** Existing benchmarks run through the new result model without loss of current functionality.

## Phase 2 — Planning strategy benchmark

1. Add `scaffold benchmark planning`.
2. Invoke the actual planning strategy factory and interface.
3. Create the planning fixture schema.
4. Convert existing planning cases into structured expected outcomes.
5. Add generic schema and repository-grounding graders.
6. Add fixture-specific structured graders.
7. Report acceptance rate, rejection reasons, and planning quality metrics.

**Exit criteria:** `mechanical` and `agent` can be compared on the same fixtures with independently graded accepted outcomes.

## Phase 3 — Fixed-red implementation benchmark

1. Add `scaffold benchmark implementation`.
2. Create fixed accepted criterion-frame fixtures.
3. Add fixed-red test setup.
4. Add build, visible-test, hidden-test, regression, and scope gates.
5. Compare `tdd` and `direct` from equivalent starting states.
6. Detect test deletion, ignoring, and assertion weakening.

**Exit criteria:** `tdd` and `direct` can be compared by final accepted repository outcome without planning contamination.

## Phase 4 — End-to-end TDD and stage attribution

1. Benchmark TDD test generation separately.
2. Validate red tests for relevance, not only non-zero exit status.
3. Distinguish test-generation failure from implementation failure.
4. Add hidden-test overfitting detection.

**Exit criteria:** TDD performance can be decomposed into test quality and implementation quality.

## Phase 5 — Human review workflow

1. Add an indeterminate review queue.
2. Produce blinded review bundles.
3. Capture reviewer verdicts and reason codes.
4. Support multiple reviewers and adjudication.
5. Merge human verdicts into final reports without losing automated results.

**Exit criteria:** Semantically ambiguous planning outputs can be graded consistently without exposing strategy identity.

## Phase 6 — End-to-end pipeline benchmark

1. Add planning and implementation strategy matrix execution.
2. Preserve stage-specific acceptance results.
3. Stop rejected plans from proceeding unless explicitly running a resilience experiment.
4. Report complete configuration acceptance and effective cost.

**Exit criteria:** Complete Scaffold configurations can be compared without losing causal attribution.

---

## 21. Initial Recommended Fixture Set

The first planning fixture set should reuse and formalize the existing benchmark concepts:

1. **Straightforward existing-struct extension**  
   Measures baseline comprehension.

2. **Misleading ticket file path**  
   Measures repository grounding and resistance to stale instructions.

3. **Hidden security obligation**  
   Measures whether adjacent manual behavior, such as a hand-written `Debug` implementation, is included.

4. **Already-implemented requirement**  
   Measures avoidance of unnecessary work.

5. **Cross-cutting behavior**  
   Measures criterion decomposition and dependency ordering.

The first implementation fixture set should include:

1. A single-file field and parsing change.
2. A secret-redaction behavior with hidden tests.
3. An already-present behavior that should result in no harmful change.
4. A cross-file behavior with regression risk.
5. A visible-test overfitting trap.

---

## 22. Acceptance Criteria for This Benchmark Feature

The benchmark feature is complete when:

- Planning and implementation strategies can be benchmarked separately.
- Every trial produces `ACCEPTED`, `REJECTED`, or `INDETERMINATE`.
- Acceptance is determined independently from the strategy under test.
- Planning trials use fixed ticket and repository fixtures.
- Implementation trials use fixed accepted planning inputs.
- Implementation grading includes hidden outcome verification.
- Results record strategy, model, fixture, repository commit, cost, duration, and acceptance gates.
- Reports prioritize accepted-outcome rate over cost or speed.
- Cost and time per accepted outcome are available.
- Failure stages and reason codes are preserved.
- Existing benchmark worktree isolation and fixture pinning are retained.
- The framework can later support a separate end-to-end configuration benchmark.

---

## 23. Key Design Decision

The benchmark must treat a generated artifact as an attempt, not a success.

A strategy succeeds only when an independent acceptance oracle determines that its output is valid and capable of satisfying, or has actually satisfied, the required outcome.

This establishes the central comparison rule:

> Correct outcomes determine whether a strategy is useful. Cost, speed, token usage, and process efficiency determine how efficiently it produces those correct outcomes.
