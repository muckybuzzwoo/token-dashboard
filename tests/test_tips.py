import json
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
    context_pressure_tips, repeated_bash_errors_tips,
    web_fetch_volume_tips, opus_only_workspace_tips,
    mcp_sprawl_tips, claude_md_stack_tips,
    long_skill_descriptions_tips, _is_web_fetch_tool,
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


def _fake_default_roots(*specs):
    """Build a `_default_roots`-shaped function returning the given specs.

    Each spec is either a bare ``Path`` (tagged scope='unknown') or a
    ``(root, scope, project_path)`` tuple. Use this in tests to neutralise the
    real `installed_plugins.json` manifest on the developer's machine — the
    catalog leaks otherwise and pollutes assertions.
    """
    out = []
    for s in specs:
        if isinstance(s, tuple):
            root, scope, project_path = s
            out.append({"root": Path(root), "scope": scope,
                        "project_path": project_path})
        else:
            out.append({"root": Path(s), "scope": "unknown", "project_path": None})
    return lambda: out


def _isolate_skill_catalog(fake_roots):
    """Patch both catalog inputs so tests don't leak the dev-machine state.

    Windows tempdirs live under the user's home (``C:\\Users\\<u>\\AppData\\...``)
    which means a naive ancestor-walk for ``.claude/skills/`` will find the
    real one. We patch `_project_skill_roots_from_cwds` to return [] so tests
    see only the explicitly-provided roots.
    """
    from token_dashboard import skills as s
    return [
        mock.patch.object(s, "_default_roots", fake_roots),
        mock.patch.object(s, "_project_skill_roots_from_cwds", lambda cwds: []),
    ]


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

    def _patch_roots(self, extra_roots=()):
        # cached_catalog now reads `_default_roots()`; patch the function so the
        # real installed_plugins.json manifest on the dev machine doesn't leak in.
        from token_dashboard import skills as s
        fake = _fake_default_roots(self.skills_root / "skills", *extra_roots)
        return mock.patch.object(s, "_default_roots", fake), s

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

        # Patch _default_roots to scan both the user-skills and plugins roots.
        from token_dashboard import skills as s
        fake = _fake_default_roots(
            self.skills_root / "skills",
            self.skills_root / "plugins",
        )
        with mock.patch.object(s, "_default_roots", fake):
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

    def test_disable_model_invocation_skill_does_not_count_toward_budget(self):
        long_desc = "x" * 300
        _make_skill(self.skills_root, "visible", long_desc)
        d = self.skills_root / "skills" / "hidden"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: hidden\ndescription: {long_desc}\n"
            "disable-model-invocation: true\n---\n\nBody.\n",
            encoding="utf-8",
        )
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=500,
            )
        # Only `visible` counts (~300 chars) which is under the 500-char budget.
        self.assertEqual(tips, [])

    def test_skill_override_user_invocable_only_does_not_count(self):
        long_desc = "x" * 300
        _make_skill(self.skills_root, "visible", long_desc)
        _make_skill(self.skills_root, "quiet", long_desc)
        settings = self.tmp / "settings.json"
        settings.write_text(
            json.dumps({"skillOverrides": {"quiet": "user-invocable-only"}}),
            encoding="utf-8",
        )
        patch_ctx, s = self._patch_roots()
        from token_dashboard import tips as tips_mod
        with patch_ctx, mock.patch.object(tips_mod, "_USER_SETTINGS_PATH", settings):
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=500,
            )
        # Only `visible` counts — `quiet` is user-invocable-only.
        self.assertEqual(tips, [])

    def test_skill_override_off_does_not_count(self):
        long_desc = "x" * 300
        _make_skill(self.skills_root, "visible", long_desc)
        _make_skill(self.skills_root, "disabled", long_desc)
        settings = self.tmp / "settings.json"
        settings.write_text(
            json.dumps({"skillOverrides": {"disabled": "off"}}),
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


class DeadSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)
        self.skills_root = self.tmp / "fake_home" / ".claude"
        self.skills_root.mkdir(parents=True)

    def _patch_roots(self):
        from token_dashboard import skills as s
        fake = _fake_default_roots(self.skills_root / "skills")
        return mock.patch.object(s, "_default_roots", fake), s

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

    def _dispatch(self, *, session, ts, tool_use_id, return_tokens):
        """Insert an Agent call + matching _tool_result return row."""
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, result_tokens, is_error, timestamp, tool_use_id) "
                "VALUES (?, ?, 'p', 'Agent', 'general-purpose', NULL, 0, ?, ?)",
                (f"m-{tool_use_id}", session, ts, tool_use_id),
            )
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, result_tokens, is_error, timestamp, tool_use_id) "
                "VALUES (?, ?, 'p', '_tool_result', ?, ?, 0, ?, ?)",
                (f"r-{tool_use_id}", session, tool_use_id, return_tokens, ts, tool_use_id),
            )
            c.commit()

    def test_bloated_returns_flagged(self):
        # Two dispatches returning 12k each → total 24k, avg 12k → flag.
        self._dispatch(session="sprawl", ts="2026-05-15T00:00:00Z",
                       tool_use_id="t1", return_tokens=12_000)
        self._dispatch(session="sprawl", ts="2026-05-15T00:01:00Z",
                       tool_use_id="t2", return_tokens=12_000)
        tips = subagent_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        t = tips[0]
        _assert_tip_shape(self, t)
        self.assertEqual(t["category"], "subagent-sprawl")
        self.assertIn("sprawl", t["scope"])
        hrefs = [l["href"] for l in t["links"]]
        self.assertIn("#/sessions/sprawl", hrefs)

    def test_tight_returns_not_flagged(self):
        # Real-world good delegation: subagents returned ~1k each → no flag,
        # regardless of how much they burned internally.
        self._dispatch(session="lean", ts="2026-05-15T00:00:00Z",
                       tool_use_id="t1", return_tokens=600)
        self._dispatch(session="lean", ts="2026-05-15T00:01:00Z",
                       tool_use_id="t2", return_tokens=1_100)
        tips = subagent_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_single_bloated_dispatch_under_total_not_flagged(self):
        # One 15k return: avg passes but total < 20k → no flag (noise floor).
        self._dispatch(session="one", ts="2026-05-15T00:00:00Z",
                       tool_use_id="t1", return_tokens=15_000)
        tips = subagent_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_many_tight_returns_not_flagged(self):
        # 10 dispatches × 3k each = 30k total but avg below 5k → not sprawl,
        # just lots of well-scoped delegation.
        for i in range(10):
            self._dispatch(session="many", ts=f"2026-05-15T00:0{i}:00Z",
                           tool_use_id=f"t{i}", return_tokens=3_000)
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


class ContextPressureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _ins(self, *, uuid, session, input_t=0, cache_read=0, cache_5m=0, cache_1h=0):
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "timestamp, model, input_tokens, cache_read_tokens, "
                "cache_create_5m_tokens, cache_create_1h_tokens) VALUES "
                "(?, ?, 'p', 'assistant', '2026-05-15T00:00:00Z', 'claude-opus-4-7', "
                "?, ?, ?, ?)",
                (uuid, session, input_t, cache_read, cache_5m, cache_1h),
            )
            c.commit()

    def test_heavy_new_content_flagged(self):
        # 50k uncached input + 60k cache_create = 110k net-new (>100k threshold)
        self._ins(uuid="m1", session="big",
                  input_t=50_000, cache_5m=60_000)
        tips = context_pressure_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        _assert_tip_shape(self, tips[0])
        self.assertEqual(tips[0]["category"], "context-pressure")

    def test_cache_read_alone_does_not_trigger(self):
        # 500k cache_read but only 10k net-new content → no flag.
        # Anthropic multi-counts cache reads across breakpoints, so this is the
        # critical regression: we must NOT use cache_read in the formula.
        self._ins(uuid="m1", session="cachy",
                  input_t=5_000, cache_read=500_000, cache_5m=5_000)
        tips = context_pressure_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_low_session_not_flagged(self):
        self._ins(uuid="m1", session="small",
                  input_t=10_000, cache_5m=20_000)
        tips = context_pressure_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


class RepeatedBashErrorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _seed_error(self, *, cmd, tool_use_id, session="s1",
                    ts="2026-05-15T00:00:00Z"):
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, timestamp, is_error, tool_use_id) VALUES "
                "(?, ?, 'p', 'Bash', ?, ?, 0, ?)",
                (f"m-{tool_use_id}", session, cmd, ts, tool_use_id),
            )
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, timestamp, is_error, tool_use_id) VALUES "
                "(?, ?, 'p', '_tool_result', ?, ?, 1, ?)",
                (f"u-{tool_use_id}", session, tool_use_id, ts, tool_use_id),
            )
            c.commit()

    def test_three_identical_errors_flagged(self):
        for i in range(3):
            self._seed_error(
                cmd="docker compose up", tool_use_id=f"tu{i}",
                ts=f"2026-05-15T00:0{i}:00Z",
            )
        tips = repeated_bash_errors_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        _assert_tip_shape(self, tips[0])
        self.assertEqual(tips[0]["category"], "bash-errors")

    def test_two_identical_errors_not_flagged(self):
        for i in range(2):
            self._seed_error(
                cmd="docker compose up", tool_use_id=f"tu{i}",
                ts=f"2026-05-15T00:0{i}:00Z",
            )
        tips = repeated_bash_errors_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


class WebFetchVolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def test_tool_pattern_recogniser(self):
        self.assertTrue(_is_web_fetch_tool("WebFetch"))
        self.assertTrue(_is_web_fetch_tool("mcp__jina__read_url"))
        self.assertTrue(_is_web_fetch_tool("mcp__firecrawl__scrape"))
        self.assertTrue(_is_web_fetch_tool("mcp__playwright__browser_navigate"))
        self.assertFalse(_is_web_fetch_tool("Bash"))
        self.assertFalse(_is_web_fetch_tool("WebSearch"))
        self.assertFalse(_is_web_fetch_tool("mcp__github__get_issue"))

    def test_high_volume_session_flagged(self):
        with connect(self.db) as c:
            for i in range(20):
                c.execute(
                    "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                    "tool_name, target, timestamp, is_error) VALUES "
                    "(?, 'web-heavy', 'p', 'WebFetch', 'https://example.com', "
                    "'2026-05-15T00:00:00Z', 0)",
                    (f"m{i}",),
                )
            c.commit()
        tips = web_fetch_volume_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        _assert_tip_shape(self, tips[0])
        self.assertEqual(tips[0]["category"], "web-fetch-volume")

    def test_low_volume_session_not_flagged(self):
        with connect(self.db) as c:
            for i in range(5):
                c.execute(
                    "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                    "tool_name, target, timestamp, is_error) VALUES "
                    "(?, 's', 'p', 'WebFetch', 'https://example.com', "
                    "'2026-05-15T00:00:00Z', 0)",
                    (f"m{i}",),
                )
            c.commit()
        tips = web_fetch_volume_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_out_of_window_webfetch_rows_excluded(self):
        """Regression for SQL operator-precedence bug — without parentheses
        around the OR, the 7-day timestamp filter was only applied to the
        mcp__% branch, and historical WebFetch rows leaked through forever."""
        with connect(self.db) as c:
            # 14 in-window WebFetch rows (below the 15 threshold on their own)
            for i in range(14):
                c.execute(
                    "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                    "tool_name, target, timestamp, is_error) VALUES "
                    "(?, 'sess', 'p', 'WebFetch', 'https://example.com', "
                    "'2026-05-15T00:00:00Z', 0)",
                    (f"in{i}",),
                )
            # 10 OLD WebFetch rows from 6 months ago — must NOT be counted
            for i in range(10):
                c.execute(
                    "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                    "tool_name, target, timestamp, is_error) VALUES "
                    "(?, 'sess', 'p', 'WebFetch', 'https://example.com', "
                    "'2025-11-01T00:00:00Z', 0)",
                    (f"old{i}",),
                )
            c.commit()
        # With the bug, the session would count 24 fetches and trigger the tip.
        # Fixed: only 14 in-window counts → below 15 threshold → no tip.
        tips = web_fetch_volume_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


class OpusOnlyWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _seed_assistant(self, *, uuid, project, model, ts="2026-05-15T00:00:00Z"):
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "timestamp, model) VALUES (?, 's', ?, 'assistant', ?, ?)",
                (uuid, project, ts, model),
            )
            c.commit()

    def test_opus_heavy_project_flagged(self):
        for i in range(55):
            self._seed_assistant(uuid=f"o{i}", project="heavy-proj",
                                 model="claude-opus-4-7")
        tips = opus_only_workspace_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        _assert_tip_shape(self, tips[0])
        self.assertEqual(tips[0]["severity"], "cost")
        self.assertIn("heavy-proj", tips[0]["title"])

    def test_mixed_project_not_flagged(self):
        for i in range(30):
            self._seed_assistant(uuid=f"o{i}", project="mixed-proj",
                                 model="claude-opus-4-7")
        for i in range(30):
            self._seed_assistant(uuid=f"s{i}", project="mixed-proj",
                                 model="claude-sonnet-4-6")
        tips = opus_only_workspace_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)

    def test_small_project_not_flagged(self):
        # All-Opus but <50 turns — below threshold for actionable signal.
        for i in range(20):
            self._seed_assistant(uuid=f"o{i}", project="tiny-proj",
                                 model="claude-opus-4-7")
        tips = opus_only_workspace_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


class McpSprawlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _seed_mcp(self, server: str, tool: str = "tool"):
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, timestamp, is_error) VALUES "
                "(?, 's', 'p', ?, 'target', '2026-05-15T00:00:00Z', 0)",
                (f"m-{server}-{tool}", f"mcp__{server}__{tool}"),
            )
            c.commit()

    def test_many_servers_flagged(self):
        for i in range(15):
            self._seed_mcp(f"server{i}")
        tips = mcp_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        _assert_tip_shape(self, tips[0])
        self.assertIn("15 MCP servers", tips[0]["title"])

    def test_few_servers_not_flagged(self):
        for i in range(5):
            self._seed_mcp(f"server{i}")
        tips = mcp_sprawl_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


class ClaudeMdStackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)

    def _seed_cwd(self, cwd: str):
        with connect(self.db) as c:
            for i in range(10):
                c.execute(
                    "INSERT INTO messages (uuid, session_id, project_slug, type, "
                    "timestamp, cwd) VALUES (?, 's', 'p', 'user', "
                    "'2026-05-15T00:00:00Z', ?)",
                    (f"u{i}", cwd),
                )
            c.commit()

    def test_three_stacked_claude_mds_flagged(self):
        root = self.tmp / "root"
        proj = root / "proj"
        nested = proj / "sub"
        nested.mkdir(parents=True)
        (root / "CLAUDE.md").write_text("# global\n" + ("line\n" * 150), encoding="utf-8")
        (proj / "CLAUDE.md").write_text("# project\n" + ("line\n" * 150), encoding="utf-8")
        (nested / "CLAUDE.md").write_text("# nested\n" + ("line\n" * 150), encoding="utf-8")
        self._seed_cwd(str(nested))
        tips = claude_md_stack_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertTrue(tips)
        _assert_tip_shape(self, tips[0])
        self.assertEqual(tips[0]["category"], "claude-md-stack")

    def test_single_claude_md_not_flagged(self):
        proj = self.tmp / "solo"
        proj.mkdir()
        (proj / "CLAUDE.md").write_text("# solo\n" + ("line\n" * 200), encoding="utf-8")
        self._seed_cwd(str(proj))
        tips = claude_md_stack_tips(self.db, today_iso="2026-05-16T00:00:00")
        self.assertFalse(tips)


