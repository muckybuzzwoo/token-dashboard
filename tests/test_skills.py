import os
import tempfile
import unittest
from pathlib import Path

from token_dashboard.skills import (
    scan_catalog,
    _slugs_for,
    _project_skill_roots_from_cwds,
    cached_catalog,
    _cache,
)
from token_dashboard.db import connect, init_db


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_user_skill(self):
        _write(self.tmp / "skills" / "frontend-design" / "SKILL.md", "x" * 400)
        cat = scan_catalog([self.tmp / "skills"])
        self.assertIn("frontend-design", cat)
        self.assertEqual(cat["frontend-design"]["chars"], 400)
        self.assertEqual(cat["frontend-design"]["tokens"], 100)

    def test_plugin_skill_registers_both_slugs(self):
        p = self.tmp / "plugins" / "marketplaces" / "official" / "plugins" / "superpowers" / "skills" / "brainstorming" / "SKILL.md"
        _write(p, "y" * 800)
        cat = scan_catalog([self.tmp / "plugins"])
        self.assertIn("brainstorming", cat)
        self.assertIn("superpowers:brainstorming", cat)
        self.assertEqual(cat["brainstorming"]["tokens"], 200)
        self.assertEqual(cat["superpowers:brainstorming"]["tokens"], 200)

    def test_scheduled_task_skill(self):
        _write(self.tmp / "scheduled-tasks" / "morning-coffee" / "SKILL.md", "z" * 100)
        cat = scan_catalog([self.tmp / "scheduled-tasks"])
        self.assertIn("morning-coffee", cat)

    def test_nested_skills_skills_dedup_prefers_shallow(self):
        shallow = self.tmp / "skills" / "foo" / "SKILL.md"
        deep    = self.tmp / "skills" / "skills" / "foo" / "SKILL.md"
        _write(shallow, "s" * 100)
        _write(deep, "d" * 999)
        cat = scan_catalog([self.tmp / "skills"])
        # Shallow wins
        self.assertEqual(cat["foo"]["chars"], 100)

    def test_slugs_for_plugin_path(self):
        p = Path("plugins/marketplaces/x/plugins/superpowers/skills/brainstorming/SKILL.md")
        slugs = set(_slugs_for(p))
        # Both forms must be present; extra ancestor aliases (e.g. marketplace name) are harmless.
        self.assertIn("brainstorming", slugs)
        self.assertIn("superpowers:brainstorming", slugs)

    def test_slugs_for_cache_versioned_path(self):
        p = Path("plugins/cache/claude-plugins-official/superpowers/5.0.7/skills/brainstorming/SKILL.md")
        slugs = set(_slugs_for(p))
        self.assertIn("brainstorming", slugs)
        self.assertIn("superpowers:brainstorming", slugs)
        # version segment must NOT become a slug prefix
        self.assertNotIn("5.0.7:brainstorming", slugs)

    def test_slugs_for_user_skill(self):
        p = Path(".claude/skills/frontend-design/SKILL.md")
        self.assertEqual(_slugs_for(p), ["frontend-design"])

    def test_missing_skill_not_in_catalog(self):
        # No file written; lookup should return None (server surfaces as tokens_per_call: None)
        cat = scan_catalog([self.tmp / "skills"])
        self.assertNotIn("never-installed", cat)

    def test_project_local_skill_slug(self):
        _write(self.tmp / "proj" / ".claude" / "skills" / "my-skill" / "SKILL.md", "p" * 400)
        cat = scan_catalog([self.tmp / "proj" / ".claude" / "skills"])
        self.assertIn("my-skill", cat)
        self.assertEqual(cat["my-skill"]["tokens"], 100)

    def test_project_skill_roots_innermost_wins(self):
        outer = self.tmp / "outer" / ".claude" / "skills"
        inner = self.tmp / "outer" / "inner" / ".claude" / "skills"
        outer.mkdir(parents=True)
        inner.mkdir(parents=True)
        roots = _project_skill_roots_from_cwds([str(self.tmp / "outer" / "inner" / "src")])
        self.assertEqual(roots, [inner])

    def test_project_skill_roots_dedupes_across_cwds(self):
        root = self.tmp / "repo" / ".claude" / "skills"
        root.mkdir(parents=True)
        cwds = [
            str(self.tmp / "repo" / "src"),
            str(self.tmp / "repo" / "tests" / "unit"),
        ]
        roots = _project_skill_roots_from_cwds(cwds)
        self.assertEqual(roots, [root])

    def test_cached_catalog_includes_project_local_from_db(self):
        # Reset the module-level cache so this test doesn't inherit neighbour state.
        _cache["at"] = 0.0
        _cache["data"] = {}
        _cache["key"] = None

        project = self.tmp / "myrepo"
        _write(project / ".claude" / "skills" / "repo-skill" / "SKILL.md", "x" * 400)

        db_path = self.tmp / "t.db"
        init_db(db_path)
        with connect(db_path) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("u1", "s1", "myrepo", "user", "2026-04-23T00:00:00Z",
                 str(project / "src")),
            )
            c.commit()

        cat = cached_catalog(db_path)
        self.assertIn("repo-skill", cat)
        self.assertEqual(cat["repo-skill"]["tokens"], 100)


if __name__ == "__main__":
    unittest.main()
