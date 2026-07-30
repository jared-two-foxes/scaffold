"""
Tests that the pipeline's parsing/grounding functions still extract the
right data when the narrower *scopes* a partially-met criterion to just
the unmet items (see narrow-plan.prompt.md's Step 3 "Scoping
partially-met criteria"). Scoping narrows *which* items a criterion
applies to (e.g. "all 11 files" -> "these 2 files") without changing
*what* it requires of them. No pipeline code changed to support
scoping - these tests pin that invariant, so a future refactor can't
silently break it.

The concrete regression case is SA-528: the ticket lists 11 test files
that should use shared helpers from `virtual_assistant::test_support`.
At narrow time 9 are already migrated; the 2 still defining local
helpers are `xero_reconcile_observability.rs` and `xero_webhook.rs`.
The scoped criterion names those 2 files specifically instead of "each
of the 11 listed test files."

Same render-stub + temp-git-repo pattern as tests/test_grounding.py.
"""

import sys
import types
import unittest
from pathlib import Path

render_stub = types.ModuleType("ticket_pipeline.lib.render")
render_stub.render_markdown = lambda _text: None
sys.modules.setdefault("ticket_pipeline.lib.render", render_stub)

from ticket_pipeline.lib import pipeline_lib as lib

# A scoped criterion of the SA-528 shape: the substance ("use shared
# helpers from `virtual_assistant::test_support` instead of local
# copies") is identical to the broad original, but the scope narrowed
# from "each of the 11 listed test files" to the 2 still-unmet files.
# The `why:` annotation records the original scope and which items are
# already met.
SCOPED_SA528_CRITERION = (
    "- [ ] `xero_reconcile_observability.rs` and `xero_webhook.rs` use the "
    "shared helper(s) from `virtual_assistant::test_support` instead of "
    "local copies. "
    "<!-- why: original criterion covers 11 files; 9 already migrated; "
    "these 2 still define local EnvVarGuard/lock helpers; verify: "
    "test-refactor; existing_test: "
    "tests/xero_reconcile_observability.rs::reconcile_observability; "
    "existing_test: tests/xero_webhook.rs::webhook_handler -->"
)

# A mini gap plan exercising extract_acceptance_criteria and
# extract_plan_context_for_criterion on scoped text. The Implementation
# Plan entry mentions the 2 unmet files specifically; a broad original
# criterion ("all 11 files") would have matched none of these lines, so
# scoping makes plan-context extraction *more* precise, not less.
SCOPED_GAP_PLAN = """\
<!-- narrowed by Narrower on 2025-01-01 from .tdd-plan.md -->

## Source
SA-528

## Acceptance Criteria
- [ ] `xero_reconcile_observability.rs` and `xero_webhook.rs` use the shared helper(s) from `virtual_assistant::test_support` instead of local copies. <!-- why: original criterion covers 11 files; 9 already migrated; these 2 still define local EnvVarGuard/lock helpers; verify: test-refactor; existing_test: tests/xero_reconcile_observability.rs::reconcile_observability; existing_test: tests/xero_webhook.rs::webhook_handler -->

## Implementation Plan
- `tests/xero_reconcile_observability.rs`: replace local EnvVarGuard with `virtual_assistant::test_support::EnvVarGuard`
- `tests/xero_webhook.rs`: replace local lock helper with `virtual_assistant::test_support::lock`
"""


