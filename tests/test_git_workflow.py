import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pipeline.lib import pipeline_lib as lib


def init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
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
                'git_workflow = true\nbase_branch = "main"\nbranch_prefix = "t/"\n',
                encoding="utf-8",
            )
            cfg = lib.load_git_config(Path(d) / "cfg.toml")
            self.assertTrue(cfg.git_workflow)
            self.assertEqual(cfg.base_branch, "main")
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
        self.assertIsNone(frame.test_commit_sha)

        frame.base_commit = "abc123"
        frame.commit_sha = "def456"
        frame.test_commit_sha = "ghi789"
        lib.save_stack([frame])
        loaded = lib.load_stack()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].base_commit, "abc123")
        self.assertEqual(loaded[0].commit_sha, "def456")
        self.assertEqual(loaded[0].test_commit_sha, "ghi789")

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
        lib.CRITERIA_STACK_FILE.write_text(json.dumps([old_entry]) + "\n", encoding="utf-8")
        loaded = lib.load_stack()
        self.assertEqual(len(loaded), 1)
        self.assertIsNone(loaded[0].base_commit)
        self.assertIsNone(loaded[0].commit_sha)
        self.assertIsNone(loaded[0].test_commit_sha)

    def test_legacy_root_stack_file_migrates(self):
        old_entry = {
            "ticket": "SA-1",
            "criterion": "- [ ] x",
            "plan_context": "",
            "test_files": None,
            "test_names": None,
            "status": "pending",
            "origin": "ticket",
        }
        Path(".criteria-stack.json").write_text(json.dumps([old_entry]) + "\n", encoding="utf-8")
        loaded = lib.load_stack()
        self.assertEqual(len(loaded), 1)
        self.assertTrue(lib.CRITERIA_STACK_FILE.is_file())
        self.assertFalse(Path(".criteria-stack.json").exists())


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
        lib.CRITERIA_STACK_FILE.write_text("[]\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".criteria-stack.json\n", encoding="utf-8")
        self.assertFalse(lib.git_user_is_dirty())
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

    def test_changed_files_includes_clean_committed_ticket_branch(self):
        base_commit = lib.git_current_head()
        lib.git_create_branch("ticket/SA-1")
        write_commit(self.root, "committed.txt", "committed\n", "ticket work")
        lib.record_git_base_commit("SA-1", base_commit)

        self.assertEqual(
            lib.git_changed_files(lib.GitConfig(git_workflow=True), "SA-1"),
            ["committed.txt"],
        )

    def test_changed_files_includes_committed_and_dirty_ticket_work(self):
        base_commit = lib.git_current_head()
        lib.git_create_branch("ticket/SA-2")
        write_commit(self.root, "committed.txt", "committed\n", "ticket work")
        (self.root / "committed.txt").write_text("committed and staged\n", encoding="utf-8")
        subprocess.run(["git", "add", "committed.txt"], cwd=self.root, check=True)
        (self.root / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
        (self.root / ".scaffold" / "pipeline.md").parent.mkdir(exist_ok=True)
        (self.root / ".scaffold" / "pipeline.md").write_text("pipeline\n", encoding="utf-8")
        (self.root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        lib.record_git_base_commit("SA-2", base_commit)

        self.assertEqual(
            set(lib.git_changed_files(lib.GitConfig(git_workflow=True), "SA-2")),
            {"committed.txt", "unstaged.txt", "untracked.txt"},
        )

    def test_changed_files_without_git_workflow_retains_worktree_behavior(self):
        base_commit = lib.git_current_head()
        write_commit(self.root, "committed.txt", "committed\n", "committed work")
        (self.root / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
        (self.root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        self.assertEqual(
            set(lib.git_changed_files(lib.GitConfig(git_workflow=False), "SA-3")),
            {"unstaged.txt", "untracked.txt"},
        )
        self.assertNotIn("committed.txt", lib.git_changed_files())
        self.assertNotEqual(base_commit, lib.git_current_head())


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
        base_commit = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(lib.lookup_git_base_branch("SA-1"), None)
        self.assertIsNone(lib.lookup_git_base_commit("SA-1"))
        lib.record_git_base_branch("SA-1", "main")
        lib.record_git_base_commit("SA-1", base_commit)
        self.assertEqual(lib.lookup_git_base_branch("SA-1"), "main")
        self.assertEqual(lib.lookup_git_base_commit("SA-1"), base_commit)
        lib.record_git_base_branch("SA-2", "dev")
        self.assertEqual(
            lib.load_git_state(),
            {
                "SA-1": {"base_branch": "main", "base_commit": base_commit},
                "SA-2": {"base_branch": "dev", "base_commit": None},
            },
        )
        lib.clear_git_base_branch("SA-1")
        self.assertIsNone(lib.lookup_git_base_branch("SA-1"))
        self.assertIsNone(lib.lookup_git_base_commit("SA-1"))
        self.assertEqual(lib.lookup_git_base_branch("SA-2"), "dev")

    def test_legacy_root_git_state_file_migrates(self):
        base_commit = "fedcba9876543210fedcba9876543210fedcba98"
        Path(".pipeline-git-state.json").write_text(
            json.dumps({"SA-1": "main"}) + "\n", encoding="utf-8"
        )
        self.assertEqual(
            lib.load_git_state(),
            {"SA-1": {"base_branch": "main", "base_commit": None}},
        )
        lib.record_git_base_commit("SA-1", base_commit)
        self.assertEqual(lib.lookup_git_base_branch("SA-1"), "main")
        self.assertEqual(lib.lookup_git_base_commit("SA-1"), base_commit)
        self.assertEqual(
            lib.load_git_state()["SA-1"],
            {"base_branch": "main", "base_commit": base_commit},
        )
        self.assertTrue(lib.GIT_STATE_FILE.is_file())
        self.assertFalse(Path(".pipeline-git-state.json").exists())


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
        lib.CRITERIA_STACK_FILE.write_text("[]\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".criteria-stack.json\n", encoding="utf-8")
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


class PostValidateGitTests(unittest.TestCase):
    """post_validate_git leaves ticket branches intact unless configured otherwise."""

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

    def test_prepare_git_branch_records_exact_base_commit_for_handoff(self):
        from ticket_pipeline import push_ticket

        base_branch = lib.git_current_branch()
        exact_base_commit = lib.git_current_head()
        cfg = lib.GitConfig(
            git_workflow=True,
            pr_on_validate=True,
            forge="github",
        )

        push_ticket.prepare_git_branch("SA-5", cfg, force=False)

        self.assertEqual(lib.lookup_git_base_branch("SA-5"), base_branch)
        self.assertEqual(lib.lookup_git_base_commit("SA-5"), exact_base_commit)

        # Move the base branch after branch creation, then make ticket work.
        lib.git_checkout(base_branch)
        write_commit(self.root, "unrelated.txt", "unrelated\n", "unrelated base work")
        lib.git_checkout("ticket/SA-5")
        write_commit(self.root, "ticket.txt", "ticket\n", "ticket work")

        with (
            mock.patch.object(lib, "git_squash_branch") as squash,
            mock.patch.object(lib, "create_github_pr") as create_pr,
        ):
            lib.post_validate_git(cfg, "SA-5")

        squash.assert_called_once_with("ticket/SA-5", exact_base_commit, "ticket/SA-5")
        create_pr.assert_called_once_with(
            cfg, "SA-5", "ticket/SA-5", base_branch, None, None, force=True
        )

    def test_noop_when_workflow_off(self):
        lib.git_create_branch("ticket/SA-2")
        (self.root / "x.txt").write_text("x\n", encoding="utf-8")
        lib.git_commit("x")
        cfg = lib.GitConfig(git_workflow=False)
        lib.post_validate_git(cfg, "SA-2")
        self.assertEqual(lib.git_current_branch(), "ticket/SA-2")

    def test_no_merge_when_no_pr_configured_leaves_branch(self):
        base = lib.git_current_branch()
        lib.git_create_branch("ticket/SA-3")
        (self.root / "y.txt").write_text("y\n", encoding="utf-8")
        lib.git_commit("y")
        lib.record_git_base_branch("SA-3", base)
        cfg = lib.GitConfig(git_workflow=True)
        lib.post_validate_git(cfg, "SA-3")
        self.assertEqual(lib.git_current_branch(), "ticket/SA-3")
        self.assertTrue(lib.git_branch_exists("ticket/SA-3"))

    def test_pr_handoff_squashes_from_recorded_base_commit(self):
        base_branch = lib.git_current_branch()
        base_commit = lib.git_current_head()
        branch = "ticket/SA-4"
        lib.git_create_branch(branch)
        (self.root / "feature.txt").write_text("ticket work\n", encoding="utf-8")
        lib.git_commit("ticket work")
        lib.record_git_base_branch("SA-4", base_branch)
        lib.record_git_base_commit("SA-4", base_commit)
        cfg = lib.GitConfig(
            git_workflow=True,
            pr_on_validate=True,
            forge="github",
        )

        with (
            mock.patch.object(lib, "git_squash_branch") as squash,
            mock.patch.object(lib, "create_github_pr") as create_pr,
        ):
            lib.post_validate_git(cfg, "SA-4")

        squash.assert_called_once_with(branch, base_commit, "ticket/SA-4")
        create_pr.assert_called_once_with(cfg, "SA-4", branch, base_branch, None, None, force=True)


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
        (Path(self._tmp.name) / "cfg.toml").write_text(
            'test_cmd = "pytest"\n'
            "git_workflow = true\n"
            'smoke_cmd = "echo ok"\n'
            "[step_models]\n"
            'review = "opencode:x"\n',
            encoding="utf-8",
        )
        cmds = lib.load_pipeline_config(Path(self._tmp.name) / "cfg.toml")
        self.assertEqual(cmds["test_cmd"], "pytest")


class ResetPipelineTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _run(self, argv):
        import importlib

        from ticket_pipeline import reset_pipeline

        importlib.reload(reset_pipeline)
        sys.argv = ["reset-pipeline"] + argv
        try:
            reset_pipeline.main()
            return 0
        except SystemExit as e:
            return e.code

    def test_yes_clears_scaffold_scratch_files_and_preserves_config(self):
        scaffold = self.root / ".scaffold"
        scaffold.mkdir()
        lib.TICKET_FILE.write_text("ticket\n", encoding="utf-8")
        lib.PLAN_FILE.write_text("plan\n", encoding="utf-8")
        (scaffold / "ticket-review-123.md").write_text("review\n", encoding="utf-8")
        (scaffold / "ticket-proposed-123.md").write_text("proposed\n", encoding="utf-8")
        (self.root / ".ticket-review-123.md").write_text("root review\n", encoding="utf-8")
        config = self.root / ".dev-pipeline.toml"
        config.write_text('test_cmd = "true"\n', encoding="utf-8")

        code = self._run(["--yes", "--log-level", "warning"])

        self.assertEqual(code, 0)
        self.assertFalse(lib.TICKET_FILE.exists())
        self.assertFalse(lib.PLAN_FILE.exists())
        self.assertFalse((scaffold / "ticket-review-123.md").exists())
        self.assertFalse((scaffold / "ticket-proposed-123.md").exists())
        self.assertTrue((self.root / ".ticket-review-123.md").exists())
        self.assertTrue(config.exists())


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
        self.cfg = lib.GitConfig(git_workflow=True)
        (self.root / lib.PIPELINE_CONFIG_FILE.name).write_text(
            'test_cmd = "true"\ngit_workflow = true\n', encoding="utf-8"
        )
        lib.git_create_branch(lib.ticket_branch_name(self.cfg, "SA-1"))
        lib.record_git_base_branch("SA-1", self.base)
        (self.root / "feat.txt").write_text("feat\n", encoding="utf-8")
        lib.git_commit("ticket/SA-1: feat")
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
        self.assertFalse((self.root / "feat.txt").exists())

    def test_keep_branch_leaves_branch_but_still_reverts(self):
        code = self._run(["--yes", "--keep-branch", "--log-level", "warning"])
        self.assertEqual(code, 0)
        self.assertEqual(lib.git_current_branch(), self.base)
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
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
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
        self.assertEqual(lib.git_current_branch(), "ticket/SA-1")

    def test_workflow_off_skips_git_and_clears_state(self):
        lib.GitConfig(git_workflow=False)
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
        self.assertTrue(lib.git_branch_exists("ticket/SA-1"))
        self.assertFalse(lib.CRITERIA_STACK_FILE.is_file())

    def test_identify_ticket_from_branch_when_stack_empty(self):
        lib.CRITERIA_STACK_FILE.unlink()
        code = self._run(["--yes", "--log-level", "warning"])
        self.assertEqual(code, 0)
        self.assertFalse(lib.git_branch_exists("ticket/SA-1"))
        self.assertEqual(lib.git_current_branch(), self.base)

    def test_no_ticket_identifiable_clears_state_only(self):
        lib.git_checkout(self.base)
        lib.CRITERIA_STACK_FILE.unlink()
        lib.clear_git_base_branch("SA-1")
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
        lib.CRITERIA_STACK_FILE.unlink()
        lib._git("branch", "-D", "ticket/SA-1")
        code = self._run(["--yes", "--log-level", "warning"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
