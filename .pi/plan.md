# Plan: Fix scaffold repo after split from dotfiles

The scaffold repo was extracted from `C:/Users/iapet/code/tooling/dotfiles/ticket-pipeline/` into a standalone repo at `C:/Users/iapet/code/own/scaffold/`. The old subdirectory no longer exists. Several things are broken, missing, or stale as a result.

## Current state

- **137 tests collected**, 129 pass, 8 fail (7 from missing prompts, 1 pre-existing git default-branch issue)
- `scaffold` CLI command broken (editable install points to deleted old path)
- `prompts/` directory exists but is empty
- `PROMPTS_DIR` resolves one level too high (`own/prompts/` instead of `scaffold/prompts/`)
- No `.gitignore`, no commits, no remote
- Build artifacts present (`egg-info/`, `__pycache__/`, `.ruff_cache/`)

---

## Step 1: Fix `PROMPTS_DIR` path resolution

**File:** `ticket_pipeline/lib/pipeline_lib.py`

The old repo nested the package 4 levels deep (`old-repo/ticket-pipeline/ticket_pipeline/lib/`), so `SCRIPT_DIR.parent.parent.parent` reached the repo root. The new repo is 3 levels deep (`scaffold/ticket_pipeline/lib/`), so only `SCRIPT_DIR.parent.parent` is needed.

### Changes

- Line 70: `PROMPTS_DIR = SCRIPT_DIR.parent.parent.parent / "prompts"` → `PROMPTS_DIR = SCRIPT_DIR.parent.parent / "prompts"`
- Lines 68–69: Update the comment from "Three levels up: this module lives in ticket-pipeline/ticket_pipeline/lib/, prompts/ is a repo-root sibling of ticket-pipeline/" to reflect the new 2-level structure.

### Verification

```python
python -c "from ticket_pipeline.lib.pipeline_lib import PROMPTS_DIR; print(PROMPTS_DIR); assert PROMPTS_DIR.exists()"
```

Should print `C:\Users\iapet\code\own\scaffold\prompts` and assert passes (once prompts are copied in Step 2).

---

## Step 2: Copy prompt files into `prompts/`

**Source:** `C:/Users/iapet/code/tooling/dotfiles/prompts/`
**Destination:** `C:/Users/iapet/code/own/scaffold/prompts/`

Copy these 16 files (only the ones referenced by the code):

```
plan.prompt.md
narrow-plan.prompt.md
plan-narrow.prompt.md
review-singlepass.prompt.md
test-criterion.prompt.md
test-refine.prompt.md
review-test-quality.prompt.md
recheck-criterion.prompt.md
explore-criterion.prompt.md
implement-criterion.prompt.md
implement-criterion-direct.prompt.md
implement-criterion-refactor.prompt.md
implement-refine.prompt.md
propose-ticket-edit.prompt.md
review-ticket.prompt.md
split-ticket.prompt.md
```

**Do NOT copy** these 7 files from the old prompts directory — they are not referenced by the pipeline code:
`explore.prompt.md`, `file-knowledge.prompt.md`, `implement.prompt.md`, `research-assistant.prompt.md`, `review.prompt.md`, `test.prompt.md`, `validate.prompt.md`

### Verification

```bash
python -c "
from ticket_pipeline.lib import pipeline_lib as lib
for name, f in [
    ('PLAN', lib.PLAN_PROMPT_FILE),
    ('PLAN_NARROW', lib.PLAN_NARROW_PROMPT_FILE),
    ('TEST_CRITERION', lib.TEST_CRITERION_PROMPT_FILE),
    ('RECHECK_CRITERION', lib.RECHECK_CRITERION_PROMPT_FILE),
    ('EXPLORE_CRITERION', lib.EXPLORE_CRITERION_PROMPT_FILE),
]:
    assert f.exists(), f'MISSING: {f}'
print('All prompt files found')
"
```

Also re-run the 7 previously-failing tests:
```bash
python -m pytest tests/test_refactor_check.py tests/test_repo_context.py::PlanNarrowPromptTests -q
```

All should pass after Steps 1+2.

---

