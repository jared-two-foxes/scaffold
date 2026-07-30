"""
Tests for the "test-refactor already satisfied" handling - three
layered changes (see the plan in the originating ticket):

  A. Mechanical pre-check: check_test_refactor_satisfied /
     _parse_test_refactor_assertions (no AI, host-side text search).
  B. Graceful "nothing to write": _run_tester_step's no-files sentinel
     and run_test_for_criterion_with_full_retry propagating it.
  C. Targeted single-criterion re-narrow: recheck_single_criterion /
     _find_recheck_verdict (AI second opinion).

The mechanical-check tests mirror tests/test_grounding.py's pattern
(temp dir + chdir, the render stub trick so this stays independent of
`rich`); the AI-step tests mock pipeline_lib.run_with_tools so no
network is touched. next_step._handle_no_test_written is exercised
through a render-stubbed import of next_step with its lib calls mocked.
"""

import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

# Same render stub trick tests/test_grounding.py uses - keeps this test
# independent of whether `rich` is installed and avoids a real console
# setup as a side effect of importing pipeline_lib/next_step. next_step
# uses render.print_line (not just render_markdown), so the stub
# provides both.
render_stub = types.ModuleType("ticket_pipeline.lib.render")
render_stub.render_markdown = lambda _text: None
render_stub.print_line = lambda *a, **k: None
sys.modules.setdefault("ticket_pipeline.lib.render", render_stub)

from ticket_pipeline.lib import pipeline_lib as lib  # noqa: E402
from ticket_pipeline.lib.ai_client import AIError, AIResult  # noqa: E402
from ticket_pipeline import next_step  # noqa: E402

# The three SA-529 invoices.rs criteria - the concrete case this whole
# mechanism was built for (the refactor landed before the pipeline run,
# so the gap plan's stale why: annotations describe a state that no
# longer exists, and the tester correctly writes nothing).
SA529_FILE = "libs/virtual_assistant/src/integration/xero/invoices.rs"
SA529_C1 = (
    "- [ ] `libs/virtual_assistant/src/integration/xero/invoices.rs` inline "
    "`#[cfg(test)]` module imports `EnvVarGuard` and `env_test_lock` from "
    "`crate::test_support` <!-- why: still defines local copies; verify: "
    "test-refactor; existing_test: libs/virtual_assistant/src/integration/"
    "xero/invoices.rs::tests::fetch_invoice_by_remote_id_requires_xero_api_base_url -->"
)
SA529_C2 = (
    "- [ ] `libs/virtual_assistant/src/integration/xero/invoices.rs` inline "
    "test module contains no local `EnvVarGuard` struct and no local "
    "`env_lock()` or `lock_env()` helper <!-- why: local helpers still "
    "present; verify: test-refactor; existing_test: libs/virtual_assistant/"
    "src/integration/xero/invoices.rs::tests::fetch_invoice_by_remote_id_"
    "requires_xero_api_base_url -->"
)
SA529_C3 = (
    "- [ ] `libs/virtual_assistant/src/integration/xero/invoices.rs` test "
    "still acquires the shared env lock and still uses "
    "`EnvVarGuard::unset(\"XERO_API_BASE_URL\")` for the missing-base-url "
    "case <!-- why: ...; verify: test-refactor; existing_test: libs/.../"
    "invoices.rs::tests::fetch_invoice_by_remote_id_requires_xero_api_base_url -->"
)


