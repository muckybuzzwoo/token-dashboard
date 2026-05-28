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
    dead_skills_tips, subagent_sprawl_tips,
    bash_bloat_tips, _has_output_limiter,
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


def _make_skill(root: Path, name: str, description: str, body: str = "Body text.\n") -> Path:
    """Write a SKILL.md with frontmatter under root/skills/<name>/SKILL.md."""
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )
    return md


def _make_plugin_skill(plugins_root: Path, plugin: str, name: str, description: str) -> Path:
    """Write a SKILL.md under a marketplaces-style plugin path:
       plugins_root/marketplaces/m/plugins/<plugin>/skills/<name>/SKILL.md
    """
    d = plugins_root / "marketplaces" / "m" / "plugins" / plugin / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody text.\n",
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
        # cached_catalog reads module-level _DEFAULT_ROOTS; patch + reset cache.
        from token_dashboard import skills as s
        return mock.patch.object(s, "_DEFAULT_ROOTS",
                                 [self.skills_root / "skills"]), s

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

    def test_plugin_skill_appears_once_in_candidates(self):
        """Regression: a plugin SKILL.md registers two slugs (bare + <plugin>:<bare>).
        The 'Least-recently-used candidates' list must dedupe by file path and
        prefer the plugin-qualified form for display.
        """
        long_desc = "y" * 400
        # One plugin skill with two slugs registered (bare + plugin form).
        _make_plugin_skill(
            self.skills_root / "plugins", "buzzwoo-ecom-shopware",
            "shopware-app-system", long_desc,
        )
        # Plus a few plain skills to push us over budget.
        for i in range(3):
            _make_skill(self.skills_root, f"filler{i}", long_desc)

        # Patch _DEFAULT_ROOTS to scan both the user-skills and plugins roots.
        from token_dashboard import skills as s
        with mock.patch.object(s, "_DEFAULT_ROOTS",
                               [self.skills_root / "skills",
                                self.skills_root / "plugins"]):
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=800,
            )

        self.assertTrue(tips)
        body = tips[0]["body"]
        # Plugin-qualified form should be the display label (not the bare form).
        self.assertIn("buzzwoo-ecom-shopware:shopware-app-system", body)
        # Bare slug must NOT also appear in the list (would be a duplicate).
        before_period = body.split(":", 1)[1] if ":" in body else body
        candidates_section = body.split("candidates:", 1)[-1]
        self.assertEqual(
            candidates_section.count("shopware-app-system"), 1,
            "Plugin skill must appear exactly once in candidates list",
        )


class DeadSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)
        self.skills_root = self.tmp / "fake_home" / ".claude"
        self.skills_root.mkdir(parents=True)

    def _patch_roots(self):
        from token_dashboard import skills as s
        return mock.patch.object(s, "_DEFAULT_ROOTS",
                                 [self.skills_root / "skills"]), s

    def _age_skill(self, path: Path, days: int) -> None:
        """Set mtime to `days` ago — used to bypass the 'recently installed' filter."""
        old = os.path.getmtime(path) - days * 86400
        os.utime(path, (old, old))

    def test_few_dead_skills_no_tip(self):
        # 4 dead skills < threshold of 5
        for i in range(4):
            md = _make_skill(self.skills_root, f"deadlet{i}", "x" * 50)
            self._age_skill(md, days=120)
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = dead_skills_tips(self.db, today_iso="2026-05-19T00:00:00")
        self.assertEqual(tips, [])

    def test_many_dead_skills_emits_tip(self):
        for i in range(6):
            md = _make_skill(self.skills_root, f"deadlet{i}", "x" * 50)
            self._age_skill(md, days=120)
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = dead_skills_tips(self.db, today_iso="2026-05-19T00:00:00")
        self.assertTrue(tips)
        t = tips[0]
        _assert_tip_shape(self, t)
        self.assertEqual(t["category"], "dead-skills")
        self.assertEqual(t["severity"], "info")
        self.assertIn("6 skills", t["title"])

    def test_used_skill_excluded(self):
        for i in range(6):
            md = _make_skill(self.skills_root, f"d{i}", "x" * 50)
            self._age_skill(md, days=120)
        # One of them was invoked within 90d → not dead.
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, timestamp, is_error) VALUES "
                "('m','s','p','Skill','d2','2026-05-15T00:00:00Z',0)"
            )
            c.commit()
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = dead_skills_tips(self.db, today_iso="2026-05-19T00:00:00")
        self.assertTrue(tips)
        body = tips[0]["body"]
        self.assertNotIn("d2,", body)
        self.assertNotIn("d2.", body)
        self.assertIn("5 skills", tips[0]["title"])

    def test_recently_installed_skills_excluded(self):
        # 6 skills, all dead, but only 4 are >=30d old
        for i in range(4):
            md = _make_skill(self.skills_root, f"old{i}", "x" * 50)
            self._age_skill(md, days=120)
        for i in range(2):
            _make_skill(self.skills_root, f"new{i}", "x" * 50)  # fresh mtime
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = dead_skills_tips(self.db, today_iso="2026-05-19T00:00:00")
        # 4 dead skills < min_count → no tip
        self.assertEqual(tips, [])


class SubagentSprawlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _ins(self, *, uuid, session, ts, sidechain, input_t=0, output_t=0,
             agent_id=None):
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "timestamp, model, is_sidechain, agent_id, input_tokens, output_tokens) "
                "VALUES (?, ?, 'p', 'assistant', ?, 'claude-opus-4-7', ?, ?, ?, ?)",
                (uuid, session, ts, sidechain, agent_id, input_t, output_t),
            )
            c.commit()

    def test_sprawl_session_flagged(self):
        # Main chain: 10k tokens; sidechain: 100k → ratio 10× and > 50k → flag.
        self._ins(uuid="m1", session="sprawl", ts="2026-05-15T00:00:00Z",
                  sidechain=0, output_t=10_000)
        self._ins(uuid="s1", session="sprawl", ts="2026-05-15T00:01:00Z",
                  sidechain=1, agent_id="research", output_t=100_000)
        tips = subagent_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        t = tips[0]
        _assert_tip_shape(self, t)
        self.assertEqual(t["category"], "subagent-sprawl")
        self.assertIn("sprawl", t["scope"])
        hrefs = [l["href"] for l in t["links"]]
        self.assertIn("#/sessions/sprawl", hrefs)

    def test_balanced_session_not_flagged(self):
        # Main: 100k, sidechain: 50k → ratio 0.5× → no flag.
        self._ins(uuid="m1", session="balanced", ts="2026-05-15T00:00:00Z",
                  sidechain=0, output_t=100_000)
        self._ins(uuid="s1", session="balanced", ts="2026-05-15T00:01:00Z",
                  sidechain=1, agent_id="research", output_t=50_000)
        tips = subagent_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_small_sidechain_under_threshold_not_flagged(self):
        # Ratio is huge but sidechain absolute < 50k → no flag (noise floor).
        self._ins(uuid="m1", session="tiny", ts="2026-05-15T00:00:00Z",
                  sidechain=0, output_t=100)
        self._ins(uuid="s1", session="tiny", ts="2026-05-15T00:01:00Z",
                  sidechain=1, agent_id="research", output_t=10_000)
        tips = subagent_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_auto_compaction_sidechain_excluded(self):
        # Heavy 'sidechain' but it's auto-compaction (acompact-*) → ignored.
        self._ins(uuid="m1", session="compact", ts="2026-05-15T00:00:00Z",
                  sidechain=0, output_t=10_000)
        self._ins(uuid="s1", session="compact", ts="2026-05-15T00:01:00Z",
                  sidechain=1, agent_id="acompact-abc123", output_t=100_000)
        tips = subagent_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


class BashBloatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _seed_bash_with_result(self, *, cmd, result_tokens, session="s1",
                                tool_use_id="tu1",
                                ts="2026-05-15T00:00:00Z"):
        with connect(self.db) as c:
            # Bash tool_use row
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, timestamp, is_error, tool_use_id) VALUES "
                "(?, ?, 'p', 'Bash', ?, ?, 0, ?)",
                (f"m-{tool_use_id}", session, cmd, ts, tool_use_id),
            )
            # Corresponding _tool_result row
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, result_tokens, timestamp, is_error, tool_use_id) "
                "VALUES (?, ?, 'p', '_tool_result', ?, ?, ?, 0, ?)",
                (f"u-{tool_use_id}", session, tool_use_id, result_tokens, ts,
                 tool_use_id),
            )
            c.commit()

    def test_limiter_regex_recognises_unix_patterns(self):
        self.assertTrue(_has_output_limiter("find / -name foo | head -20"))
        self.assertTrue(_has_output_limiter("grep -r pattern | tail -50"))
        self.assertTrue(_has_output_limiter("ls -R | wc -l"))

    def test_limiter_regex_recognises_powershell_patterns(self):
        self.assertTrue(_has_output_limiter(
            "Get-ChildItem -Recurse | Select-Object -First 20"))
        self.assertTrue(_has_output_limiter("Get-Process -First 5"))
        self.assertTrue(_has_output_limiter(
            "Get-EventLog -LogName System -TotalCount 100"))

    def test_limiter_regex_no_false_positive_on_bare_command(self):
        self.assertFalse(_has_output_limiter("find / -name '*.py'"))
        self.assertFalse(_has_output_limiter("grep -r pattern ."))

    def test_bloated_command_without_limiter_flagged(self):
        # Same command, two invocations, both producing >5k tokens.
        for i in range(2):
            self._seed_bash_with_result(
                cmd="find / -name '*.py'",
                result_tokens=20_000,
                tool_use_id=f"tu{i}",
                ts=f"2026-05-15T00:0{i}:00Z",
            )
        tips = bash_bloat_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        t = tips[0]
        _assert_tip_shape(self, t)
        self.assertEqual(t["category"], "bash-bloat")
        self.assertIn("find /", t["title"])
        hrefs = [l["href"] for l in t["links"]]
        self.assertTrue(any("#/sessions/" in h for h in hrefs))

    def test_command_with_limiter_not_flagged(self):
        # Same big output, but limiter already present → don't nag the user.
        for i in range(2):
            self._seed_bash_with_result(
                cmd="find / -name '*.py' | head -20",
                result_tokens=20_000,
                tool_use_id=f"tu{i}",
                ts=f"2026-05-15T00:0{i}:00Z",
            )
        tips = bash_bloat_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_small_results_not_flagged(self):
        # Two invocations but each result is small → not a bloat case.
        for i in range(2):
            self._seed_bash_with_result(
                cmd="ls",
                result_tokens=200,
                tool_use_id=f"tu{i}",
                ts=f"2026-05-15T00:0{i}:00Z",
            )
        tips = bash_bloat_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_single_occurrence_not_flagged(self):
        # One big invocation alone isn't a pattern worth a tip.
        self._seed_bash_with_result(
            cmd="git log --all -p",
            result_tokens=50_000,
            tool_use_id="tu1",
            ts="2026-05-15T00:00:00Z",
        )
        tips = bash_bloat_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


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
