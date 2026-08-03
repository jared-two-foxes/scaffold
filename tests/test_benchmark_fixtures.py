"""Tests for the benchmark fixtures module."""

import json
import tempfile
import unittest
from pathlib import Path

from ticket_pipeline.benchmark.fixtures import (
    FixtureMeta,
    PlanningFixture,
    discover_fixtures,
    resolve_base_ref,
)


def _write_fixture(base: Path, meta: dict, ticket: str, expected: dict) -> Path:
    """Helper to write a minimal fixture directory."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "fixture.json").write_text(json.dumps(meta), encoding="utf-8")
    (base / "ticket.md").write_text(ticket, encoding="utf-8")
    (base / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    return base


class FixtureMetaTests(unittest.TestCase):
    def test_load_from_dict(self):
        d = {
            "fixture_version": 2,
            "category": "planning",
            "suite": "core",
            "case": "my-case",
            "target_repo": "owner/repo",
            "base_ref": "abc123",
            "case_type": "hidden-adjacent-obligation",
        }
        meta = FixtureMeta.from_dict(d)
        self.assertEqual(meta.fixture_version, 2)
        self.assertEqual(meta.category, "planning")
        self.assertEqual(meta.suite, "core")
        self.assertEqual(meta.case, "my-case")
        self.assertEqual(meta.base_ref, "abc123")
        self.assertEqual(meta.case_type, "hidden-adjacent-obligation")

    def test_defaults(self):
        d = {
            "category": "planning",
            "suite": "core",
            "case": "x",
            "target_repo": "o/r",
            "base_ref": "HEAD",
        }
        meta = FixtureMeta.from_dict(d)
        self.assertEqual(meta.fixture_version, 1)
        self.assertEqual(meta.case_type, "")


class PlanningFixtureLoadTests(unittest.TestCase):
    def test_load_complete_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            fixture_dir = Path(td)
            meta = {
                "fixture_version": 1,
                "category": "planning",
                "suite": "core",
                "case": "test-case",
                "target_repo": "owner/repo",
                "base_ref": "abc123",
            }
            ticket = "# My ticket\n\nDo the thing."
            expected = {
                "required_outcomes": [
                    {"id": "field-added", "critical": True, "description": "field is added"}
                ],
                "required_existing_paths": ["src/lib.rs"],
                "forbidden_paths": ["src/new_file.rs"],
                "already_satisfied_outcomes": [],
                "expected_strategy_by_outcome": {"field-added": "tdd"},
                "critical_false_work_patterns": ["bad_pattern.rs"],
            }
            _write_fixture(fixture_dir, meta, ticket, expected)

            fixture = PlanningFixture.load(fixture_dir)
            self.assertEqual(fixture.meta.case, "test-case")
            self.assertEqual(fixture.ticket_content, ticket)
            self.assertEqual(len(fixture.required_outcomes), 1)
            self.assertEqual(fixture.required_outcomes[0].id, "field-added")
            self.assertTrue(fixture.required_outcomes[0].critical)
            self.assertIn("src/lib.rs", fixture.required_existing_paths)
            self.assertIn("src/new_file.rs", fixture.forbidden_paths)
            self.assertEqual(fixture.expected_strategy_by_outcome["field-added"], "tdd")
            self.assertIn("bad_pattern.rs", fixture.critical_false_work_patterns)

    def test_missing_fixture_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                FixtureMeta.load(Path(td))

    def test_missing_expected_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            fixture_dir = Path(td)
            meta = {
                "fixture_version": 1,
                "category": "planning",
                "suite": "core",
                "case": "x",
                "target_repo": "o/r",
                "base_ref": "HEAD",
            }
            (fixture_dir / "fixture.json").write_text(json.dumps(meta), encoding="utf-8")
            (fixture_dir / "ticket.md").write_text("ticket", encoding="utf-8")
            # No expected.json
            with self.assertRaises(FileNotFoundError):
                PlanningFixture.load(fixture_dir)


class DiscoverFixturesTests(unittest.TestCase):
    def test_finds_fixtures_in_suite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Create two fixtures
            for case in ("case-a", "case-b"):
                d = root / "planning" / "core" / case
                d.mkdir(parents=True)
                (d / "fixture.json").write_text("{}", encoding="utf-8")

            # Create a non-fixture dir (no fixture.json)
            (root / "planning" / "core" / "not-a-fixture").mkdir()

            results = discover_fixtures(root, "planning", "core")
            names = [p.name for p in results]
            self.assertIn("case-a", names)
            self.assertIn("case-b", names)
            self.assertNotIn("not-a-fixture", names)

    def test_missing_suite_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            results = discover_fixtures(Path(td), "planning", "nonexistent")
            self.assertEqual(results, [])


class ResolveBaseRefTests(unittest.TestCase):
    def test_override_takes_priority(self):
        with tempfile.TemporaryDirectory() as td:
            ref = resolve_base_ref(Path(td), override="my-override-ref")
            self.assertEqual(ref, "my-override-ref")

    def test_reads_base_ref_from_fixture_json(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "fixture.json").write_text(
                json.dumps({"base_ref": "pinned-commit-sha"}), encoding="utf-8"
            )
            ref = resolve_base_ref(d)
            self.assertEqual(ref, "pinned-commit-sha")

    def test_missing_fixture_json_returns_head(self):
        with tempfile.TemporaryDirectory() as td:
            ref = resolve_base_ref(Path(td))
            self.assertEqual(ref, "HEAD")

    def test_missing_base_ref_in_json_returns_head(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "fixture.json").write_text(json.dumps({"other_key": "x"}), encoding="utf-8")
            ref = resolve_base_ref(d)
            self.assertEqual(ref, "HEAD")


if __name__ == "__main__":
    unittest.main()
