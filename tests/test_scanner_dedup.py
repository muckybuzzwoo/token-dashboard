"""Scanner must dedupe streaming-snapshot records that share a message.id.

Claude Code writes the same API response 2–3 times while streaming, each as its
own JSONL line with a distinct top-level `uuid` but identical `message.id`.
Only the final snapshot matches the actual billing. Summing all snapshots
over-counts input/output/cache tokens.
"""
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir


def _write_jsonl(path: str, lines):
    with open(path, "w", encoding="utf-8") as f:
        for obj in lines:
            import json as _json
            f.write(_json.dumps(obj) + "\n")


def _streaming_partial(uuid: str, msg_id: str, session: str, ts: str, output_tokens: int):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": "u1",
        "sessionId": session,
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": "streaming..."}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 500,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 200,
                },
            },
        },
    }


class StreamingDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        self.proj_dir = os.path.join(self.proj_root, "C--work-sample")
        os.makedirs(self.proj_dir)
        init_db(self.db)

    def _jsonl_path(self):
        return os.path.join(self.proj_dir, "s1.jsonl")

    def test_within_file_streaming_dupes_collapse_to_final(self):
        user = {
            "type": "user", "uuid": "u1", "sessionId": "s1",
            "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
            "message": {"role": "user", "content": "hi"},
        }
        p1 = _streaming_partial("r1", "msg_X", "s1", "2026-04-10T00:00:01Z", output_tokens=27)
        p2 = _streaming_partial("r2", "msg_X", "s1", "2026-04-10T00:00:02Z", output_tokens=27)
        p3 = _streaming_partial("r3", "msg_X", "s1", "2026-04-10T00:00:03Z", output_tokens=303)
        _write_jsonl(self._jsonl_path(), [user, p1, p2, p3])

        scan_dir(self.proj_root, self.db)

        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT uuid, input_tokens, output_tokens, cache_read_tokens, cache_create_1h_tokens "
                "FROM messages WHERE type='assistant'"
            ).fetchall()

        self.assertEqual(len(rows), 1, "streaming duplicates must collapse to one row")
        row = rows[0]
        # Final snapshot wins — output_tokens = 303, not the sum (357) nor a partial (27).
        self.assertEqual(row["output_tokens"], 303)
        self.assertEqual(row["input_tokens"], 100)
        self.assertEqual(row["cache_read_tokens"], 500)
        self.assertEqual(row["cache_create_1h_tokens"], 200)
        # Winner is the last-seen JSONL row (the final snapshot).
        self.assertEqual(row["uuid"], "r3")

    def test_incremental_scan_final_replaces_partial(self):
        """Partials written first, final appended later — scan twice, final wins."""
        user = {
            "type": "user", "uuid": "u1", "sessionId": "s1",
            "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
            "message": {"role": "user", "content": "hi"},
        }
        p1 = _streaming_partial("r1", "msg_Y", "s1", "2026-04-10T00:00:01Z", output_tokens=27)
        p2 = _streaming_partial("r2", "msg_Y", "s1", "2026-04-10T00:00:02Z", output_tokens=27)
        _write_jsonl(self._jsonl_path(), [user, p1, p2])

        scan_dir(self.proj_root, self.db)

        # Append the final snapshot.
        import json as _json
        with open(self._jsonl_path(), "a", encoding="utf-8") as f:
            final = _streaming_partial("r3", "msg_Y", "s1", "2026-04-10T00:00:03Z", output_tokens=303)
            f.write(_json.dumps(final) + "\n")

        scan_dir(self.proj_root, self.db)

        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT uuid, output_tokens FROM messages WHERE type='assistant'"
            ).fetchall()

        self.assertEqual(len(rows), 1, "final snapshot must replace earlier partial across scans")
        self.assertEqual(rows[0]["output_tokens"], 303)
        self.assertEqual(rows[0]["uuid"], "r3")

    def test_superseded_tool_calls_are_removed(self):
        """When a partial with tool_use is replaced by a final, the partial's
        tool_calls rows must not linger (they'd inflate tool counts)."""
        user = {
            "type": "user", "uuid": "u1", "sessionId": "s1",
            "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
            "message": {"role": "user", "content": "hi"},
        }

        def rec_with_tool(uuid, ts, out):
            return {
                "type": "assistant", "uuid": uuid, "parentUuid": "u1",
                "sessionId": "s1", "timestamp": ts, "isSidechain": False,
                "message": {
                    "id": "msg_Z", "model": "claude-opus-4-7",
                    "content": [
                        {"type": "tool_use", "id": "tu1", "name": "Read",
                         "input": {"file_path": "foo.py"}},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": out},
                },
            }

        _write_jsonl(self._jsonl_path(), [
            user,
            rec_with_tool("r1", "2026-04-10T00:00:01Z", 5),
            rec_with_tool("r2", "2026-04-10T00:00:02Z", 50),
        ])

        scan_dir(self.proj_root, self.db)

        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            tools = c.execute(
                "SELECT message_uuid, tool_name FROM tool_calls WHERE tool_name='Read'"
            ).fetchall()

        self.assertEqual(len(tools), 1, "only the winning record's tool_calls remain")
        self.assertEqual(tools[0]["message_uuid"], "r2")

    def test_assistant_without_message_id_falls_back_to_uuid(self):
        """No message.id → behave as before: each uuid is its own row."""
        recs = [
            {"type": "user", "uuid": "u1", "sessionId": "s1",
             "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "s1",
             "timestamp": "2026-04-10T00:00:01Z", "isSidechain": False,
             "message": {"model": "claude-opus-4-7",
                         "usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"type": "assistant", "uuid": "a2", "parentUuid": "u1", "sessionId": "s1",
             "timestamp": "2026-04-10T00:00:02Z", "isSidechain": False,
             "message": {"model": "claude-opus-4-7",
                         "usage": {"input_tokens": 2, "output_tokens": 2}}},
        ]
        _write_jsonl(self._jsonl_path(), recs)

        scan_dir(self.proj_root, self.db)

        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT uuid FROM messages WHERE type='assistant' ORDER BY uuid"
            ).fetchall()

        self.assertEqual([r["uuid"] for r in rows], ["a1", "a2"])


def _disjoint(uuid, msg_id, session, ts, block, out=300):
    """One assistant record carrying a single content block. Siblings share a
    message.id and all carry the SAME final usage — the current Claude Code
    per-content-block format (not growing snapshots)."""
    return {
        "type": "assistant", "uuid": uuid, "parentUuid": "u1",
        "sessionId": session, "timestamp": ts, "isSidechain": False,
        "message": {
            "id": msg_id, "model": "claude-opus-4-7",
            "content": [block],
            "usage": {
                "input_tokens": 100, "output_tokens": out,
                "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                   "ephemeral_1h_input_tokens": 0},
            },
        },
    }


_THINK = {"type": "thinking", "thinking": "hmm"}
_READ = {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "foo.py"}}
_GREP = {"type": "tool_use", "id": "tu2", "name": "Grep", "input": {"pattern": "bar"}}