def init_git_repo(root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def git_add(root: Path, *paths: str) -> None:
    import subprocess
    subprocess.run(["git", "add", *paths], cwd=root, check=True)


class _TempGitRepo:
    def __enter__(self) -> Path:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        init_git_repo(root)
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class _cwd:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._prev: str | None = None

    def __enter__(self) -> None:
        import os
        self._prev = os.getcwd()
        os.chdir(self._path)

    def __exit__(self, *exc) -> None:
        import os
        os.chdir(self._prev)


class ExtractAcceptanceCriteriaScopedTests(unittest.TestCase):
    def test_returns_the_scoped_criterion_text(self):
        criteria = lib.extract_acceptance_criteria(SCOPED_GAP_PLAN)
        self.assertEqual(1, len(criteria))
        # The scoped criterion is returned verbatim, including the
        # trailing comment with its why/verify/existing_test tags.
        self.assertEqual(SCOPED_SA528_CRITERION, criteria[0])

    def test_empty_criteria_section_returns_empty_list(self):
        plan = "## Acceptance Criteria\n(none - all criteria satisfied)\n"
        self.assertEqual([], lib.extract_acceptance_criteria(plan))


class ExtractVerificationModeScopedTests(unittest.TestCase):
    def test_parses_test_refactor_from_scoped_criterion(self):
        self.assertEqual(
            "test-refactor",
            lib.extract_verification_mode(SCOPED_SA528_CRITERION),
        )

    def test_parses_test_when_no_tag_present(self):
        # A scoped criterion without a verify tag still defaults to
        # "test" - scoping doesn't change the default.
        scoped = "- [ ] `foo.rs` uses `Bar`. <!-- why: original covers 3 files; 2 done -->"
        self.assertEqual("test", lib.extract_verification_mode(scoped))


class ExtractExistingTestRefsScopedTests(unittest.TestCase):
    def test_returns_refs_pointing_only_at_unmet_files(self):
        refs = lib.extract_existing_test_refs(SCOPED_SA528_CRITERION)
        self.assertEqual(
            [
                "tests/xero_reconcile_observability.rs::reconcile_observability",
                "tests/xero_webhook.rs::webhook_handler",
            ],
            refs,
        )

    def test_no_existing_test_tag_returns_empty(self):
        scoped = "- [ ] `foo.rs` uses `Bar`. <!-- why: original covers 3 files; 2 done; verify: manual -->"
        self.assertEqual([], lib.extract_existing_test_refs(scoped))


class ExtractPlanContextForCriterionScopedTests(unittest.TestCase):
    def test_scoped_criterion_matches_only_unmet_files_entries(self):
        # The scoped criterion names `xero_reconcile_observability.rs`
        # and `xero_webhook.rs` in backticks, so plan-context extraction
        # matches just the two Implementation Plan lines for those files
        # - not the already-migrated `tests/foo.rs` entry.
        context = lib.extract_plan_context_for_criterion(
            SCOPED_SA528_CRITERION, SCOPED_GAP_PLAN
        )
        lines = context.splitlines()
        self.assertEqual(2, len(lines))
        self.assertTrue(
            all("xero_reconcile_observability" in l or "xero_webhook" in l for l in lines)
        )
        self.assertNotIn("foo.rs", context)

    def test_broad_original_criterion_matches_the_same_entries(self):
        # A gap plan's Implementation Plan only contains entries for
        # RETAINED (unmet) criteria - PASS-criterion entries are dropped
        # per Step 3. So both the broad original and the scoped criterion
        # match the same 2 lines here: the broad one via the shared
        # `virtual_assistant::test_support` token, the scoped one via the
        # specific file names (and the shared token). Scoping's benefit
        # is in the criterion TEXT (the test-writer/implementer focus on
        # the 2 unmet files), not in plan-context exclusion. This test
        # pins that scoping never makes plan-context extraction WORSE:
        # it returns exactly the relevant entries.
        broad = (
            "- [ ] Each of the 11 listed test files uses the shared helper(s) "
            "from `virtual_assistant::test_support` instead of local copies. "
            "<!-- why: not yet satisfied; verify: test-refactor -->"
        )
        broad_context = lib.extract_plan_context_for_criterion(broad, SCOPED_GAP_PLAN)
        scoped_context = lib.extract_plan_context_for_criterion(
            SCOPED_SA528_CRITERION, SCOPED_GAP_PLAN
        )
        # Both return exactly the 2 retained entries - scoping doesn't
        # lose the relevant plan context.
        self.assertEqual(2, len(broad_context.splitlines()))
        self.assertEqual(2, len(scoped_context.splitlines()))
        self.assertIn("xero_reconcile_observability", scoped_context)
        self.assertIn("xero_webhook", scoped_context)


class VerifyCriterionGroundingScopedTests(unittest.TestCase):
    def test_scoped_criterion_grounds_when_symbols_exist(self):
        # The scoped criterion mentions `EnvVarGuard` (as a prose
        # reference in the why-comment? No - it's in the visible text of
        # SCOPED_SA528_CRITERION? Actually it's only in the trailing
        # comment). The visible text's capitalized candidates are
        # `EnvVarGuard`-free; grounding only checks visible text. The
        # existing_test refs point at files we create here, so they
        # resolve. Result: no reasons (fully grounded).
        with _TempGitRepo() as root:
            (root / "tests").mkdir()
            (root / "tests" / "xero_reconcile_observability.rs").write_text(
                "fn reconcile_observability() {}\n", encoding="utf-8"
            )
            (root / "tests" / "xero_webhook.rs").write_text(
                "fn webhook_handler() {}\n", encoding="utf-8"
            )
            with _cwd(root):
                refs = lib.extract_existing_test_refs(SCOPED_SA528_CRITERION)
                reasons = lib.verify_criterion_grounding(SCOPED_SA528_CRITERION, refs)
        self.assertEqual([], reasons)

    def test_scoped_criterion_flags_missing_existing_test_file(self):
        # The scoped criterion's existing_test refs point at files that
        # don't exist yet (the gap) - grounding flags them, same as it
        # would for a broad criterion.
        with _TempGitRepo() as root, _cwd(root):
            refs = lib.extract_existing_test_refs(SCOPED_SA528_CRITERION)
            reasons = lib.verify_criterion_grounding(SCOPED_SA528_CRITERION, refs)
        self.assertEqual(2, len(reasons))
        joined = " ".join(reasons)
        self.assertIn("xero_reconcile_observability.rs", joined)
        self.assertIn("xero_webhook.rs", joined)

    def test_scoped_criterion_with_ungrounded_symbol_is_flagged(self):
        # A scoped criterion whose visible text names a symbol not in
        # tracked source is flagged - scoping doesn't weaken the
        # symbol-grounding check. `extract_grounding_candidates` drops
        # the first capitalized token as sentence-initial, so the
        # ungrounded symbol must NOT be the first candidate: put a
        # grounded symbol (`Paid`, present in a tracked file) ahead of
        # the ungrounded one (`EnvVarGuard`).
        scoped = (
            "- [ ] `xero_reconcile_observability.rs` maps `Paid` and uses "
            "`EnvVarGuard` from `virtual_assistant::test_support`. "
            "<!-- why: original covers 11 files; 10 done; verify: test-refactor; "
            "existing_test: tests/xero_reconcile_observability.rs::reconcile_observability -->"
        )
        with _TempGitRepo() as root:
            (root / "src").mkdir()
            (root / "src" / "types.rs").write_text(
                "pub enum InvoiceStatus { Draft, Paid }\n", encoding="utf-8"
            )
            (root / "tests").mkdir()
            (root / "tests" / "xero_reconcile_observability.rs").write_text(
                "fn reconcile_observability() {}\n", encoding="utf-8"
            )
            git_add(root, "src/types.rs", "tests/xero_reconcile_observability.rs")
            with _cwd(root):
                refs = lib.extract_existing_test_refs(scoped)
                reasons = lib.verify_criterion_grounding(scoped, refs)
        # `Paid` is grounded (in src/types.rs) and dropped as
        # sentence-initial anyway; `EnvVarGuard` is the only ungrounded
        # candidate, so exactly one reason flags it.
        self.assertEqual(1, len(reasons))
        self.assertIn("EnvVarGuard", reasons[0])


class SA528RegressionTests(unittest.TestCase):
    """The SA-528 case end-to-end through the parsing functions: a
    scoped gap-plan criterion produces the expected frame data
    (correct verification mode, correct test refs pointing only at the
    2 unmet files' tests)."""

    def test_sa528_scoped_criterion_extracts_correct_frame_data(self):
        criteria = lib.extract_acceptance_criteria(SCOPED_GAP_PLAN)
        self.assertEqual(1, len(criteria))
        criterion = criteria[0]

        self.assertEqual("test-refactor", lib.extract_verification_mode(criterion))

        refs = lib.extract_existing_test_refs(criterion)
        self.assertEqual(
            [
                "tests/xero_reconcile_observability.rs::reconcile_observability",
                "tests/xero_webhook.rs::webhook_handler",
            ],
            refs,
        )

        context = lib.extract_plan_context_for_criterion(criterion, SCOPED_GAP_PLAN)
        self.assertNotIn("foo.rs", context)
        self.assertEqual(2, len(context.splitlines()))

    def test_sa528_scoped_frame_passes_grounding_when_files_exist(self):
        # Build a CriterionFrame as push_ticket would, from the scoped
        # gap plan, and run it through filter_grounded_frames - the
        # frame should be pushed (not declined) when the referenced
        # test files exist.
        with _TempGitRepo() as root:
            (root / "tests").mkdir()
            (root / "tests" / "xero_reconcile_observability.rs").write_text(
                "fn reconcile_observability() {}\n", encoding="utf-8"
            )
            (root / "tests" / "xero_webhook.rs").write_text(
                "fn webhook_handler() {}\n", encoding="utf-8"
            )
            git_add(root, "tests/xero_reconcile_observability.rs", "tests/xero_webhook.rs")

            criterion = lib.extract_acceptance_criteria(SCOPED_GAP_PLAN)[0]
            frame = lib.CriterionFrame(
                ticket="SA-528",
                criterion=criterion,
                plan_context=lib.extract_plan_context_for_criterion(criterion, SCOPED_GAP_PLAN),
                test_files=None,
                test_names=None,
                status="pending",
                origin="ticket",
                existing_test_refs=lib.extract_existing_test_refs(criterion),
            )
            with _cwd(root):
                to_push, newly_declined, skipped = lib.filter_grounded_frames([frame])

        self.assertEqual([frame], to_push)
        self.assertEqual([], newly_declined)
        self.assertEqual(0, skipped)


if __name__ == "__main__":
    unittest.main()