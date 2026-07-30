import subprocess
import sys
import types
import unittest
from pathlib import Path

# Same render stub trick test_repo_context.py uses - keeps this test
# independent of whether `rich` is installed and avoids a real console
# setup as a side effect of importing pipeline_lib.
render_stub = types.ModuleType("ticket_pipeline.lib.render")
render_stub.render_markdown = lambda _text: None
sys.modules.setdefault("ticket_pipeline.lib.render", render_stub)

from ticket_pipeline.lib import pipeline_lib as lib

# The exact SA-454 criterion this whole mechanism was built to catch -
# see criterion-grounding-plan.md. `Outstanding` is not a real
# InvoiceStatus variant; `Balance`/`Paid`/`Approved` are real.
SA454_CRITERION = (
    "- [ ] `Fetched invoices map `Balance = 0` to `Paid`, `Balance > 0` to "
    "`Outstanding`, and `Balance = null` to no status change` "
    "<!-- why: current code still maps nonzero balances to `Approved`, not "
    "`Outstanding`; verify: test; existing_test: "
    "src/quickbooks_webhooks.rs::quickbooks_invoice_balance_statuses_follow_remote_balance -->"
)


def init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def git_add(root: Path, *paths: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=root, check=True)


class ExtractGroundingCandidatesTests(unittest.TestCase):
    def test_sa454_criterion_yields_only_the_real_candidates(self):
        candidates = lib.extract_grounding_candidates(SA454_CRITERION)

        self.assertEqual(["Balance", "Paid", "Outstanding"], candidates)

    def test_drops_sentence_initial_token(self):
        candidates = lib.extract_grounding_candidates("Fetched invoices map to `Paid`.")

        self.assertNotIn("Fetched", candidates)
        self.assertIn("Paid", candidates)

    def test_ignores_trailing_html_comment(self):
        candidates = lib.extract_grounding_candidates(
            "- [ ] Maps to `Paid` <!-- why: also mentions `Cancelled` here -->"
        )

        self.assertIn("Paid", candidates)
        self.assertNotIn("Cancelled", candidates)

    def test_dedupes_preserving_order(self):
        candidates = lib.extract_grounding_candidates("First `Alpha` then `Beta` then `Alpha` again.")

        self.assertEqual(["Alpha", "Beta"], candidates)

    def test_stoplist_filters_candidates(self):
        original = lib.GROUNDING_STOPLIST
        lib.GROUNDING_STOPLIST = frozenset({"Gadget"})
        try:
            candidates = lib.extract_grounding_candidates("Something maps to `Widget` and `Gadget`.")
        finally:
            lib.GROUNDING_STOPLIST = original

        self.assertEqual(["Widget"], candidates)

    def test_no_candidates_returns_empty_list(self):
        self.assertEqual([], lib.extract_grounding_candidates("(ticket validation pending)"))


class CheckSymbolGroundingTests(unittest.TestCase):
    def test_flags_only_the_token_absent_from_tracked_source(self):
        with _TempGitRepo() as root:
            (root / "src").mkdir()
            (root / "src" / "types.rs").write_text(
                "pub enum InvoiceStatus { Draft, Approved, Paid, Cancelled }\n",
                encoding="utf-8",
            )
            git_add(root, "src/types.rs")
            # Untracked file mentioning the fabricated term - must be
            # ignored (tracked-only scope), otherwise the check could be
            # fooled by the pipeline's own scratch files.
            (root / "scratch.md").write_text("Outstanding\n", encoding="utf-8")

            with _cwd(root):
                ungrounded = lib.check_symbol_grounding(["Approved", "Paid", "Outstanding"])

        self.assertEqual(["Outstanding"], ungrounded)

    def test_empty_candidates_returns_empty_list(self):
        with _TempGitRepo() as root, _cwd(root):
            self.assertEqual([], lib.check_symbol_grounding([]))


class VerifyExistingTestRefsResolveTests(unittest.TestCase):
    def test_resolved_ref_returns_no_reasons(self):
        with _TempGitRepo() as root:
            (root / "tests.rs").write_text(
                "fn quickbooks_invoice_balance_statuses_follow_remote_balance() {}\n",
                encoding="utf-8",
            )
            with _cwd(root):
                reasons = lib.verify_existing_test_refs_resolve(
                    ["tests.rs::quickbooks_invoice_balance_statuses_follow_remote_balance"]
                )
        self.assertEqual([], reasons)

    def test_missing_file_is_flagged(self):
        with _TempGitRepo() as root, _cwd(root):
            reasons = lib.verify_existing_test_refs_resolve(["nope.rs::some_test"])
        self.assertEqual(1, len(reasons))
        self.assertIn("does not exist", reasons[0])

    def test_missing_function_in_existing_file_is_flagged(self):
        with _TempGitRepo() as root:
            (root / "tests.rs").write_text("fn some_other_test() {}\n", encoding="utf-8")
            with _cwd(root):
                reasons = lib.verify_existing_test_refs_resolve(["tests.rs::missing_test"])
        self.assertEqual(1, len(reasons))
        self.assertIn("no symbol named", reasons[0])

    def test_malformed_ref_is_flagged(self):
        with _TempGitRepo() as root, _cwd(root):
            reasons = lib.verify_existing_test_refs_resolve(["not-a-valid-ref"])
        self.assertEqual(1, len(reasons))
        self.assertIn("not in 'file::name' shape", reasons[0])

    def test_empty_refs_returns_no_reasons(self):
        with _TempGitRepo() as root, _cwd(root):
            self.assertEqual([], lib.verify_existing_test_refs_resolve([]))


