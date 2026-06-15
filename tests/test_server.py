import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

import token_dashboard.server as server
from token_dashboard.db import init_db
from token_dashboard.server import build_handler


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, prompt_text, prompt_chars) VALUES ('u',NULL,'s','p','user','2026-04-19T00:00:00Z',NULL,0,0,0,0,0,'hi',2)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('a','u','s','p','assistant','2026-04-19T00:00:01Z','claude-haiku-4-5',1,1,0,0,0)")
            c.commit()
        self.port = _free_port()
        H = build_handler(self.db)
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def _post(self, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req).read()

    def test_index_html(self):
        body = self._get("/")
        self.assertIn(b"Token Dashboard", body)

    def test_overview_json(self):
        body = json.loads(self._get("/api/overview"))
        self.assertIn("sessions", body)
        self.assertEqual(body["sessions"], 1)

    def test_prompts_json(self):
        body = json.loads(self._get("/api/prompts?limit=10"))
        self.assertIsInstance(body, list)

    def test_sessions_json_includes_cost(self):
        body = json.loads(self._get("/api/sessions?limit=10"))
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 1)
        row = body[0]
        self.assertEqual(row["session_id"], "s")
        # The fixture's single assistant turn (haiku, 1 in / 1 out) is priced,
        # so cost is a non-negative number and the estimated flag is a bool.
        self.assertIn("cost_usd", row)
        self.assertIn("cost_estimated", row)
        self.assertIsNotNone(row["cost_usd"])
        self.assertGreaterEqual(row["cost_usd"], 0.0)
        self.assertIsInstance(row["cost_estimated"], bool)

    def test_projects_json(self):
        body = json.loads(self._get("/api/projects"))
        self.assertIsInstance(body, list)
        self.assertEqual(body[0]["project_slug"], "p")

    def test_plan_json(self):
        body = json.loads(self._get("/api/plan"))
        self.assertIn("plan", body)
        self.assertIn("pricing", body)

    def test_settings_json_defaults_to_home_claude(self):
        body = json.loads(self._get("/api/settings"))
        self.assertTrue(body["claude_dir"].endswith(".claude"))
        self.assertTrue(body["projects_dir"].endswith(os.path.join(".claude", "projects")))
        self.assertFalse(body["projects_overridden"])
        self.assertIn(body["claude_dir"], body["claude_dirs"])

    def test_settings_post_valid_claude_dir(self):
        claude_dir = os.path.join(self.tmp, ".claude")
        os.makedirs(os.path.join(claude_dir, "projects"))
        body = json.loads(self._post("/api/settings", {"claude_dir": claude_dir}))
        self.assertEqual(body["claude_dir"], claude_dir)
        self.assertEqual(body["projects_dir"], os.path.join(claude_dir, "projects"))
        self.assertEqual(body["claude_dirs"][0], claude_dir)
        self.assertIn(claude_dir, body["claude_dirs"])

    def test_settings_post_deduplicates_claude_dirs(self):
        claude_dir = os.path.join(self.tmp, ".claude")
        os.makedirs(os.path.join(claude_dir, "projects"))
        self._post("/api/settings", {"claude_dir": claude_dir})
        body = json.loads(self._post("/api/settings", {"claude_dir": claude_dir}))
        self.assertEqual(body["claude_dirs"].count(claude_dir), 1)

    def test_settings_post_can_clear_cached_scan_data(self):
        claude_dir = os.path.join(self.tmp, ".claude")
        os.makedirs(os.path.join(claude_dir, "projects"))
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, timestamp) VALUES ('a','s','p','Read','2026-04-19T00:00:02Z')"
            )
            c.execute("INSERT INTO files (path, mtime, bytes_read, scanned_at) VALUES ('old.jsonl', 1, 10, 1)")
            c.commit()

        body = json.loads(self._post("/api/settings", {"claude_dir": claude_dir, "reset_scan_data": True}))

        self.assertEqual(body["claude_dir"], claude_dir)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT v FROM settings WHERE k='claude_dir'").fetchone()[0], claude_dir)

    def test_settings_post_without_reset_keeps_cached_scan_data(self):
        claude_dir = os.path.join(self.tmp, ".claude")
        os.makedirs(os.path.join(claude_dir, "projects"))

        body = json.loads(self._post("/api/settings", {"claude_dir": claude_dir, "reset_scan_data": False}))

        self.assertEqual(body["claude_dir"], claude_dir)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    def test_scan_uses_saved_claude_dir(self):
        claude_dir = os.path.join(self.tmp, ".claude")
        project = os.path.join(claude_dir, "projects", "demo")
        os.makedirs(project)
        with open(os.path.join(project, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"type":"user","uuid":"su1","sessionId":"ss1","timestamp":"2026-04-20T00:00:00Z","message":{"role":"user","content":"hi"}}\n')
        self._post("/api/settings", {"claude_dir": claude_dir})
        body = json.loads(self._get("/api/scan"))
        self.assertEqual(body["files"], 1)
        self.assertEqual(body["messages"], 1)

    def test_settings_post_invalid_claude_dir(self):
        missing = os.path.join(self.tmp, "missing", ".claude")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/api/settings", {"claude_dir": missing})
        self.assertEqual(cm.exception.code, 400)
        body = json.loads(cm.exception.read())
        self.assertIn("does not exist", body["error"])

    def test_head_returns_200_not_501(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_head_api_endpoint(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_workspaces_json_shape(self):
        body = json.loads(self._get("/api/workspaces"))
        # Empty fixture (no cwd, no tool_calls with file targets) still returns the
        # full bipartite-sankey shape — proves the endpoint wires through cleanly.
        self.assertIn("nodes", body)
        self.assertIn("links", body)
        self.assertIn("total_calls", body)
        self.assertIn("self_loop_calls", body)
        self.assertIn("cross_workspace_calls", body)
        self.assertIn("tools_considered", body)
        self.assertEqual(body["total_calls"], 0)

    def test_cross_workspace_leaks_returns_list(self):
        body = json.loads(self._get("/api/cross-workspace-leaks?limit=5"))
        self.assertIsInstance(body, list)
        # Limit clamping should accept the param without raising.
        self.assertLessEqual(len(body), 5)

    def test_subagents_json_shape(self):
        body = json.loads(self._get("/api/subagents"))
        # The endpoint wires six sub-queries (breakdown, top_sessions, by_kind,
        # by_entrypoint, sdk_runs, dispatch_tree) — make sure none of them blow up
        # on an empty-ish fixture and the response shape is stable.
        for key in ("breakdown", "top_sessions", "by_kind",
                    "by_entrypoint", "sdk_runs", "dispatch_tree"):
            self.assertIn(key, body, f"missing key: {key}")
            self.assertIsInstance(body[key], list)
        # Cost-decoration happened: the haiku assistant row should have cost_usd present
        # (None or a number, but the key must exist).
        if body["breakdown"]:
            self.assertIn("cost_usd", body["breakdown"][0])

    def test_rtk_json_reports_when_cli_is_missing(self):
        body = server._rtk_payload(home=os.path.join(self.tmp, "no-rtk-home"))
        self.assertFalse(body["available"])
        self.assertIn("install_url", body)
        self.assertIsNone(body["summary"])


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "refresh.db")
        init_db(self.db)
        self.orig_scan_dir = server.scan_dir
        self.orig_warm_bundle = server._warm_bundle

    def tearDown(self):
        server.scan_dir = self.orig_scan_dir
        server._warm_bundle = self.orig_warm_bundle

    def test_refresh_does_not_eagerly_warm_overview_bundles(self):
        calls = {"warm": 0}
        server.scan_dir = lambda projects_dir, db_path: {"messages": 0, "tools": 0, "files": 0}
        server._warm_bundle = lambda db_path, pricing: calls.__setitem__("warm", calls["warm"] + 1)

        server._do_refresh(self.db, "/nonexistent", {"models": {}, "plans": {}})

        self.assertEqual(calls["warm"], 0)

    def test_overlapping_refreshes_are_coalesced(self):
        calls = {"scan": 0}

        def slow_scan(projects_dir, db_path):
            calls["scan"] += 1
            time.sleep(0.05)
            return {"messages": 0, "tools": 0, "files": 0}

        server.scan_dir = slow_scan
        server._warm_bundle = lambda db_path, pricing: None

        t1 = threading.Thread(target=server._do_refresh, args=(self.db, "/nonexistent", {"models": {}, "plans": {}}))
        t2 = threading.Thread(target=server._do_refresh, args=(self.db, "/nonexistent", {"models": {}, "plans": {}}))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(calls["scan"], 1)


if __name__ == "__main__":
    unittest.main()
