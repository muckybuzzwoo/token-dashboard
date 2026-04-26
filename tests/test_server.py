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
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

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

    def test_projects_json(self):
        body = json.loads(self._get("/api/projects"))
        self.assertIsInstance(body, list)
        self.assertEqual(body[0]["project_slug"], "p")

    def test_plan_json(self):
        body = json.loads(self._get("/api/plan"))
        self.assertIn("plan", body)
        self.assertIn("pricing", body)

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