class VerifyCriterionGroundingIntegrationTests(unittest.TestCase):
    def test_sa454_criterion_flags_outstanding_only(self):
        with _TempGitRepo() as root:
            (root / "src").mkdir()
            (root / "src" / "types.rs").write_text(
                "pub enum InvoiceStatus { Draft, Approved, Paid, Cancelled }\n",
                encoding="utf-8",
            )
            (root / "src" / "quickbooks_webhooks.rs").write_text(
                "// remote QuickBooks API field is named \"Balance\"\n"
                "fn quickbooks_invoice_balance_statuses_follow_remote_balance() {}\n",
                encoding="utf-8",
            )
            git_add(root, "src/types.rs", "src/quickbooks_webhooks.rs")

            with _cwd(root):
                reasons = lib.verify_criterion_grounding(
                    SA454_CRITERION, lib.extract_existing_test_refs(SA454_CRITERION)
                )

        self.assertEqual(1, len(reasons))
        self.assertIn("Outstanding", reasons[0])

    def test_fully_grounded_criterion_returns_no_reasons(self):
        with _TempGitRepo() as root:
            (root / "src").mkdir()
            (root / "src" / "types.rs").write_text(
                "pub enum InvoiceStatus { Draft, Approved, Paid, Cancelled }\n",
                encoding="utf-8",
            )
            git_add(root, "src/types.rs")
            criterion = "- [ ] Maps zero balance to `Paid` and nonzero balance to `Approved`."
            with _cwd(root):
                reasons = lib.verify_criterion_grounding(criterion, [])
        self.assertEqual([], reasons)


class DeclinedLedgerTests(unittest.TestCase):
    def test_record_then_is_declined_round_trip(self):
        with _TempGitRepo() as root, _cwd(root):
            self.assertFalse(lib.is_declined("SA-454", SA454_CRITERION))

            lib.record_declined("SA-454", SA454_CRITERION, "validate-missed", ["claims `Outstanding`..."])

            self.assertTrue(lib.is_declined("SA-454", SA454_CRITERION))
            self.assertFalse(lib.is_declined("SA-454", "some other criterion"))
            self.assertFalse(lib.is_declined("SA-999", SA454_CRITERION))

            entries = lib.load_declined()
            self.assertEqual(1, len(entries))
            self.assertEqual("SA-454", entries[0].ticket)
            self.assertEqual("validate-missed", entries[0].origin)

    def test_multiple_records_append_rather_than_overwrite(self):
        with _TempGitRepo() as root, _cwd(root):
            lib.record_declined("SA-1", "criterion one", "ticket", ["reason a"])
            lib.record_declined("SA-2", "criterion two", "review", ["reason b"])

            entries = lib.load_declined()
            self.assertEqual(2, len(entries))

    def test_declined_file_is_in_scaffolding_paths(self):
        self.assertIn(str(lib.DECLINED_CRITERIA_FILE), lib._SCAFFOLDING_PATHS)


class FilterGroundedFramesTests(unittest.TestCase):
    def _frame(self, criterion: str, origin: str = "ticket", existing_test_refs=None) -> "lib.CriterionFrame":
        return lib.CriterionFrame(
            ticket="SA-454",
            criterion=criterion,
            plan_context="",
            test_files=None,
            test_names=None,
            status="pending",
            origin=origin,
            existing_test_refs=existing_test_refs or [],
        )

    def test_grounded_frame_is_pushed_ungrounded_is_declined(self):
        with _TempGitRepo() as root:
            (root / "src").mkdir()
            (root / "src" / "types.rs").write_text(
                "pub enum InvoiceStatus { Draft, Approved, Paid, Cancelled }\n",
                encoding="utf-8",
            )
            git_add(root, "src/types.rs")

            good = self._frame("- [ ] Maps to `Paid` and `Approved`.")
            bad = self._frame(SA454_CRITERION)

            with _cwd(root):
                to_push, newly_declined, skipped = lib.filter_grounded_frames([good, bad])

        self.assertEqual([good], to_push)
        self.assertEqual(1, len(newly_declined))
        self.assertEqual(bad, newly_declined[0][0])
        self.assertEqual(0, skipped)

    def test_sentinel_origin_is_never_checked(self):
        with _TempGitRepo() as root, _cwd(root):
            sentinel = lib.CriterionFrame(
                ticket="SA-454",
                criterion=lib.VALIDATING_CRITERION_TEXT,
                plan_context="",
                test_files=None,
                test_names=None,
                status=lib.VALIDATING_STATUS,
                origin=lib.VALIDATING_ORIGIN,
            )
            to_push, newly_declined, skipped = lib.filter_grounded_frames([sentinel])

        self.assertEqual([sentinel], to_push)
        self.assertEqual([], newly_declined)
        self.assertEqual(0, skipped)

    def test_already_declined_frame_is_skipped_not_rechecked(self):
        with _TempGitRepo() as root, _cwd(root):
            frame = self._frame(SA454_CRITERION)
            lib.record_declined("SA-454", SA454_CRITERION, "ticket", ["some prior reason"])

            to_push, newly_declined, skipped = lib.filter_grounded_frames([frame])

            self.assertEqual([], to_push)
            self.assertEqual([], newly_declined)
            self.assertEqual(1, skipped)
            # Only the original record_declined call wrote an entry - a
            # skipped (already-declined) frame must not be re-recorded.
            self.assertEqual(1, len(lib.load_declined()))


class _TempGitRepo:
    """Context manager yielding a fresh temp dir with `git init` already
    run - the minimum every git-grep-backed grounding check needs to
    operate against real tracked-file semantics rather than mocking git
    out entirely."""

    def __enter__(self) -> Path:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        init_git_repo(root)
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class _cwd:
    """Minimal chdir context manager (contextlib.chdir requires 3.13;
    this package targets >=3.11)."""

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


if __name__ == "__main__":
    unittest.main()
