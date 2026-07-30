import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Same render stub trick test_repo_context.py/test_grounding.py use - keeps
# this test independent of whether `rich` is installed and avoids a real
# console setup as a side effect of importing pipeline_lib. Must be
# registered before ticket_pipeline.lib.pipeline_lib is imported below.
render_stub = types.ModuleType("ticket_pipeline.lib.render")
render_stub.render_markdown = lambda _text: None
render_stub.print_line = lambda _text="": None
sys.modules.setdefault("ticket_pipeline.lib.render", render_stub)

from ticket_pipeline.lib import pipeline_lib as lib


def init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def git_commit(root: Path, msg: str = "init") -> None:
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)


def write_commit(root: Path, path: str, content: str, msg: str = "change") -> str:
    (root / path).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=root, check=True)
    git_commit(root, msg)
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


class GitConfigTests(unittest.TestCase):
    def test_defaults_to_disabled(self):
        cfg = lib.load_git_config(Path("does-not-exist.toml"))
        self.assertFalse(cfg.git_workflow)
        self.assertEqual(cfg.branch_prefix, "ticket/")
        self.assertEqual(cfg.forge, "none")

    def test_reads_enabled_config(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "cfg.toml").write_text(
                'git_workflow = true\nbase_branch = "main"\n'
                'git_merge_on_validate = false\nbranch_prefix = "t/"\n',
                encoding="utf-8",
            )
            cfg = lib.load_git_config(Path(d) / "cfg.toml")
            self.assertTrue(cfg.git_workflow)
            self.assertEqual(cfg.base_branch, "main")
            self.assertFalse(cfg.git_merge_on_validate)
            self.assertEqual(cfg.branch_prefix, "t/")

    def test_ticket_branch_name_uses_prefix(self):
        cfg = lib.GitConfig(git_workflow=True, branch_prefix="t/")
        self.assertEqual(lib.ticket_branch_name(cfg, "SA-1"), "t/SA-1")
        cfg2 = lib.GitConfig(git_workflow=True)
        self.assertEqual(lib.ticket_branch_name(cfg2, "SA-1"), "ticket/SA-1")


class CriterionCommitMessageTests(unittest.TestCase):
    def test_strips_checkbox_and_truncates(self):
        cfg = lib.GitConfig(git_workflow=True)
        long = "- [ ] " + "x" * 200
        msg = lib.criterion_commit_message(cfg, "SA-9", long)
        self.assertTrue(msg.startswith("ticket/SA-9: "))
        self.assertNotIn("[ ]", msg)
        self.assertLessEqual(len(msg), len("ticket/SA-9: ") + 72)

    def test_short_criterion(self):
        cfg = lib.GitConfig(git_workflow=True)
        msg = lib.criterion_commit_message(cfg, "SA-9", "- [ ] add foo")
        self.assertEqual(msg, "ticket/SA-9: add foo")


class CriterionFrameRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_new_fields_default_none_and_persist(self):
        frame = lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do x",
            plan_context="",
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
        )
        self.assertIsNone(frame.base_commit)
        self.assertIsNone(frame.commit_sha)

        frame.base_commit = "abc123"
        frame.commit_sha = "def456"
        lib.save_stack([frame])
        loaded = lib.load_stack()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].base_commit, "abc123")
        self.assertEqual(loaded[0].commit_sha, "def456")

    def test_old_stack_file_without_new_fields_loads(self):
        old_entry = {
            "ticket": "SA-1",
            "criterion": "- [ ] x",
            "plan_context": "",
            "test_files": None,
            "test_names": None,
            "status": "pending",
            "origin": "ticket",
            "verification": "test",
            "existing_test_refs": [],
            "unconfirmed_tests": [],
        }
        lib.CRITERIA_STACK_FILE.write_text(
            json.dumps([old_entry]) + "\n", encoding="utf-8"
        )
        loaded = lib.load_stack()
        self.assertEqual(len(loaded), 1)
        self.assertIsNone(loaded[0].base_commit)
        self.assertIsNone(loaded[0].commit_sha)


class GitHelperTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        init_git_repo(self.root)
        write_commit(self.root, "README.md", "hi\n", "init")
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_is_repo_and_head(self):
        self.assertTrue(lib.git_is_repo())
        head = lib.git_current_head()
        self.assertEqual(len(head), 40)

    def test_dirty_and_status(self):
        self.assertFalse(lib.git_is_dirty())
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        self.assertTrue(lib.git_is_dirty())
        self.assertIn("new.txt", lib.git_status_porcelain())

    def test_user_is_dirty_ignores_pipeline_managed_files(self):
        self.assertFalse(lib.git_user_is_dirty())
        # Pipeline state files and .gitignore don't count as user work.
        lib.CRITERIA_STACK_FILE.write_text("[]\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(
            ".criteria-stack.json\n", encoding="utf-8"
        )
        self.assertFalse(lib.git_user_is_dirty())
        # A real user file does.
        (self.root / "src.rs").write_text("fn main(){}", encoding="utf-8")
        self.assertTrue(lib.git_user_is_dirty())

    def test_branch_exists_create_checkout(self):
        self.assertFalse(lib.git_branch_exists("ticket/SA-1"))
        lib.git_create_branch("ticket/SA-1")
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
        self.assertEqual(lib.git_current_branch(), "ticket/SA-1")
        lib.git_checkout("main")
        self.assertEqual(lib.git_current_branch(), "main")

    def test_commit_returns_none_on_empty_stage(self):
        sha = lib.git_commit("nothing to stage")
        self.assertIsNone(sha)

    def test_commit_returns_sha_when_changes(self):
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        sha = lib.git_commit("add a")
        self.assertIsNotNone(sha)
        self.assertEqual(len(sha), 40)

    def test_reset_hard_rolls_back(self):
        base = lib.git_current_head()
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        lib.git_commit("add b")
        self.assertTrue((self.root / "b.txt").exists())
        lib.git_reset_hard(base)
        self.assertFalse((self.root / "b.txt").exists())


class EnsureGitignoreTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        init_git_repo(self.root)
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_creates_gitignore_with_pipeline_entries(self):
        lib.ensure_gitignore_entries()
        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".criteria-stack.json", text)
        self.assertIn(".pipeline-git-state.json", text)

    def test_idempotent_does_not_duplicate(self):
        lib.ensure_gitignore_entries()
        before = (self.root / ".gitignore").read_text(encoding="utf-8")
        lib.ensure_gitignore_entries()
        after = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_noop_outside_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                before = list(Path(d).iterdir())
                lib.ensure_gitignore_entries()
                after = list(Path(d).iterdir())
                self.assertEqual(before, after)
            finally:
                os.chdir(cwd)


class GitStateSidecarTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_record_lookup_clear(self):
        self.assertEqual(lib.lookup_git_base_branch("SA-1"), None)
        lib.record_git_base_branch("SA-1", "main")
        self.assertEqual(lib.lookup_git_base_branch("SA-1"), "main")
        lib.record_git_base_branch("SA-2", "dev")
        self.assertEqual(lib.load_git_state(), {"SA-1": "main", "SA-2": "dev"})
        lib.clear_git_base_branch("SA-1")
        self.assertIsNone(lib.lookup_git_base_branch("SA-1"))
        self.assertEqual(lib.lookup_git_base_branch("SA-2"), "dev")


class CommitCriterionTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        init_git_repo(self.root)
        write_commit(self.root, "README.md", "hi\n", "init")
        os.chdir(self.root)
        self.cfg = lib.GitConfig(git_workflow=True)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_commits_changes(self):
        (self.root / "src.rs").write_text("fn main() {}", encoding="utf-8")
        sha = lib.commit_criterion(self.cfg, "SA-1", "- [ ] add main")
        self.assertIsNotNone(sha)
        r = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(r.stdout.strip(), "ticket/SA-1: add main")

    def test_returns_none_on_empty_diff(self):
        sha = lib.commit_criterion(self.cfg, "SA-1", "- [ ] nothing")
        self.assertIsNone(sha)

    def test_ignores_gitignored_pipeline_state(self):
        # Simulate a pipeline state file present in the worktree.
        lib.CRITERIA_STACK_FILE.write_text("[]\n", encoding="utf-8")
        # Make sure it's gitignored so commit_criterion doesn't stage it.
        (self.root / ".gitignore").write_text(
            ".criteria-stack.json\n", encoding="utf-8"
        )
        (self.root / "src.rs").write_text("x", encoding="utf-8")
        lib.commit_criterion(self.cfg, "SA-1", "- [ ] add src")
        r = subprocess.run(
            ["git", "show", "--stat", "--format=", "HEAD"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(".criteria-stack.json", r.stdout)
        self.assertIn("src.rs", r.stdout)


class PostValidateMergeTests(unittest.TestCase):
    """Layer 3 Tier 1: local merge of ticket/<id> back to base."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        init_git_repo(self.root)
        write_commit(self.root, "README.md", "hi\n", "init")
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_merges_ticket_branch_into_base_and_deletes_branch(self):
        base = lib.git_current_branch()  # master/main
        lib.git_create_branch("ticket/SA-1")
        (self.root / "feat.txt").write_text("feat\n", encoding="utf-8")
        lib.git_commit("ticket/SA-1: feat")
        lib.record_git_base_branch("SA-1", base)

        cfg = lib.GitConfig(git_workflow=True, git_merge_on_validate=True)
        lib.post_validate_git(cfg, "SA-1")

        # Back on base, branch gone, feat present.
        self.assertEqual(lib.git_current_branch(), base)
        self.assertFalse(lib.git_branch_exists("ticket/SA-1"))
        self.assertTrue((self.root / "feat.txt").exists())
        self.assertIsNone(lib.lookup_git_base_branch("SA-1"))

    def test_noop_when_workflow_off(self):
        lib.git_create_branch("ticket/SA-2")
        (self.root / "x.txt").write_text("x\n", encoding="utf-8")
        lib.git_commit("x")
        cfg = lib.GitConfig(git_workflow=False)
        lib.post_validate_git(cfg, "SA-2")
        # Still on the ticket branch, nothing merged.
        self.assertEqual(lib.git_current_branch(), "ticket/SA-2")

    def test_no_merge_when_disabled_leaves_branch(self):
        base = lib.git_current_branch()
        lib.git_create_branch("ticket/SA-3")
        (self.root / "y.txt").write_text("y\n", encoding="utf-8")
        lib.git_commit("y")
        lib.record_git_base_branch("SA-3", base)
        cfg = lib.GitConfig(git_workflow=True, git_merge_on_validate=False)
        lib.post_validate_git(cfg, "SA-3")
        self.assertEqual(lib.git_current_branch(), "ticket/SA-3")
        self.assertTrue(lib.git_branch_exists("ticket/SA-3"))

    def test_missing_branch_is_noop(self):
        cfg = lib.GitConfig(git_workflow=True, git_merge_on_validate=True)
        # No branch created - should log and return, not raise.
        lib.post_validate_git(cfg, "SA-999")


class LoadPipelineConfigAllowsGitKeysTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        init_git_repo(Path(self._tmp.name))

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_git_keys_accepted(self):
        # Provide a toolchain command + git keys so the unknown-key
        # check sees only allowed extras.
        (Path(self._tmp.name) / "cfg.toml").write_text(
            'test_cmd = "pytest"\n'
            "git_workflow = true\n"
            "git_merge_on_validate = true\n"
            'smoke_cmd = "echo ok"\n'
            "[step_models]\n"
            'review = "opencode:x"\n',
            encoding="utf-8",
        )
        # Should not die on unknown keys.
        cmds = lib.load_pipeline_config(Path(self._tmp.name) / "cfg.toml")
        self.assertEqual(cmds["test_cmd"], "pytest")


class ResetWorkflowTests(unittest.TestCase):
    """reset-workflow: revert to base + delete ticket branch + clear state."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        init_git_repo(self.root)
        write_commit(self.root, "README.md", "hi\n", "init")
        os.chdir(self.root)
        self.base = lib.git_current_branch()
        self.cfg = lib.GitConfig(git_workflow=True, git_merge_on_validate=True)
        # Enable git_workflow in the repo's own config so reset-workflow's
        # default --config picks it up.
        (self.root / lib.PIPELINE_CONFIG_FILE.name).write_text(
            'test_cmd = "true"\ngit_workflow = true\n', encoding="utf-8"
        )
        # Stand up a ticket branch as push-ticket would.
        lib.git_create_branch(lib.ticket_branch_name(self.cfg, "SA-1"))
        lib.record_git_base_branch("SA-1", self.base)
        # A committed criterion (what next-step's POP would leave).
        (self.root / "feat.txt").write_text("feat\n", encoding="utf-8")
        lib.git_commit("ticket/SA-1: feat")
        # A stack frame so _identify_ticket finds SA-1.
        lib.save_stack(
            [
                lib.CriterionFrame(
                    ticket="SA-1",
                    criterion="- [ ] feat",
                    plan_context="",
                    test_files=None,
                    test_names=None,
                    status="done",
                    origin="ticket",
                )
            ]
        )

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _run(self, argv):
        import importlib
        from ticket_pipeline import reset_workflow

        importlib.reload(reset_workflow)
        sys.argv = ["reset-workflow"] + argv
        try:
            reset_workflow.main()
            return 0
        except SystemExit as e:
            return e.code

    def test_dry_run_changes_nothing(self):
        code = self._run(["--log-level", "warning"])
        self.assertEqual(code, 0)
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
        self.assertEqual(lib.git_current_branch(), "ticket/SA-1")
        self.assertTrue(lib.CRITERIA_STACK_FILE.is_file())
        self.assertIsNotNone(lib.lookup_git_base_branch("SA-1"))

    def test_yes_reverts_and_deletes_branch_and_clears_state(self):
        code = self._run(["--yes", "--log-level", "warning"])
        self.assertEqual(code, 0)
        self.assertEqual(lib.git_current_branch(), self.base)
        self.assertFalse(lib.git_branch_exists("ticket/SA-1"))
        self.assertFalse(lib.CRITERIA_STACK_FILE.is_file())
        self.assertIsNone(lib.lookup_git_base_branch("SA-1"))
        # The ticket branch's committed work is gone with the branch.
        self.assertFalse((self.root / "feat.txt").exists())

    def test_keep_branch_leaves_branch_but_still_reverts(self):
        code = self._run(["--yes", "--keep-branch", "--log-level", "warning"])
        self.assertEqual(code, 0)
        self.assertEqual(lib.git_current_branch(), self.base)
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
        # State still cleared.
        self.assertFalse(lib.CRITERIA_STACK_FILE.is_file())

    def test_keep_stack_preserves_stack(self):
        code = self._run(["--yes", "--keep-stack", "--log-level", "warning"])
        self.assertEqual(code, 0)
        self.assertTrue(lib.CRITERIA_STACK_FILE.is_file())
        self.assertFalse(lib.git_branch_exists("ticket/SA-1"))

    def test_refuses_on_dirty_tree(self):
        (self.root / "uncommitted.txt").write_text("x", encoding="utf-8")
        code = self._run(["--yes", "--log-level", "warning"])
        self.assertNotEqual(code, 0)
        # Nothing happened.
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
        self.assertEqual(lib.git_current_branch(), "ticket/SA-1")

    def test_workflow_off_skips_git_and_clears_state(self):
        # Switch off git_workflow: no branch teardown, just file cleanup.
        cfg_off = lib.GitConfig(git_workflow=False)
        # Simulate by directly calling with workflow off - reload won't
        # change the config file; instead exercise the off-path via the
        # command using a config file.
        off_cfg = Path(self.root) / "off.toml"
        off_cfg.write_text('test_cmd = "true"\n', encoding="utf-8")
        import importlib
        from ticket_pipeline import reset_workflow

        importlib.reload(reset_workflow)
        sys.argv = [
            "reset-workflow",
            "--yes",
            "--config",
            str(off_cfg),
            "--log-level",
            "warning",
        ]
        reset_workflow.main()
        # Branch untouched, stack cleared.
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
        self.assertFalse(lib.CRITERIA_STACK_FILE.is_file())

    def test_identify_ticket_from_branch_when_stack_empty(self):
        # Drop the stack; current branch is ticket/SA-1 -> id parsed from it.
        lib.CRITERIA_STACK_FILE.unlink()
        code = self._run(["--yes", "--log-level", "warning"])
        self.assertEqual(code, 0)
        self.assertFalse(lib.git_branch_exists("ticket/SA-1"))
        self.assertEqual(lib.git_current_branch(), self.base)

    def test_no_ticket_identifiable_clears_state_only(self):
        # On base branch, empty stack, empty sidecar, empty branch prefix
        # match -> no git steps, just cleanup.
        lib.git_checkout(self.base)
        lib.CRITERIA_STACK_FILE.unlink()
        lib.clear_git_base_branch("SA-1")
        # Re-add a stack so there's something to clear.
        lib.save_stack(
            [
                lib.CriterionFrame(
                    ticket="SA-1",
                    criterion="- [ ] x",
                    plan_context="",
                    test_files=None,
                    test_names=None,
                    status="pending",
                    origin="ticket",
                )
            ]
        )
        # Ticket branch still exists but we're not on it and stack says
        # SA-1 -> it WILL identify SA-1 from stack. Force the no-identify
        # path by clearing the stack too and removing the branch.
        lib.CRITERIA_STACK_FILE.unlink()
        r = lib._git("branch", "-D", "ticket/SA-1")
        # Now nothing identifies a ticket.
        code = self._run(["--yes", "--log-level", "warning"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