## Step 3: Re-install the editable package

The current editable install points to the deleted path `C:/Users/iapet/code/tooling/dotfiles/ticket-pipeline`. The `scaffold` console script is broken.

```bash
pip install -e .
```

### Verification

```bash
scaffold --help
```

Should print the help text (currently fails with a traceback).

---

## Step 4: Create `.gitignore`

Create `C:/Users/iapet/code/own/scaffold/.gitignore`:

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/

# Linter cache
.ruff_cache/

# Pipeline runtime state files
.pipeline-log.jsonl
.criteria-stack.json
.tdd-plan.md
.gap-plan.md
.declined-criteria.json
.dev-pipeline.toml
.pipeline-git-state.json
.ticket.md
.updated-plan.md

# OS artifacts
.DS_Store
Thumbs.db
desktop.ini
```

---

## Step 5: Delete build artifacts and caches

These are regenerable and should not be committed:

```bash
rm -rf ticket_pipeline.egg-info/
rm -rf ticket_pipeline/__pycache__/
rm -rf ticket_pipeline/lib/__pycache__/
rm -rf .ruff_cache/
```

(Step 3's `pip install -e .` will recreate `egg-info/`, which is now gitignored.)

---

## Step 6: Update stale comments referencing old repo structure

Update these comments to reflect the new standalone repo layout:

| File | Lines | Current text (paraphrased) | Fix |
|------|-------|---------------------------|-----|
| `pipeline_lib.py` | 32 | "prompts lives in the repo's legacy-pipeline/ directory" | Remove or rewrite — no legacy-pipeline dir exists |
| `pipeline_lib.py` | 68–69 | "Three levels up: this module lives in ticket-pipeline/ticket_pipeline/lib/" | Update to "Two levels up: this module lives in ticket_pipeline/lib/" (covered in Step 1) |
| `bench.py` | 57–58 | "root (ticket-pipeline/) rather than inside the installed" | Update to reference repo root directly |
| `bench_block.py` | 36 | "See ../criteria-stack-plan.md and ../legacy-pipeline/" | Remove stale reference — neither file exists |
| `ai_client.py` | 40 | "ticket-pipeline/ticket_pipeline/lib/model-pricing.toml" | Update to "ticket_pipeline/lib/model-pricing.toml" |
| `ai_client.py` | 179 | "Loads ticket-pipeline/ticket_pipeline/lib/model-pricing.toml" | Same fix |
| `pyproject.toml` | 1–6 | References old layout rationale about editable install and paths | Update to reflect new flat structure |

---

## Step 7: Fix `test_branch_exists_create_checkout` (optional, pre-existing)

**File:** `tests/test_git_workflow.py`, lines 163–164

The test hardcodes `master` but this system's git default branch is `main`. Two options:

- **Option A:** Change `init_git_repo()` to pass `-b master` to `git init`, making the test explicit regardless of system default.
- **Option B:** Change the test to use whatever branch `git init` created (query it dynamically).

Option A is simpler and more explicit.

### Verification

```bash
python -m pytest tests/test_git_workflow.py::GitHelperTests::test_branch_exists_create_checkout -q
```

---

## Step 8: Initial git commit and remote setup

```bash
git add -A
git commit -m "Initial commit: ticket-pipeline scaffold tool extracted from dotfiles"
git remote add origin <remote-url>
git push -u origin main
```

(Do this after Steps 1–6 so the first commit is clean.)

---

## Execution order

Steps 1 and 2 must happen together (path fix is useless without prompt files; prompt files are invisible without path fix). Step 3 depends on 1+2. Steps 4–6 are independent cleanup. Step 7 is independent. Step 8 should be last.

```
Step 1 (fix PROMPTS_DIR) ─┐
Step 2 (copy prompts)     ─┼─→ Step 3 (pip install -e .) ─┐
                          │                                ├──→ Step 8 (git commit + push)
Step 4 (.gitignore)      ──┤                                │
Step 5 (delete artifacts) ──┤                                │
Step 6 (stale comments)  ──┘                                │
Step 7 (master/main test) ─────────────────────────────────┘
```