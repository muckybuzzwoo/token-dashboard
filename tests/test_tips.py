import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_dashboard.db import init_db, connect
from token_dashboard.tips import (
    cache_discipline_tips, repeated_target_tips, right_size_tips,
    outlier_tips, cross_workspace_tips, all_tips, dismiss_tip,
    skill_listing_budget_tips, claude_md_size_tips,
)


def _assert_tip_shape(test, tip):
    """Every tip must carry the new structural fields."""
    test.assertIn("severity", tip)
    test.assertIn(tip["severity"], {"info", "warning", "cost"})
    test.assertIsInstance(tip.get("links"), list)
    test.assertIn("estimated_savings_usd", tip)


class CacheTipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _ins(self, ts, project, cache_read, cache_create, session="s"):
        with connect(self.db) as c:
            c.execute("""INSERT INTO messages (uuid, session_id, project_slug, type, timestamp,
                model, input_tokens, output_tokens, cache_read_tokens,
                cache_create_5m_tokens, cache_create_1h_tokens) VALUES
                (?, ?, ?, 'assistant', ?, 'claude-opus-4-7', 100, 100, ?, ?, 0)""",
                (f"uuid-{ts}-{session}", session, project, ts, cache_read, cache_create))
            c.commit()

    def test_low_cache_hit_emits_tip_with_session_link(self):
        self._ins("2026-04-15T00:00:00Z", "projX", 10, 1_000_000, session="sess-worst")
        tips = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        cache_tips = [t for t in tips if t["category"] == "cache"]
        self.assertTrue(cache_tips)
        t = cache_tips[0]
        _assert_tip_shape(self, t)
        self.assertEqual(t["severity"], "warning")
        hrefs = [l["href"] for l in t["links"]]
        self.assertIn("#/sessions/sess-worst", hrefs)
        self.assertTrue(any(h.startswith("https://") for h in hrefs))

    def test_healthy_cache_no_tip(self):
        for i in range(10):
            self._ins(f"2026-04-15T00:00:0{i}Z", "projY", 1_000_000, 50)
        tips = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertFalse(any(t["category"] == "cache" for t in tips))


class RepeatTipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model) VALUES ('m1','s1','p','assistant','2026-04-15T00:00:00Z','claude-opus-4-7')")
            for _ in range(15):
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) VALUES ('m1','s1','p','Read','src/Root.tsx','2026-04-15T00:00:00Z',0)")
            for _ in range(20):
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) VALUES ('m1','s1','p','Bash','npm run lint','2026-04-15T00:00:00Z',0)")
            c.commit()

    def test_repeated_tips_carry_session_link(self):
        tips = repeated_target_tips(self.db, today_iso="2026-04-19T00:00:00")
        by_cat = {t["category"]: t for t in tips}
        self.assertIn("repeat-file", by_cat)
        self.assertIn("repeat-bash", by_cat)
        for t in (by_cat["repeat-file"], by_cat["repeat-bash"]):
            _assert_tip_shape(self, t)
            hrefs = [l["href"] for l in t["links"]]
            self.assertIn("#/sessions/s1", hrefs)


class RightSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def test_short_opus_turns_flagged_with_cost_severity(self):
        with connect(self.db) as c:
            for i in range(10):
                c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, is_sidechain) VALUES (?, 's','p','assistant','2026-04-18T00:00:00Z','claude-opus-4-7', 1000000, 200, 0, 0, 0, 0)", (f"a{i}",))
            c.commit()
        tips = right_size_tips(self.db, today_iso="2026-04-19T00:00:00")
        rs = [t for t in tips if t["category"] == "right-size"]
        self.assertTrue(rs)
        _assert_tip_shape(self, rs[0])
        self.assertEqual(rs[0]["severity"], "cost")
        self.assertIsNotNone(rs[0]["estimated_savings_usd"])
        self.assertGreater(rs[0]["estimated_savings_usd"], 0)


class OutlierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def test_tool_bloat_threshold_at_10k_not_50k(self):
        # Five 12k-result rows should now trigger (info severity), 5x50k → warning.
        with connect(self.db) as c:
            for i in range(6):
                c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) VALUES (?, 'sA','p','user','2026-04-18T00:00:00Z')", (f"u{i}",))
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, timestamp, is_error) VALUES (?, 'sA','p','_tool_result','tu',12000,'2026-04-18T00:00:00Z',0)", (f"u{i}",))
            c.commit()
        tips = outlier_tips(self.db, today_iso="2026-04-19T00:00:00")
        bloat = [t for t in tips if t["category"] == "tool-bloat"]
        self.assertTrue(bloat)
        _assert_tip_shape(self, bloat[0])
        self.assertEqual(bloat[0]["severity"], "info")
        hrefs = [l["href"] for l in bloat[0]["links"]]
        self.assertIn("#/sessions/sA", hrefs)

    def test_tool_bloat_severity_escalates_above_50k(self):
        with connect(self.db) as c:
            for i in range(6):
                c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) VALUES (?, 'sB','p','user','2026-04-18T00:00:00Z')", (f"u{i}",))
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, timestamp, is_error) VALUES (?, 'sB','p','_tool_result','tu',80000,'2026-04-18T00:00:00Z',0)", (f"u{i}",))
            c.commit()
        tips = outlier_tips(self.db, today_iso="2026-04-19T00:00:00")
        bloat = [t for t in tips if t["category"] == "tool-bloat"]
        self.assertTrue(bloat)
        self.assertEqual(bloat[0]["severity"], "warning")


class CrossWorkspaceTipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "cw.db")
        init_db(self.db)
        slug_a = "C--Users-a-projects-ProjA"
        slug_b = "C--Users-a-projects-ProjB"
        cwd_a = r"C:\Users\a\projects\ProjA"
        cwd_b = r"C:\Users\a\projects\ProjB"
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, timestamp, model) VALUES "
                "('m1','s1',?,?,'assistant',0,'2026-05-15T00:00:00Z','claude-opus-4-7')",
                (slug_a, cwd_a),
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, timestamp, model) VALUES "
                "('m2','s2',?,?,'assistant',0,'2026-05-15T00:00:00Z','claude-opus-4-7')",
                (slug_b, cwd_b),
            )
            for i in range(60):
                c.execute(
                    "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                    "tool_name, target, timestamp, is_error) VALUES "
                    "('m1','s1',?,'Read',?,?,0)",
                    (slug_a, cwd_b + r"\spec.md", f"2026-05-15T00:0{i//10}:0{i%10}Z"),
                )
            c.commit()

    def test_high_cross_workspace_activity_emits_tip(self):
        tips = cross_workspace_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(any(t["category"] == "cross-workspace" for t in tips))
        tip = [t for t in tips if t["category"] == "cross-workspace"][0]
        self.assertIn("ProjA", tip["title"])
        self.assertIn("ProjB", tip["title"])

    def test_low_activity_under_threshold_no_tip(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "small.db")
        init_db(db)
        slug = "C--Users-a-projects-ProjA"
        cwd  = r"C:\Users\a\projects\ProjA"
        with connect(db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, timestamp, model) VALUES "
                "('m1','s1',?,?,'assistant',0,'2026-05-15T00:00:00Z','claude-opus-4-7')",
                (slug, cwd),
            )
            for i in range(3):
                c.execute(
                    "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                    "tool_name, target, timestamp, is_error) VALUES "
                    "('m1','s1',?,'Read',?,?,0)",
                    (slug, r"C:\Users\a\projects\Other\file.md", f"2026-05-15T00:00:0{i}Z"),
                )
            c.commit()
        tips = cross_workspace_tips(db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(any(t["category"] == "cross-workspace" for t in tips))


class DismissTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('m','s','projZ','assistant','2026-04-15T00:00:00Z','claude-opus-4-7', 100, 100, 10, 1000000, 0)")
            c.commit()

    def test_dismissed_tip_doesnt_reappear(self):
        tips_before = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertTrue(tips_before)
        dismiss_tip(self.db, tips_before[0]["key"])
        tips_after = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertFalse(tips_after)


def _make_skill(root: Path, name: str, description: str, body: str = "Body text.\n",
                extra_frontmatter: str = "") -> Path:
    """Write a SKILL.md with frontmatter under root/skills/<name>/SKILL.md."""
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    extra = f"\n{extra_frontmatter}" if extra_frontmatter else ""
    md.write_text(
        f"---\nname: {name}\ndescription: {description}{extra}\n---\n\n{body}",
        encoding="utf-8",
    )
    return md


class SkillListingBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)
        self.skills_root = self.tmp / "fake_home" / ".claude"
        self.skills_root.mkdir(parents=True)

    def _patch_roots(self):
        # cached_catalog reads active roots through default_roots(); patch + reset cache.
        from token_dashboard import skills as s
        return mock.patch.object(
            s, "default_roots", return_value=[self.skills_root / "skills"]
        ), s

    def test_under_budget_no_tip(self):
        _make_skill(self.skills_root, "tiny", "short desc")
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertEqual(tips, [])

    def test_over_budget_emits_tip_with_skills_link(self):
        # Fabricate enough description chars to exceed an 800-char budget.
        long_desc = "x" * 300
        for i in range(5):
            _make_skill(self.skills_root, f"skill{i}", long_desc)
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=800,
            )
        self.assertTrue(tips)
        t = tips[0]
        _assert_tip_shape(self, t)
        self.assertEqual(t["category"], "skill-budget")
        self.assertEqual(t["severity"], "warning")
        hrefs = [l["href"] for l in t["links"]]
        self.assertIn("#/skills", hrefs)

    def test_user_invocable_only_skill_description_does_not_count(self):
        long_desc = "x" * 300
        _make_skill(self.skills_root, "visible", long_desc)
        _make_skill(self.skills_root, "manual", long_desc)
        settings = self.tmp / "settings.json"
        settings.write_text(
            '{"skillOverrides": {"manual": "user-invocable-only"}}',
            encoding="utf-8",
        )
        patch_ctx, s = self._patch_roots()
        from token_dashboard import tips as tips_mod
        with patch_ctx, mock.patch.object(tips_mod, "_USER_SETTINGS_PATH", settings):
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=500,
            )
        self.assertEqual(tips, [])

    def test_disable_model_invocation_skill_description_does_not_count(self):
        long_desc = "x" * 300
        _make_skill(self.skills_root, "visible", long_desc)
        _make_skill(
            self.skills_root,
            "manual",
            long_desc,
            extra_frontmatter="disable-model-invocation: true",
        )
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=500,
            )
        self.assertEqual(tips, [])


class ClaudeMdSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()

    def _seed_messages(self, cwd: str, n: int = 10):
        with connect(self.db) as c:
            for i in range(n):
                c.execute(
                    """INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd)
                       VALUES (?, 's','p','user','2026-04-15T00:00:00Z', ?)""",
                    (f"u{i}-{cwd}", cwd),
                )
            c.commit()

    def test_small_claude_md_no_tip(self):
        (self.proj / "CLAUDE.md").write_text("# small\n" + ("line\n" * 30), encoding="utf-8")
        self._seed_messages(str(self.proj))
        tips = claude_md_size_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertFalse(any(t["category"] == "claude-md-size" for t in tips))

    def test_large_claude_md_emits_tip(self):
        (self.proj / "CLAUDE.md").write_text("# big\n" + ("line\n" * 300), encoding="utf-8")
        self._seed_messages(str(self.proj))
        tips = claude_md_size_tips(self.db, today_iso="2026-04-19T00:00:00")
        big = [t for t in tips if t["category"] == "claude-md-size"]
        self.assertTrue(big)
        _assert_tip_shape(self, big[0])
        self.assertEqual(big[0]["severity"], "info")
        # Drill-down link should be the Anthropic docs.
        self.assertTrue(any(l["href"].startswith("https://") for l in big[0]["links"]))


if __name__ == "__main__":
    unittest.main()