class SkillBudgetScopeAwarenessTests(unittest.TestCase):
    """Phase 2 verification: budget tip reflects per-context effective footprint."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)
        self.home = self.tmp / "home"
        self.home.mkdir()

    def _make_skill_at(self, root: Path, name: str, desc: str) -> Path:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        md = d / "SKILL.md"
        md.write_text(f"---\nname: {name}\ndescription: {desc}\n---\nx\n",
                      encoding="utf-8")
        return md

    def test_project_scoped_skills_excluded_when_user_in_other_project(self):
        """Project-scoped skills loaded for repo-A must NOT count toward the
        budget when the user's most-active cwd is repo-B."""
        repo_a = self.tmp / "repos" / "a"
        repo_b = self.tmp / "repos" / "b"
        repo_a.mkdir(parents=True)
        repo_b.mkdir(parents=True)
        skills_a = self.tmp / "skills-a"
        skills_a.mkdir()
        # 5 skills × 400-char descriptions = 2000 chars — would exceed budget=800
        for i in range(5):
            self._make_skill_at(skills_a, f"a{i}", "x" * 400)

        # Seed messages.cwd pointing to repo_b, not repo_a.
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("u1", "s1", "b", "user", "2026-04-15T00:00:00Z", str(repo_b)),
            )
            c.commit()

        from token_dashboard import skills as s
        fake = _fake_default_roots((skills_a, "project-global", str(repo_a)))
        patches = _isolate_skill_catalog(fake)
        for p in patches:
            p.start()
        try:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=800,
            )
        finally:
            for p in reversed(patches):
                p.stop()
        # The 5 skills are only loaded in repo_a; user works in repo_b → tip
        # must not fire.
        self.assertEqual(tips, [])

    def test_user_global_skills_drive_budget_in_any_cwd(self):
        skills = self.tmp / "user-skills"
        skills.mkdir()
        for i in range(5):
            self._make_skill_at(skills, f"u{i}", "x" * 400)

        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("u1", "s1", "anywhere", "user", "2026-04-15T00:00:00Z",
                 str(self.tmp / "anywhere")),
            )
            c.commit()

        from token_dashboard import skills as s
        fake = _fake_default_roots((skills, "user-global", None))
        patches = _isolate_skill_catalog(fake)
        for p in patches:
            p.start()
        try:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=800,
            )
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertTrue(tips)
        # When all active skills are user-global, the body must say so —
        # the user can't escape the cost by switching projects.
        self.assertIn("user-global", tips[0]["body"])

    def test_body_names_top_project_when_mixing_scopes(self):
        repo = self.tmp / "repos" / "main-project"
        repo.mkdir(parents=True)
        proj_skills = self.tmp / "proj-skills"
        proj_skills.mkdir()
        user_skills = self.tmp / "user-skills"
        user_skills.mkdir()
        # 3 user-global + 3 project-global, each 400 chars.
        for i in range(3):
            self._make_skill_at(user_skills, f"u{i}", "x" * 400)
            self._make_skill_at(proj_skills, f"p{i}", "y" * 400)

        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("u1", "s1", "main", "user", "2026-04-15T00:00:00Z", str(repo)),
            )
            c.commit()

        from token_dashboard import skills as s
        fake = _fake_default_roots(
            (user_skills, "user-global", None),
            (proj_skills, "project-global", str(repo)),
        )
        patches = _isolate_skill_catalog(fake)
        for p in patches:
            p.start()
        try:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = skill_listing_budget_tips(
                self.db, today_iso="2026-04-19T00:00:00", budget_chars=800,
            )
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertTrue(tips)
        body = tips[0]["body"]
        # Body should name the project the user is most active in.
        self.assertIn("main-project", body)
        # And split the count between global vs project-scoped.
        self.assertIn("3 global skill", body)
        self.assertIn("3 project-scoped skill", body)