class DisjointFormatTests(unittest.TestCase):
    """Current Claude Code splits one response into disjoint per-content-block
    records sharing a message.id. Collapsing to the last record must keep the
    token total right (siblings repeat the final usage) WITHOUT discarding the
    parallel tool_use blocks that live in the non-final siblings (issue #25)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        self.proj_dir = os.path.join(self.proj_root, "C--work-sample")
        os.makedirs(self.proj_dir)
        init_db(self.db)

    def _path(self):
        return os.path.join(self.proj_dir, "s1.jsonl")

    def _assistants_and_tools(self):
        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            asg = c.execute(
                "SELECT uuid, input_tokens, output_tokens FROM messages WHERE type='assistant'"
            ).fetchall()
            tools = c.execute(
                "SELECT message_uuid, tool_name, tool_use_id FROM tool_calls "
                "WHERE tool_name IN ('Read','Grep') ORDER BY tool_name"
            ).fetchall()
        return asg, tools

    def test_disjoint_blocks_keep_all_tool_calls(self):
        user = {"type": "user", "uuid": "u1", "sessionId": "s1",
                "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
                "message": {"role": "user", "content": "do X"}}
        _write_jsonl(self._path(), [
            user,
            _disjoint("a_think", "msg_D", "s1", "2026-04-10T00:00:01Z", _THINK),
            _disjoint("a_read",  "msg_D", "s1", "2026-04-10T00:00:02Z", _READ),
            _disjoint("a_grep",  "msg_D", "s1", "2026-04-10T00:00:03Z", _GREP),  # keeper
        ])
        scan_dir(self.proj_root, self.db)

        asg, tools = self._assistants_and_tools()
        # Tokens: collapse to ONE row carrying the final usage, not the 3x sum.
        self.assertEqual(len(asg), 1)
        self.assertEqual(asg[0]["uuid"], "a_grep")
        self.assertEqual(asg[0]["output_tokens"], 300)
        self.assertEqual(asg[0]["input_tokens"], 100)
        # Bug #1: BOTH parallel tool_use blocks survive, attributed to the keeper.
        self.assertEqual({t["tool_name"] for t in tools}, {"Read", "Grep"})
        self.assertEqual({t["tool_use_id"] for t in tools}, {"tu1", "tu2"})
        self.assertTrue(all(t["message_uuid"] == "a_grep" for t in tools))

    def test_disjoint_blocks_incremental_keep_all_tool_calls(self):
        import json as _json
        user = {"type": "user", "uuid": "u1", "sessionId": "s1",
                "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
                "message": {"role": "user", "content": "do X"}}
        _write_jsonl(self._path(), [
            user,
            _disjoint("a_think", "msg_D", "s1", "2026-04-10T00:00:01Z", _THINK),
            _disjoint("a_read",  "msg_D", "s1", "2026-04-10T00:00:02Z", _READ),
        ])
        scan_dir(self.proj_root, self.db)
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(_json.dumps(
                _disjoint("a_grep", "msg_D", "s1", "2026-04-10T00:00:03Z", _GREP)) + "\n")
        scan_dir(self.proj_root, self.db)

        asg, tools = self._assistants_and_tools()
        self.assertEqual(len(asg), 1)
        self.assertEqual(asg[0]["uuid"], "a_grep")
        self.assertEqual(asg[0]["output_tokens"], 300)
        # The Read tool_use from the earlier scan must NOT be lost when the
        # final sibling lands in a later scan.
        self.assertEqual({t["tool_name"] for t in tools}, {"Read", "Grep"})
        self.assertTrue(all(t["message_uuid"] == "a_grep" for t in tools))

    def test_full_rescan_is_idempotent(self):
        user = {"type": "user", "uuid": "u1", "sessionId": "s1",
                "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
                "message": {"role": "user", "content": "do X"}}
        _write_jsonl(self._path(), [
            user,
            _disjoint("a_think", "msg_D", "s1", "2026-04-10T00:00:01Z", _THINK),
            _disjoint("a_read",  "msg_D", "s1", "2026-04-10T00:00:02Z", _READ),
            _disjoint("a_grep",  "msg_D", "s1", "2026-04-10T00:00:03Z", _GREP),
        ])
        scan_dir(self.proj_root, self.db)
        # Force a full re-read of the same bytes by forgetting the file offset.
        with sqlite3.connect(self.db) as c:
            c.execute("DELETE FROM files")
            c.commit()
        scan_dir(self.proj_root, self.db)

        asg, tools = self._assistants_and_tools()
        self.assertEqual(len(asg), 1, "rescan must not duplicate the collapsed row")
        self.assertEqual(asg[0]["output_tokens"], 300)
        self.assertEqual(len(tools), 2, "rescan must not duplicate tool_calls")
        self.assertEqual({t["tool_use_id"] for t in tools}, {"tu1", "tu2"})


if __name__ == "__main__":
    unittest.main()
