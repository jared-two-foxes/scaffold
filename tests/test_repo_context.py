import sys
import tempfile
import types
import unittest
from pathlib import Path

# Stubbed under its fully-qualified name so pipeline_lib's `from .render
# import render_markdown` picks this up instead of the real module -
# keeps this test independent of whether `rich` (render.py's real
# dependency) is installed, and avoids a real console setup as a side
# effect of importing pipeline_lib. Must be registered before
# ticket_pipeline.lib.pipeline_lib is imported below.
render_stub = types.ModuleType("ticket_pipeline.lib.render")
render_stub.render_markdown = lambda _text: None
sys.modules.setdefault("ticket_pipeline.lib.render", render_stub)

from ticket_pipeline.lib import pipeline_lib
from ticket_pipeline.lib import repo_context


class TicketEvidenceTokenTests(unittest.TestCase):
    def test_extracts_code_like_tokens_and_filters_commands(self):
        ticket = """
        ## Acceptance Criteria
        - [ ] `POSTMARK_SIGNING_SECRET` env var is parsed into
              `EmailConfig.postmark_signing_secret: Option<String>`.
        - [ ] Update `libs/app/src/email_config.rs`.
        - [ ] `cargo test -p app` passes.
        """

        tokens = repo_context.extract_ticket_evidence_tokens(ticket)

        self.assertIn("POSTMARK_SIGNING_SECRET", tokens)
        self.assertIn("libs/app/src/email_config.rs", tokens)
        self.assertIn("EmailConfig.postmark_signing_secret", tokens)
        self.assertIn("postmark_signing_secret", tokens)
        self.assertNotIn("cargo test -p app", tokens)
        self.assertNotIn("cargo", tokens)
        self.assertNotIn("test", tokens)

    def test_extract_caps_searched_tokens(self):
        ticket = " ".join(f"TOKEN_{i}" for i in range(20))

        tokens = repo_context.extract_ticket_evidence_tokens(ticket, max_tokens=12)

        self.assertEqual(12, len(tokens))
        self.assertEqual("TOKEN_0", tokens[0])
        self.assertEqual("TOKEN_11", tokens[-1])


class TicketEvidenceSeedTests(unittest.TestCase):
    def test_seed_keeps_matches_and_env_no_matches_but_omits_symbol_no_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "target").mkdir()
            (root / "src" / "config.rs").write_text(
                "struct RateLimitConfig {\n"
                "    webhook_retry_rate_limit: u32,\n"
                "}\n"
                "const WEBHOOK_RETRY_RATE_LIMIT: &str = \"WEBHOOK_RETRY_RATE_LIMIT\";\n",
                encoding="utf-8",
            )
            (root / "target" / "ignored.rs").write_text(
                "POSTMARK_SIGNING_SECRET should not be searched in target\n",
                encoding="utf-8",
            )
            ticket = """
            - [ ] `WEBHOOK_RETRY_RATE_LIMIT` configures
                  `RateLimitConfig.webhook_retry_rate_limit`.
            - [ ] `POSTMARK_SIGNING_SECRET` is not present yet.
            - [ ] MissingSymbol should not be rendered if it has no matches.
            """

            seed = repo_context.gather_ticket_evidence_seed(ticket, root)
            rendered = repo_context.render_ticket_evidence_seed_block(seed)

        self.assertIn("## Ticket Evidence Seed", rendered)
        self.assertIn("WEBHOOK_RETRY_RATE_LIMIT", rendered)
        self.assertIn("src/config.rs", rendered)
        self.assertIn("POSTMARK_SIGNING_SECRET", rendered)
        self.assertIn("(no matches", rendered)
        self.assertNotIn("target/ignored.rs", rendered)
        self.assertNotIn("MissingSymbol", rendered)

    def test_render_respects_character_budget(self):
        entries = [
            repo_context.TicketEvidenceEntry(
                token=f"TOKEN_{i}",
                kind="env",
                result="x" * 80,
                has_matches=True,
            )
            for i in range(5)
        ]
        seed = repo_context.TicketEvidenceSeed(
            entries=entries,
            searched_tokens=[entry.token for entry in entries],
        )

        rendered = repo_context.render_ticket_evidence_seed_block(seed, max_chars=220)

        self.assertIn("ticket evidence seed truncated", rendered)
        self.assertLessEqual(len(rendered), 320)


class PlanNarrowPromptTests(unittest.TestCase):
    def test_plan_narrow_prompt_includes_ticket_evidence_before_tool_guidance(self):
        ticket = """
        ## Acceptance Criteria
        - [ ] `WEBHOOK_RETRY_RATE_LIMIT` env var is parsed.
        """

        prompt = pipeline_lib.build_plan_narrow_prompt(ticket)

        seed_pos = prompt.index("## Ticket Evidence Seed")
        guidance_pos = prompt.index("Use read_file/list_dir/search_files for anything else")
        self.assertLess(seed_pos, guidance_pos)
        self.assertIn("preliminary host-side search results", prompt)


if __name__ == "__main__":
    unittest.main()