class DeadSkillsScopeAwarenessTests(unittest.TestCase):
    """Phase 2: project-scoped skills in unvisited projects aren't 'dead'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)

    def _make(self, root: Path, name: str, desc: str = "x") -> Path:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        md = d / "SKILL.md"
        md.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n", encoding="utf-8")
        old = os.path.getmtime(md) - 120 * 86400  # bypass new-install grace period
        os.utime(md, (old, old))
        return md

    def test_unvisited_project_skills_not_counted_as_dead(self):
        # 6 skills under repo_unvisited (project-scoped), zero invocations.
        # User has only worked in repo_visited recently.
        repo_unvisited = self.tmp / "unvisited"
        repo_unvisited.mkdir()
        repo_visited = self.tmp / "visited"
        repo_visited.mkdir()
        skills_root = self.tmp / "skills-unvisited"
        skills_root.mkdir()
        for i in range(6):
            self._make(skills_root, f"dead{i}")

        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("u1", "s1", "v", "user", "2026-05-15T00:00:00Z", str(repo_visited)),
            )
            c.commit()

        from token_dashboard import skills as s
        fake = _fake_default_roots(
            (skills_root, "project-global", str(repo_unvisited))
        )
        patches = _isolate_skill_catalog(fake)
        for p in patches:
            p.start()
        try:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = dead_skills_tips(self.db, today_iso="2026-05-19T00:00:00")
        finally:
            for p in reversed(patches):
                p.stop()
        # Project-scoped to a repo the user hasn't visited in 90d → those
        # zero-invocation counts are meaningless; tip must NOT flag them as dead.
        self.assertEqual(tips, [])


class LongSkillDescriptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)
        self.skills_root = self.tmp / "fake_home" / ".claude"
        self.skills_root.mkdir(parents=True)

    def _patch_roots(self):
        from token_dashboard import skills as s
        fake = _fake_default_roots(self.skills_root / "skills")
        return mock.patch.object(s, "_default_roots", fake), s

    def test_three_long_descriptions_flagged(self):
        for i in range(3):
            _make_skill(self.skills_root, f"verbose{i}", "x" * 500)
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = long_skill_descriptions_tips(
                self.db, today_iso="2026-05-16T00:00:00"
            )
        self.assertTrue(tips)
        _assert_tip_shape(self, tips[0])
        self.assertEqual(tips[0]["category"], "long-skill-descriptions")
        self.assertIn("verbose0", tips[0]["body"])

    def test_short_descriptions_not_flagged(self):
        for i in range(5):
            _make_skill(self.skills_root, f"terse{i}", "short")
        patch_ctx, s = self._patch_roots()
        with patch_ctx:
            s._cache = {"at": 0.0, "data": {}, "key": None}
            tips = long_skill_descriptions_tips(
                self.db, today_iso="2026-05-16T00:00:00"
            )
        self.assertEqual(tips, [])


class TipSchemaHelperTests(unittest.TestCase):
    def test_instance_builder_shape(self):
        from token_dashboard import tips as tipmod
        inst = tipmod._instance(title="foo.py — 3×", key="repeat-file:foo.py",
                                links=[{"label": "s", "href": "#/sessions/x"}, None])
        self.assertEqual(inst, {"title": "foo.py — 3×", "key": "repeat-file:foo.py",
                        "links": [{"label": "s", "href": "#/sessions/x"}]})

    def test_make_tip_without_instances_has_no_instances_key(self):
        from token_dashboard import tips as tipmod
        t = tipmod._make_tip(key="k", category="c", severity="info",
                             title="t", body="b", scope="s")
        self.assertNotIn("instances", t)

    def test_make_tip_with_instances_includes_them(self):
        from token_dashboard import tips as tipmod
        inst = tipmod._instance(title="i", key="c:1", links=[])
        t = tipmod._make_tip(key="c:overall", category="c", severity="info",
                             title="Heading", body="shared", scope="overall",
                             instances=[inst])
        self.assertEqual(t["instances"], [inst])
        self.assertEqual(t["title"], "Heading")


if __name__ == "__main__":
    unittest.main()
