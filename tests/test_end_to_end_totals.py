"""End-to-end accounting invariant: JSONL on disk → overview_totals numbers.

This is the one test the suite was missing — it wires a hand-computed fixture
all the way through the real ``scan_dir`` pipeline and asserts every
token-bucket SUM matches. Previously, a scanner regression that silently
dropped or double-counted tokens could pass CI because no test checked
token totals against known-good values.
"""
import json
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db, overview_totals
from token_dashboard.scanner import scan_dir


def _user(uuid: str, ts: str, text: str) -> dict:
    return {
        "type": "user", "uuid": uuid, "sessionId": "s1",
        "timestamp": ts, "isSidechain": False,
        "message": {"role": "user", "content": text},
    }


def _assistant(uuid: str, parent: str, msg_id: str, ts: str, usage: dict,
               tool_uses=None) -> dict:
    content = [{"type": "text", "text": "..."}]
    if tool_uses:
        content.extend(tool_uses)
    return {
        "type": "assistant", "uuid": uuid, "parentUuid": parent,
        "sessionId": "s1", "timestamp": ts, "isSidechain": False,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "content": content,
            "usage": usage,
        },
    }


def _usage(inp: int, out: int, cr: int, c5: int, c1: int) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation": {
            "ephemeral_5m_input_tokens": c5,
            "ephemeral_1h_input_tokens": c1,
        },
    }


class EndToEndTotalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        self.proj_dir = os.path.join(self.proj_root, "C--work-sample")
        os.makedirs(self.proj_dir)
        init_db(self.db)

    def test_scan_totals_match_hand_computed_sums(self):
        """Fixture covers: dedup of streaming snapshots (msg_A), a tool_use
        record (msg_B), and a plain assistant record (msg_C). Every
        token-bucket SUM in overview_totals must equal the hand-computed
        value after only the final snapshot of msg_A survives dedup."""
        records = [
            # --- Turn 1 ---
            _user("u1", "2026-04-10T00:00:00Z", "prompt 1"),
            # Streaming partial for msg_A — must be evicted by the final.
            _assistant("a1", "u1", "msg_A", "2026-04-10T00:00:01Z",
                       _usage(inp=100, out=10,  cr=500, c5=200, c1=0)),
            # Final snapshot for msg_A — this one is what billing saw.
            _assistant("a2", "u1", "msg_A", "2026-04-10T00:00:02Z",
                       _usage(inp=100, out=200, cr=500, c5=200, c1=50)),

            # --- Turn 2 ---
            _user("u2", "2026-04-10T00:01:00Z", "prompt 2"),
            # Assistant with a tool_use block (contributes 1 row to tool_calls).
            _assistant("a3", "u2", "msg_B", "2026-04-10T00:01:01Z",
                       _usage(inp=50, out=80, cr=300, c5=0, c1=100),
                       tool_uses=[{
                           "type": "tool_use", "id": "tu1", "name": "Read",
                           "input": {"file_path": "foo.py"},
                       }]),
            # Plain assistant reply.
            _assistant("a4", "u2", "msg_C", "2026-04-10T00:01:02Z",
                       _usage(inp=60, out=120, cr=350, c5=30, c1=0)),
        ]

        path = os.path.join(self.proj_dir, "s1.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        scan_dir(self.proj_root, self.db)

        totals = overview_totals(self.db)

        # Hand-computed (a1 evicted; a2 + a3 + a4 survive):
        # input  = 100 + 50 + 60   = 210
        # output = 200 + 80 + 120  = 400
        # cache_read      = 500 + 300 + 350 = 1150
        # cache_create_5m = 200 +   0 +  30 =  230
        # cache_create_1h =  50 + 100 +   0 =  150
        self.assertEqual(totals["sessions"], 1)
        self.assertEqual(totals["turns"], 2, "two user prompts in this fixture")
        self.assertEqual(totals["input_tokens"], 210)
        self.assertEqual(totals["output_tokens"], 400)
        self.assertEqual(totals["cache_read_tokens"], 1150)
        self.assertEqual(totals["cache_create_5m_tokens"], 230)
        self.assertEqual(totals["cache_create_1h_tokens"], 150)

        # Dedup invariant: only 3 assistant rows survive (a1 evicted).
        with sqlite3.connect(self.db) as c:
            assistant_uuids = sorted(
                r[0] for r in c.execute(
                    "SELECT uuid FROM messages WHERE type='assistant'"
                )
            )
            tool_count = c.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE tool_name='Read'"
            ).fetchone()[0]
        self.assertEqual(assistant_uuids, ["a2", "a3", "a4"])
        self.assertEqual(tool_count, 1, "single tool_use row in fixture")


if __name__ == "__main__":
    unittest.main()
