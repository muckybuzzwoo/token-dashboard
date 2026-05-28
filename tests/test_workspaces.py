import os
import tempfile
import unittest

from token_dashboard.db import (
    init_db, connect,
    workspaces_matrix, cross_workspace_leaks,
    subagent_breakdown, top_subagent_sessions, orchestration_breakdown,
    dispatch_tree,
    _workspace_root_path, _build_workspace_index, _classify_path,
)


def _add_msg(conn, uuid, session_id, slug, cwd, ts, model="claude-opus-4-7",
             is_sidechain=0, mtype="assistant", io=(10, 20), cache_read=0):
    conn.execute(
        "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
        "is_sidechain, timestamp, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)",
        (uuid, session_id, slug, cwd, mtype, is_sidechain, ts, model,
         io[0], io[1], cache_read),
    )


def _add_tool(conn, message_uuid, session_id, slug, name, target, ts):
    conn.execute(
        "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
        "tool_name, target, timestamp, is_error) VALUES (?,?,?,?,?,?,0)",
        (message_uuid, session_id, slug, name, target, ts),
    )


class WorkspaceRootTests(unittest.TestCase):
    def test_walks_up_to_slug_match_full_path(self):
        self.assertEqual(
            _workspace_root_path(
                r"C:\Users\a\projects\MyProj\sub",
                "C--Users-a-projects-MyProj",
            ),
            r"C:\Users\a\projects\MyProj",
        )

    def test_returns_none_when_no_ancestor_matches(self):
        self.assertIsNone(_workspace_root_path(r"C:\foo\bar", "totally-different"))

    def test_handles_posix_paths(self):
        self.assertEqual(
            _workspace_root_path("/home/x/proj/sub", "-home-x-proj"),
            "/home/x/proj",
        )


class ClassifyPathTests(unittest.TestCase):
    def setUp(self):
        self.index = [
            (r"c:\users\a\projects\longer-project", "Longer"),
            (r"c:\users\a\projects\proj", "Proj"),
        ]

    def test_matches_longest_prefix_first(self):
        self.assertEqual(
            _classify_path(r"C:\Users\a\projects\longer-project\src\x.py", self.index),
            "Longer",
        )

    def test_exact_root_match(self):
        self.assertEqual(
            _classify_path(r"c:\users\a\projects\proj", self.index),
            "Proj",
        )

    def test_unknown_path_is_external(self):
        self.assertEqual(_classify_path(r"C:\Windows\System32\drivers\etc\hosts", self.index), "external")

    def test_none_path_is_external(self):
        self.assertEqual(_classify_path(None, self.index), "external")


class WorkspacesMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        slug_a = "C--Users-a-projects-ProjA"
        slug_b = "C--Users-a-projects-ProjB"
        cwd_a = r"C:\Users\a\projects\ProjA"
        cwd_b = r"C:\Users\a\projects\ProjB"
        with connect(self.db) as c:
            _add_msg(c, "ma", "s1", slug_a, cwd_a, "2026-05-01T00:00:00Z")
            _add_msg(c, "mb", "s2", slug_b, cwd_b, "2026-05-01T00:00:00Z")
            _add_tool(c, "ma", "s1", slug_a, "Read", cwd_a + r"\src\x.py", "2026-05-01T00:00:01Z")
            _add_tool(c, "ma", "s1", slug_a, "Read", cwd_a + r"\src\x.py", "2026-05-01T00:00:02Z")
            _add_tool(c, "ma", "s1", slug_a, "Edit", cwd_b + r"\notes.md", "2026-05-01T00:00:03Z")
            _add_tool(c, "ma", "s1", slug_a, "Read", r"C:\Windows\hosts", "2026-05-01T00:00:04Z")
            _add_tool(c, "mb", "s2", slug_b, "Write", cwd_b + r"\out.txt", "2026-05-01T00:00:05Z")
            c.commit()

    def test_returns_bipartite_sankey_shape(self):
        m = workspaces_matrix(self.db)
        self.assertIn("nodes", m)
        self.assertIn("links", m)
        self.assertIn("total_calls", m)
        self.assertIn("self_loop_calls", m)
        self.assertIn("cross_workspace_calls", m)
        self.assertGreater(m["total_calls"], 0)
        for l in m["links"]:
            self.assertIn("source", l)
            self.assertIn("target", l)
            self.assertIn("value", l)
        for l in m["links"]:
            self.assertTrue(l["source"].endswith(" (agent)"), l["source"])
            self.assertTrue(l["target"].endswith(" (files)"), l["target"])
        for n in m["nodes"]:
            self.assertIn("name", n)

    def test_classifies_cross_workspace_link(self):
        m = workspaces_matrix(self.db)
        pairs = {(l["source"], l["target"]): l["value"] for l in m["links"]}
        self.assertEqual(pairs.get(("ProjA (agent)", "ProjA (files)")), 2)
        self.assertEqual(pairs.get(("ProjB (agent)", "ProjB (files)")), 1)
        self.assertEqual(pairs.get(("ProjA (agent)", "ProjB (files)")), 1)
        self.assertEqual(pairs.get(("ProjA (agent)", "external (files)")), 1)

    def test_separates_self_loop_and_cross_counters(self):
        m = workspaces_matrix(self.db)
        self.assertEqual(m["self_loop_calls"], 3)
        self.assertEqual(m["cross_workspace_calls"], 2)
        self.assertEqual(m["self_loop_calls"] + m["cross_workspace_calls"], m["total_calls"])

    def test_only_path_tools_counted(self):
        with connect(self.db) as c:
            _add_tool(c, "ma", "s1", "C--Users-a-projects-ProjA", "Bash", "npm test",
                      "2026-05-01T00:00:10Z")
            c.commit()
        m = workspaces_matrix(self.db)
        self.assertEqual(m["total_calls"], 5)


class OrchestrationBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "orch.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, agent_id, entrypoint, timestamp, model, "
                "input_tokens, output_tokens, cache_read_tokens) VALUES "
                "('m1','s1','p','/x','assistant',0,NULL,'cli','2026-05-01T00:00:00Z','claude-opus-4-7',100,200,0)"
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, agent_id, entrypoint, timestamp, model, "
                "input_tokens, output_tokens, cache_read_tokens) VALUES "
                "('m2','s1','p','/x','assistant',1,'acompact-abc','cli','2026-05-01T00:01:00Z','claude-sonnet-4-6',50,100,0)"
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, agent_id, entrypoint, timestamp, model, "
                "input_tokens, output_tokens, cache_read_tokens) VALUES "
                "('m3','s1','p','/x','assistant',1,'a1234','cli','2026-05-01T00:02:00Z','claude-haiku-4-5-20251001',30,40,0)"
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, agent_id, entrypoint, timestamp, model, "
                "input_tokens, output_tokens, cache_read_tokens) VALUES "
                "('m4','s2','p','/x','assistant',0,NULL,'sdk-py','2026-05-01T00:03:00Z','claude-opus-4-7',500,1000,0)"
            )
            c.commit()

    def test_kind_splits_main_compact_subagent(self):
        o = orchestration_breakdown(self.db)
        kinds = {(r["kind"], r["model"]): r for r in o["by_kind"]}
        self.assertIn(("main", "claude-opus-4-7"), kinds)
        self.assertIn(("compact", "claude-sonnet-4-6"), kinds)
        self.assertIn(("subagent", "claude-haiku-4-5-20251001"), kinds)

    def test_entrypoint_breakdown_separates_cli_from_sdk(self):
        o = orchestration_breakdown(self.db)
        eps = {(r["entrypoint"], r["model"]) for r in o["by_entrypoint"]}
        self.assertIn(("cli", "claude-opus-4-7"), eps)
        self.assertIn(("sdk-py", "claude-opus-4-7"), eps)

    def test_sdk_runs_lists_external_orchestration(self):
        o = orchestration_breakdown(self.db)
        self.assertEqual(len(o["sdk_runs"]), 1)
        run = o["sdk_runs"][0]
        self.assertEqual(run["entrypoint"], "sdk-py")
        self.assertEqual(run["sessions"], 1)
        self.assertIn("claude-opus-4-7", run["models"])

    def test_skips_synthetic_in_kind(self):
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "is_sidechain, timestamp, model) VALUES "
                "('mx','s1','p','/x','assistant',0,'2026-05-01T00:10:00Z','<synthetic>')"
            )
            c.commit()
        o = orchestration_breakdown(self.db)
        models = {r["model"] for r in o["by_kind"]}
        self.assertNotIn("<synthetic>", models)


class CrossWorkspaceLeaksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "leaks.db")
        init_db(self.db)
        slug_a = "C--Users-a-projects-ProjA"
        slug_b = "C--Users-a-projects-ProjB"
        cwd_a = r"C:\Users\a\projects\ProjA"
        cwd_b = r"C:\Users\a\projects\ProjB"
        with connect(self.db) as c:
            _add_msg(c, "ma1", "s1", slug_a, cwd_a, "2026-05-01T00:00:00Z")
            _add_msg(c, "ma2", "s2", slug_a, cwd_a, "2026-05-02T00:00:00Z")
            _add_msg(c, "mb1", "s3", slug_b, cwd_b, "2026-05-01T00:00:00Z")
            for i in range(5):
                _add_tool(c, "ma1", "s1", slug_a, "Read", cwd_b + r"\spec.md",
                          f"2026-05-01T00:00:0{i}Z")
            _add_tool(c, "ma2", "s2", slug_a, "Read", cwd_b + r"\spec.md", "2026-05-02T00:00:01Z")
            _add_tool(c, "ma1", "s1", slug_a, "Read", cwd_a + r"\local.py", "2026-05-01T00:00:09Z")
            c.commit()

    def test_excludes_self_loops(self):
        leaks = cross_workspace_leaks(self.db)
        for l in leaks:
            self.assertNotEqual(l["source"], l["target"])

    def test_aggregates_across_sessions(self):
        leaks = cross_workspace_leaks(self.db)
        ab = [l for l in leaks if l["source"] == "ProjA" and l["target"] == "ProjB"]
        self.assertEqual(len(ab), 1)
        self.assertEqual(ab[0]["calls"], 6)
        self.assertEqual(ab[0]["sessions"], 2)

    def test_top_files_ordered(self):
        leaks = cross_workspace_leaks(self.db)
        leak = [l for l in leaks if l["source"] == "ProjA" and l["target"] == "ProjB"][0]
        self.assertGreater(len(leak["top_files"]), 0)
        self.assertEqual(leak["top_files"][0]["n"], 6)


class SubagentBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "sub.db")
        init_db(self.db)
        with connect(self.db) as c:
            _add_msg(c, "m1", "s1", "pA", "/x/a", "2026-05-01T00:00:00Z",
                     model="claude-opus-4-7", is_sidechain=0, io=(100, 200))
            _add_msg(c, "m2", "s1", "pA", "/x/a", "2026-05-01T00:00:10Z",
                     model="claude-sonnet-4-6", is_sidechain=1, io=(50, 100))
            _add_msg(c, "m3", "s2", "pA", "/x/a", "2026-05-01T00:00:20Z",
                     model="claude-sonnet-4-6", is_sidechain=1, io=(30, 40))
            _add_msg(c, "m4", "s2", "pA", "/x/a", "2026-05-01T00:00:30Z",
                     model="claude-haiku-4-5-20251001", is_sidechain=1, io=(10, 20))
            c.commit()

    def test_splits_by_sidechain(self):
        rows = subagent_breakdown(self.db)
        by = {(r["model"], r["is_sidechain"]): r for r in rows}
        self.assertIn(("claude-opus-4-7", 0), by)
        self.assertIn(("claude-sonnet-4-6", 1), by)
        self.assertEqual(by[("claude-sonnet-4-6", 1)]["messages"], 2)
        self.assertEqual(by[("claude-sonnet-4-6", 1)]["sessions"], 2)

    def test_skips_synthetic(self):
        with connect(self.db) as c:
            _add_msg(c, "mx", "s1", "pA", "/x/a", "2026-05-01T00:01:00Z",
                     model="<synthetic>", is_sidechain=0)
            c.commit()
        models = [r["model"] for r in subagent_breakdown(self.db)]
        self.assertNotIn("<synthetic>", models)

    def test_top_subagent_sessions_orders_by_io(self):
        tops = top_subagent_sessions(self.db, limit=5)
        self.assertEqual(tops[0]["session_id"], "s1")
        self.assertEqual(tops[0]["subagent_msgs"], 1)
        self.assertIn("claude-sonnet-4-6", tops[0]["models"])


class DispatchTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "tree.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "is_sidechain, timestamp, model, input_tokens, output_tokens) VALUES "
                "('main1','s1','p','assistant',0,'2026-05-01T00:00:00Z','claude-opus-4-7',100,200)"
            )
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, "
                "tool_name, target, timestamp, is_error) VALUES "
                "('main1','s1','p','Agent','plan-architect','2026-05-01T00:00:00.500Z',0)"
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "is_sidechain, agent_id, timestamp, model, input_tokens, output_tokens) VALUES "
                "('sub1','s1','p','user',1,'a-sonnet-1','2026-05-01T00:00:01Z','claude-sonnet-4-6',0,0)"
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "is_sidechain, agent_id, timestamp, model, input_tokens, output_tokens) VALUES "
                "('sub2','s1','p','assistant',1,'a-sonnet-1','2026-05-01T00:00:02Z','claude-sonnet-4-6',50,100)"
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "is_sidechain, agent_id, timestamp, model, input_tokens, output_tokens) VALUES "
                "('sub3','s1','p','assistant',1,'acompact-zz','2026-05-01T00:00:05Z','claude-sonnet-4-6',10,20)"
            )
            c.commit()

    def test_reconstructs_dispatcher_and_child_link(self):
        rows = dispatch_tree(self.db)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["dispatcher_model"], "claude-opus-4-7")
        self.assertEqual(r["agent_id"], "a-sonnet-1")
        self.assertEqual(r["subagent_type"], "plan-architect")
        self.assertEqual(r["thread_msgs"], 1)
        self.assertIn("claude-sonnet-4-6", r["models"])

    def test_excludes_acompact_threads(self):
        rows = dispatch_tree(self.db)
        for r in rows:
            self.assertFalse(r["agent_id"].startswith("acompact-"))


if __name__ == "__main__":
    unittest.main()