@contextmanager
def _chdir(path: Path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# A satisfied invoices.rs: shared imports present, no local struct/helpers,
# still uses EnvVarGuard::unset and env_test_lock.
SATISFIED_INVOICES = """\
use crate::test_support::{EnvVarGuard, env_test_lock};

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::{EnvVarGuard, env_test_lock};

    #[test]
    fn fetch_invoice_by_remote_id_requires_xero_api_base_url() {
        let _lock = env_test_lock();
        EnvVarGuard::unset("XERO_API_BASE_URL");
        // ... assertions ...
    }
}
"""

# An unsatisfied invoices.rs: local struct + local helpers, no shared import.
UNSATISFIED_INVOICES = """\
#[cfg(test)]
mod tests {
    struct EnvVarGuard { var: String, orig: Option<String> }
    fn env_lock() {}
    fn lock_env() {}

    #[test]
    fn fetch_invoice_by_remote_id_requires_xero_api_base_url() {
        let _lock = lock_env();
        EnvVarGuard::unset("XERO_API_BASE_URL");
    }
}
"""


class ParseTestRefactorAssertionsTests(unittest.TestCase):
    """Unit tests for the claim parser - no file I/O, pure text parsing."""

    def test_imports_clause_yields_source_plus_names_as_positives(self):
        positives, negatives = lib._parse_test_refactor_assertions(
            "module imports `EnvVarGuard` and `env_test_lock` from `crate::test_support`"
        )
        self.assertEqual(
            positives, ["crate::test_support", "EnvVarGuard", "env_test_lock"]
        )
        self.assertEqual([], negatives)

    def test_no_local_struct_clause_yields_struct_negative(self):
        _pos, negatives = lib._parse_test_refactor_assertions(
            "contains no local `EnvVarGuard` struct"
        )
        self.assertEqual(1, len(negatives))
        self.assertTrue(negatives[0].search("struct EnvVarGuard"))
        self.assertFalse(negatives[0].search("struct Other"))

    def test_no_local_helper_clause_yields_fn_negatives_for_each_name(self):
        _pos, negatives = lib._parse_test_refactor_assertions(
            "no local `env_lock()` or `lock_env()` helper"
        )
        names = sorted(n.pattern for n in negatives)
        self.assertEqual(
            [r"\bfn\s+env_lock\b", r"\bfn\s+lock_env\b"], names
        )

    def test_struct_and_helper_clauses_joined_by_and_both_parsed(self):
        _pos, negatives = lib._parse_test_refactor_assertions(
            "contains no local `EnvVarGuard` struct and no local "
            "`env_lock()` or `lock_env()` helper"
        )
        # One struct negative + two fn negatives.
        self.assertEqual(3, len(negatives))
        joined = " ".join(n.pattern for n in negatives)
        self.assertIn("struct", joined)
        self.assertIn("env_lock", joined)
        self.assertIn("lock_env", joined)

    def test_uses_clause_yields_leading_ident_path_positive(self):
        positives, _neg = lib._parse_test_refactor_assertions(
            "still uses `EnvVarGuard::unset(\"XERO_API_BASE_URL\")` here"
        )
        self.assertEqual(["EnvVarGuard::unset"], positives)

    def test_unparseable_wording_yields_no_assertions(self):
        self.assertEqual(([], []), lib._parse_test_refactor_assertions(
            "test still acquires the shared env lock and behaves correctly"
        ))

    def test_imports_keyword_inside_word_is_not_matched_as_uses(self):
        # "imports" must not be misread by the "uses?" pattern (which
        # matches "use"/"uses" at a word boundary); "imports" has no
        # such boundary before "use".
        positives, _neg = lib._parse_test_refactor_assertions(
            "imports `EnvVarGuard` and `env_test_lock` from `crate::test_support`"
        )
        self.assertNotIn("EnvVarGuard::", positives)


class CheckTestRefactorSatisfiedTests(unittest.TestCase):
    """End-to-end mechanical check against temp files (no git needed -
    extract_referenced_paths uses Path.is_file, not git grep)."""

    def test_sa529_all_three_criteria_satisfied_returns_true(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, SA529_FILE, SATISFIED_INVOICES)
            with _chdir(root):
                self.assertTrue(lib.check_test_refactor_satisfied(SA529_C1, []))
                self.assertTrue(lib.check_test_refactor_satisfied(SA529_C2, []))
                self.assertTrue(lib.check_test_refactor_satisfied(SA529_C3, []))

    def test_sa529_unsatisfied_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, SA529_FILE, UNSATISFIED_INVOICES)
            with _chdir(root):
                # C1: no `crate::test_support` import -> positive fails.
                self.assertFalse(lib.check_test_refactor_satisfied(SA529_C1, []))
                # C2: local struct + helpers present -> negatives fail.
                self.assertFalse(lib.check_test_refactor_satisfied(SA529_C2, []))

    def test_imports_present_but_missing_one_name_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, SA529_FILE, """\
use crate::test_support::{EnvVarGuard};  // only one helper imported
#[cfg(test)]
mod tests {
    use crate::test_support::EnvVarGuard;
}
""")
            with _chdir(root):
                # env_test_lock not present anywhere -> positive fails.
                self.assertFalse(lib.check_test_refactor_satisfied(SA529_C1, []))

    def test_no_local_struct_in_cfg_test_still_flagged_returns_false(self):
        # The struct lives inside #[cfg(test)] - still a local definition,
        # so C2 (which requires NO local struct) is not satisfied.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, SA529_FILE, """\
