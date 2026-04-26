"""Unit tests for token_dashboard.skill_budgets.

Parser fixtures use inline strings — never reads ~/.claude/. The actuals
tests seed a tmp SQLite DB and exercise the LEAD window-function boundary.
"""
import os
import tempfile
import unittest

from token_dashboard.db import connect, init_db
from token_dashboard.skill_budgets import (
    parse_budget_from_text,
    skill_actuals,
)


class ParseBudgetTests(unittest.TestCase):
    def test_parse_inline_budget(self):
        body = (
            "---\nname: example-skill\n---\n\n"
            "Execute these steps in order. Complete in <800 output tokens. Conversational.\n"
        )
        self.assertEqual(parse_budget_from_text(body), 800)

    def test_parse_section_budget(self):
        body = (
            "---\nname: skill-foo\n---\n\n"
            "Some body.\n\n## Token Budget\n< 100 output tokens. Fire-and-forget.\n"
        )
        self.assertEqual(parse_budget_from_text(body), 100)

    def test_parse_budget_with_commas(self):
        body = "Complete in <5,500 output tokens. Every claim must trace.\n"
        self.assertEqual(parse_budget_from_text(body), 5500)

    def test_parse_no_budget(self):
        body = (
            "---\nname: skill-bar\ndescription: Generic.\n---\n\n"
            "No declaration in body. Just prose.\n"
        )
        self.assertIsNone(parse_budget_from_text(body))

    def test_parse_inline_wins_over_section(self):
        # Both present — inline (top-of-file, more prescriptive) wins.
        body = (
            "Execute these steps. Complete in <800 output tokens.\n\n"
            "## Token Budget\n< 1,500 output tokens\n"
        )
        self.assertEqual(parse_budget_from_text(body), 800)


def _seed_messages(c, rows):
    """Insert assistant messages. Each row = (uuid, session, ts, output_tokens)."""
    for uuid, session, ts, out in rows:
        c.execute(
            "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, output_tokens) "
            "VALUES (?, ?, 'p', 'assistant', ?, ?)",
            (uuid, session, ts, out),
        )


def _seed_skill_call(c, *, uuid, session, target, ts):
    c.execute(
        "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) "
        "VALUES (?, ?, 'p', 'Skill', ?, ?, 0)",
        (uuid, session, target, ts),
    )


class SkillActualsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "s.db")
        init_db(self.db)

    def test_skill_actuals_basic(self):
        """Single Skill call, 2 subsequent assistant messages → one sample summing both."""
        with connect(self.db) as c:
            _seed_skill_call(c, uuid="a1", session="s1",
                             target="brainstorming", ts="2026-04-10T00:00:00Z")
            _seed_messages(c, [
                ("m1", "s1", "2026-04-10T00:00:05Z", 100),
                ("m2", "s1", "2026-04-10T00:00:10Z", 200),
            ])
            c.commit()

        actuals = skill_actuals(self.db)
        self.assertIn("brainstorming", actuals)
        stat = actuals["brainstorming"]
        self.assertEqual(stat["count"], 1)
        self.assertEqual(stat["p50"], 300)
        self.assertEqual(stat["p95"], 300)

    def test_skill_actuals_next_skill_terminates_window(self):
        """Two Skill calls in one session: first's window ends at second's ts."""
        with connect(self.db) as c:
            _seed_skill_call(c, uuid="a1", session="s1",
                             target="first", ts="2026-04-10T00:00:00Z")
            _seed_messages(c, [
                ("m1", "s1", "2026-04-10T00:00:01Z", 100),
                ("m2", "s1", "2026-04-10T00:00:02Z", 200),
                ("m3", "s1", "2026-04-10T00:00:03Z", 300),
            ])
            _seed_skill_call(c, uuid="a2", session="s1",
                             target="second", ts="2026-04-10T00:00:10Z")
            _seed_messages(c, [
                ("m4", "s1", "2026-04-10T00:00:11Z", 50),
                ("m5", "s1", "2026-04-10T00:00:12Z", 70),
            ])
            c.commit()

        actuals = skill_actuals(self.db)
        # first: messages m1+m2+m3 (before the second call) = 600
        # second: messages m4+m5 (after second call, no next call) = 120
        self.assertEqual(actuals["first"]["p50"], 600)
        self.assertEqual(actuals["second"]["p50"], 120)

    def test_skill_actuals_end_of_session_window(self):
        """Last Skill call in a session with no subsequent call: all remaining output counted."""
        with connect(self.db) as c:
            _seed_skill_call(c, uuid="a1", session="s1",
                             target="tail", ts="2026-04-10T00:00:00Z")
            _seed_messages(c, [
                ("m1", "s1", "2026-04-10T00:00:01Z", 500),
                ("m2", "s1", "2026-04-10T01:00:00Z", 500),
            ])
            c.commit()

        actuals = skill_actuals(self.db)
        self.assertEqual(actuals["tail"]["p50"], 1000)

    def test_skill_actuals_cross_session_does_not_leak(self):
        """A Skill call in session A must not receive output from session B."""
        with connect(self.db) as c:
            _seed_skill_call(c, uuid="a1", session="sA",
                             target="isolated", ts="2026-04-10T00:00:00Z")
            # Same timestamp range, different session — must NOT be counted.
            _seed_messages(c, [
                ("m1", "sB", "2026-04-10T00:00:05Z", 9999),
            ])
            c.commit()

        actuals = skill_actuals(self.db)
        self.assertEqual(actuals["isolated"]["p50"], 0)
        self.assertEqual(actuals["isolated"]["count"], 1)

    def test_skill_actuals_respects_since(self):
        """Skill calls before `since` are filtered out."""
        with connect(self.db) as c:
            _seed_skill_call(c, uuid="a1", session="s1",
                             target="old", ts="2026-04-10T00:00:00Z")
            _seed_skill_call(c, uuid="a2", session="s2",
                             target="new", ts="2026-04-20T00:00:00Z")
            _seed_messages(c, [
                ("m1", "s1", "2026-04-10T00:00:01Z", 111),
                ("m2", "s2", "2026-04-20T00:00:01Z", 222),
            ])
            c.commit()

        actuals = skill_actuals(self.db, since="2026-04-15T00:00:00Z")
        self.assertNotIn("old", actuals)
        self.assertEqual(actuals["new"]["p50"], 222)


if __name__ == "__main__":
    unittest.main()
