"""Integration tests for /api/skills budget fields."""
import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from token_dashboard.db import init_db
from token_dashboard.server import build_handler, _cache_clear
from token_dashboard import skills, skill_budgets


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerSkillBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

        # Seed a project-local SKILL.md so cached_catalog (which walks cwds
        # from messages) discovers it. Declared body-text budget = 100.
        self.project = Path(self.tmp) / "myrepo"
        skill_md = self.project / ".claude" / "skills" / "tight-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(
            "---\nname: tight-skill\n---\n\n"
            "## Token Budget\n< 100 output tokens.\n",
            encoding="utf-8",
        )

        with sqlite3.connect(self.db) as c:
            # One user message so cwds lookup picks up the project-local skill root.
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd, output_tokens) "
                "VALUES ('u0', 's1', 'p', 'user', '2026-04-10T00:00:00Z', ?, 0)",
                (str(self.project / "src"),),
            )
            # The assistant message that holds the Skill tool_use block (uuid m1)
            # AND the over-budget output. tool_calls.message_uuid points at it
            # (mirrors how scan_file writes both in lockstep). With the merged
            # skill_breakdown's EXISTS(...m.type='assistant') filter, the tool
            # row must join to a real assistant row to count.
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, output_tokens) "
                "VALUES ('m1', 's1', 'p', 'assistant', '2026-04-10T00:00:02Z', 500)",
            )
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) "
                "VALUES ('m1', 's1', 'p', 'Skill', 'tight-skill', '2026-04-10T00:00:01Z', 0)",
            )
            c.commit()

        # Reset skill caches so the test doesn't inherit neighbour state.
        skills._cache["at"] = 0.0
        skills._cache["data"] = {}
        skills._cache["key"] = None
        skill_budgets._budget_cache.clear()
        _cache_clear()

        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        skills._cache["at"] = 0.0
        skills._cache["data"] = {}
        skills._cache["key"] = None
        skill_budgets._budget_cache.clear()
        _cache_clear()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_skills_endpoint_includes_budget_fields(self):
        rows = json.loads(self._get("/api/skills"))
        self.assertIsInstance(rows, list)
        self.assertTrue(rows, "expected at least one skill row from seeded tool_calls")
        for r in rows:
            self.assertIn("budget_output_tokens", r)
            self.assertIn("p50_output_tokens", r)
            self.assertIn("p95_output_tokens", r)
            self.assertIn("over_budget", r)

    def test_over_budget_flag_on_tight_skill(self):
        rows = json.loads(self._get("/api/skills"))
        by_slug = {r["skill"]: r for r in rows}
        self.assertIn("tight-skill", by_slug)
        r = by_slug["tight-skill"]
        self.assertEqual(r["budget_output_tokens"], 100)
        self.assertEqual(r["p50_output_tokens"], 500)
        self.assertTrue(r["over_budget"])


class ServerSkillSubagentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

        with sqlite3.connect(self.db) as c:
            # One main-chain user message so the project surfaces in catalog walks.
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) "
                "VALUES ('u0', 's1', 'p', 'user', '2026-04-10T00:00:00Z')",
            )
            # Own cost: main-chain assistant emits 100 output tokens on claude-opus-4-5.
            # Inserted before the tool_calls row so the EXISTS join in
            # skill_breakdown's tool_inv CTE can resolve.
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, is_sidechain, timestamp, "
                "model, output_tokens) "
                "VALUES ('m1', 's1', 'p', 'assistant', 0, '2026-04-10T00:00:02Z', "
                "'claude-opus-4-5', 100)",
            )
            # Skill invocation parented to that assistant message (m1).
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) "
                "VALUES ('m1', 's1', 'p', 'Skill', 'orchestrator', '2026-04-10T00:00:01Z', 0)",
            )
            # Sidechain subagent chain: user injection + assistant response.
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, is_sidechain, "
                "timestamp, agent_id) "
                "VALUES ('sc-u', 's1', 'p', 'user', 1, '2026-04-10T00:00:03Z', 'agX')",
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, is_sidechain, "
                "timestamp, model, output_tokens, agent_id) "
                "VALUES ('sc1', 's1', 'p', 'assistant', 1, '2026-04-10T00:00:04Z', "
                "'claude-opus-4-5', 400, 'agX')",
            )
            c.commit()

        skills._cache["at"] = 0.0
        skills._cache["data"] = {}
        skills._cache["key"] = None
        skill_budgets._budget_cache.clear()

        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        skills._cache["at"] = 0.0
        skills._cache["data"] = {}
        skills._cache["key"] = None
        skill_budgets._budget_cache.clear()
        _cache_clear()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_skills_endpoint_exposes_subagent_fields(self):
        rows = json.loads(self._get("/api/skills"))
        by_slug = {r["skill"]: r for r in rows}
        self.assertIn("orchestrator", by_slug)
        r = by_slug["orchestrator"]
        self.assertIn("subagent_cost_usd", r)
        self.assertIn("subagent_output_tokens", r)
        self.assertIn("total_with_subagents_usd", r)
        self.assertEqual(r["subagent_output_tokens"], 400)
        # total_with_subagents_usd must equal own + subagent.
        self.assertAlmostEqual(
            r["total_with_subagents_usd"],
            (r["total_cost_usd"] or 0.0) + (r["subagent_cost_usd"] or 0.0),
            places=6,
        )
        # Subagent cost should be positive (400 output tokens on opus).
        self.assertGreater(r["subagent_cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