use crate::test_support::{EnvVarGuard, env_test_lock};
#[cfg(test)]
mod tests {
    struct EnvVarGuard { x: u8 }  // local copy still here
    use crate::test_support::env_test_lock;
}
""")
            with _chdir(root):
                self.assertFalse(lib.check_test_refactor_satisfied(SA529_C2, []))

    def test_no_local_helper_present_fn_env_lock_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, SA529_FILE, """\
use crate::test_support::{EnvVarGuard, env_test_lock};
#[cfg(test)]
mod tests {
    fn env_lock() {}  // local helper still here
}
""")
            with _chdir(root):
                self.assertFalse(lib.check_test_refactor_satisfied(SA529_C2, []))

    def test_mixed_positive_and_negative_all_satisfied_returns_true(self):
        criterion = (
            "- [ ] `src/lib.rs` imports `Foo` from `crate::bar` and contains "
            "no local `Foo` struct <!-- verify: test-refactor -->"
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, "src/lib.rs", "use crate::bar::Foo;\n// refactored to the shared helper\n")
            with _chdir(root):
                self.assertTrue(lib.check_test_refactor_satisfied(criterion, []))

    def test_mixed_positive_and_negative_one_negative_fails_returns_false(self):
        criterion = (
            "- [ ] `src/lib.rs` imports `Foo` from `crate::bar` and contains "
            "no local `Foo` struct <!-- verify: test-refactor -->"
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # import present (positive ok) BUT a local struct also present
            # (negative fails).
            _write(root, "src/lib.rs", "use crate::bar::Foo;\nstruct Foo;\n")
            with _chdir(root):
                self.assertFalse(lib.check_test_refactor_satisfied(criterion, []))

    def test_unparseable_wording_returns_false(self):
        # A criterion with no recognized structural pattern -> no
        # assertions parsed -> inconclusive (False), never vacuously True.
        criterion = "- [ ] `src/lib.rs` test still acquires the shared env lock and behaves correctly"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, "src/lib.rs", "fn main() {}\n")
            with _chdir(root):
                self.assertFalse(lib.check_test_refactor_satisfied(criterion, []))

    def test_nonexistent_file_returns_false(self):
        criterion = "- [ ] `does/not/exist.rs` imports `Foo` from `crate::bar`"
        with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
            self.assertFalse(lib.check_test_refactor_satisfied(criterion, []))

    def test_existing_test_refs_param_is_accepted_but_not_required(self):
        # existing_test_refs is part of the signature for symmetry; the
        # check itself works off the criterion text + file content, so
        # passing refs (or none) doesn't change a satisfied verdict.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root, SA529_FILE, SATISFIED_INVOICES)
            with _chdir(root):
                self.assertTrue(
                    lib.check_test_refactor_satisfied(
                        SA529_C1,
                        [f"{SA529_FILE}::tests::fetch_invoice_by_remote_id_requires_xero_api_base_url"],
                    )
                )


class FindRecheckVerdictTests(unittest.TestCase):
    def test_bare_verdict_on_final_line(self):
        self.assertEqual("SATISFIED", lib._find_recheck_verdict("evidence...\nSATISFIED"))
        self.assertEqual("NOT SATISFIED", lib._find_recheck_verdict("evidence\nNOT SATISFIED"))
        self.assertEqual("UNKNOWN", lib._find_recheck_verdict("evidence\nUNKNOWN"))

    def test_verdict_after_label_with_markdown_emphasis(self):
        self.assertEqual(
            "SATISFIED",
            lib._find_recheck_verdict("## Verdict\n**SATISFIED**"),
        )

    def test_not_satisfied_preferred_over_satisfied_substring_in_reasoning(self):
        # If the reasoning mentions both, the final-line verdict wins
        # (the prompt instructs the model to put the verdict last).
        text = (
            "At first glance this looks NOT SATISFIED, but reading the "
            "file shows the import is present.\nSATISFIED"
        )
        self.assertEqual("SATISFIED", lib._find_recheck_verdict(text))

    def test_unparseable_returns_none(self):
        self.assertIsNone(lib._find_recheck_verdict("the criterion is met I think"))


class RecheckSingleCriterionTests(unittest.TestCase):
    """Mock pipeline_lib.run_with_tools so no network is touched."""

    def _run(self, ai_text):
        with mock.patch.object(lib, "run_with_tools", return_value=AIResult(text=ai_text)):
            return lib.recheck_single_criterion("criterion", "context", "model", ticket_id="SA-1")

    def test_parses_satisfied(self):
        with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):  # log_event writes here
            self.assertEqual("SATISFIED", self._run("all good\nSATISFIED"))

    def test_parses_not_satisfied(self):
        with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
            self.assertEqual("NOT SATISFIED", self._run("missing import\nNOT SATISFIED"))

    def test_parses_unknown(self):
        with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
            self.assertEqual("UNKNOWN", self._run("can't tell\nUNKNOWN"))

    def test_ai_failure_degrades_to_unknown(self):
        # No time.sleep delay: patch sleep so the retry backoff is free.
        with mock.patch.object(lib, "time", new=mock.MagicMock()):
            with mock.patch.object(
                lib, "run_with_tools", side_effect=AIError("boom")
            ):
                with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
                    verdict = lib.recheck_single_criterion("c", "ctx", "model")
        self.assertEqual("UNKNOWN", verdict)

    def test_unparseable_verdict_degrades_to_unknown(self):
        with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
            self.assertEqual("UNKNOWN", self._run("no verdict line here"))


class RunTesterStepNoFilesTests(unittest.TestCase):
    """Change B: the no-files sentinel."""

    def test_no_files_is_fatal_true_dies(self):
        with mock.patch.object(lib, "run_with_tools", return_value=AIResult(text="")):
            with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
                with self.assertRaises(SystemExit):
                    lib._run_tester_step("p", "model", None, "c", no_files_is_fatal=True)

    def test_no_files_is_fatal_false_returns_none_sentinel(self):
        with mock.patch.object(lib, "run_with_tools", return_value=AIResult(text="")):
            with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
                result = lib._run_tester_step("p", "model", None, "c", no_files_is_fatal=False)
        self.assertEqual((None, None), result)


class FullRetryNoFilesTests(unittest.TestCase):
    def test_full_retry_propagates_sentinel_without_running_compile(self):
        # run_test_for_criterion_with_full_retry should short-circuit on
        # the no-files sentinel and return (None, None, [], None, None)
        # without invoking the compile gate.
        commands = {"test_compile_cmd": "false"}  # must NOT be called
        with mock.patch.object(lib, "run_with_tools", return_value=AIResult(text="")):
            with mock.patch.object(lib, "run_command") as run_command:
                with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
                    out = lib.run_test_for_criterion_with_full_retry(
                        "criterion", "context", "model", commands,
                    )
        self.assertEqual((None, None, [], None, None), out)
        run_command.assert_not_called()


class HandleNoTestWrittenTests(unittest.TestCase):
    """Change B + C: the recovery path in next_step._handle_no_test_written."""

    def _frame(self, criterion=SA529_C2, verification="test-refactor"):
        return lib.CriterionFrame(
            ticket="SA-529",
            criterion=criterion,
            plan_context="some plan context",
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification=verification,
            existing_test_refs=[],
        )

    def test_accept_no_test_pops_frame(self):
        frame = self._frame()
        stack = [frame]
        with mock.patch.object(lib, "load_stack", return_value=stack), \
             mock.patch.object(lib, "save_stack") as save_stack:
            next_step._handle_no_test_written(stack, frame, "model", accept_no_test=True)
        self.assertEqual("done", frame.status)
        self.assertEqual([], frame.unconfirmed_tests)
        save_stack.assert_called_once()

    def test_mechanical_check_satisfied_pops_frame(self):
        # Provide a satisfied file so check_test_refactor_satisfied
        # returns True; AI recheck must NOT be called.
        frame = self._frame()
        stack = [frame]
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), SA529_FILE, SATISFIED_INVOICES)
            with _chdir(Path(d)), \
                 mock.patch.object(lib, "load_stack", return_value=stack), \
                 mock.patch.object(lib, "save_stack"), \
                 mock.patch.object(lib, "recheck_single_criterion") as recheck:
                next_step._handle_no_test_written(stack, frame, "model", accept_no_test=False)
            recheck.assert_not_called()
        self.assertEqual("done", frame.status)

    def test_recheck_satisfied_pops_frame(self):
        frame = self._frame(criterion="- [ ] `nope.rs` behavioral criterion", verification="test")
        stack = [frame]
        with mock.patch.object(lib, "check_test_refactor_satisfied", return_value=False), \
             mock.patch.object(lib, "load_stack", return_value=stack), \
             mock.patch.object(lib, "save_stack"), \
             mock.patch.object(lib, "recheck_single_criterion", return_value="SATISFIED"):
            next_step._handle_no_test_written(stack, frame, "model", accept_no_test=False)
        self.assertEqual("done", frame.status)

    def test_recheck_not_satisfied_pauses(self):
        frame = self._frame(criterion="- [ ] `nope.rs` behavioral criterion", verification="test")
        stack = [frame]
        with mock.patch.object(lib, "check_test_refactor_satisfied", return_value=False), \
             mock.patch.object(lib, "load_stack", return_value=stack), \
             mock.patch.object(lib, "save_stack"), \
             mock.patch.object(lib, "recheck_single_criterion", return_value="NOT SATISFIED"):
            with self.assertRaises(SystemExit) as cm:
                next_step._handle_no_test_written(stack, frame, "model", accept_no_test=False)
        self.assertEqual(0, cm.exception.code)
        self.assertEqual(next_step.NOTHING_WRITTEN_STATUS, frame.status)

    def test_resume_skip_ai_does_not_call_recheck_and_pauses(self):
        # skip_ai=True (the resume path): mechanical check inconclusive
        # -> pause, and recheck must NOT be called.
        frame = self._frame(criterion="- [ ] `nope.rs` behavioral criterion", verification="test")
        stack = [frame]
        with mock.patch.object(lib, "check_test_refactor_satisfied", return_value=False), \
             mock.patch.object(lib, "load_stack", return_value=stack), \
             mock.patch.object(lib, "save_stack"), \
             mock.patch.object(lib, "recheck_single_criterion") as recheck:
            with self.assertRaises(SystemExit):
                next_step._handle_no_test_written(
                    stack, frame, "model", accept_no_test=False, skip_ai=True,
                )
        recheck.assert_not_called()
        self.assertEqual(next_step.NOTHING_WRITTEN_STATUS, frame.status)

    def test_resume_accept_no_test_pops_without_ai(self):
        frame = self._frame()
        stack = [frame]
        with mock.patch.object(lib, "load_stack", return_value=stack), \
             mock.patch.object(lib, "save_stack"), \
             mock.patch.object(lib, "recheck_single_criterion") as recheck:
            next_step._handle_no_test_written(
                stack, frame, "model", accept_no_test=True, skip_ai=True,
            )
        recheck.assert_not_called()
        self.assertEqual("done", frame.status)


if __name__ == "__main__":
    unittest.main()