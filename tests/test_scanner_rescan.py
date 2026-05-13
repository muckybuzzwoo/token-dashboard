"""Re-scanning a JSONL file must not duplicate or lose rows.

When Claude Code writes the same `.jsonl` in place (log compaction, crash
recovery, or simply a touched mtime with the same content), the scanner
falls through to a full rescan from offset 0. Messages use INSERT OR REPLACE
on uuid so those rows stay correct, but tool_calls has no unique key and
INSERTs blindly — so every rescan inflates tool counts.
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest

from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir


def _assistant_with_tool_use(uuid: str, msg_id: str, ts: str, output_tokens: int) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": "u1",
        "sessionId": "s1",
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "content": [
                {"type": "tool_use", "id": "tu1", "name": "Read",
                 "input": {"file_path": "foo.py"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": output_tokens},
        },
    }


def _user_record() -> dict:
    return {
        "type": "user", "uuid": "u1", "sessionId": "s1",
        "timestamp": "2026-04-10T00:00:00Z", "isSidechain": False,
        "message": {"role": "user", "content": "hi"},
    }


def _write_jsonl(path: str, records) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class RescanIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        self.proj_dir = os.path.join(self.proj_root, "C--work-sample")
        os.makedirs(self.proj_dir)
        init_db(self.db)

    def _jsonl_path(self) -> str:
        return os.path.join(self.proj_dir, "s1.jsonl")

    def _count_tools(self) -> int:
        with sqlite3.connect(self.db) as c:
            return c.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE tool_name='Read'"
            ).fetchone()[0]

    def test_partial_line_at_eof_is_not_skipped_on_next_scan(self):
        """A record mid-flush (no trailing newline yet) must be picked up
        on the next scan once the line completes — we cannot advance the
        byte offset past a line we haven't successfully parsed."""
        path = self._jsonl_path()

        # Scan 1: complete record A, then a partial line for record B with no
        # trailing newline (simulating Claude Code mid-flush).
        rec_a = _assistant_with_tool_use("a1", "msg_A", "2026-04-10T00:00:01Z", 10)
        partial_b = json.dumps(
            _assistant_with_tool_use("a2", "msg_B", "2026-04-10T00:00:02Z", 20)
        )
        half = len(partial_b) // 2
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_user_record()) + "\n")
            f.write(json.dumps(rec_a) + "\n")
            f.write(partial_b[:half])  # truncated, no "\n"

        scan_dir(self.proj_root, self.db)

        with sqlite3.connect(self.db) as c:
            uuids_after_scan1 = sorted(
                r[0] for r in c.execute("SELECT uuid FROM messages")
            )
        self.assertEqual(
            uuids_after_scan1, ["a1", "u1"],
            "partial line must be skipped on first scan (JSON decode fails)",
        )

        # Scan 2: complete record B's line + append a brand-new record C.
        rec_c = _assistant_with_tool_use("a3", "msg_C", "2026-04-10T00:00:03Z", 30)
        with open(path, "a", encoding="utf-8") as f:
            f.write(partial_b[half:] + "\n")
            f.write(json.dumps(rec_c) + "\n")
        future = time.time() + 10
        os.utime(path, (future, future))

        scan_dir(self.proj_root, self.db)

        with sqlite3.connect(self.db) as c:
            uuids_after_scan2 = sorted(
                r[0] for r in c.execute("SELECT uuid FROM messages")
            )
        self.assertEqual(
            uuids_after_scan2, ["a1", "a2", "a3", "u1"],
            "record whose line was partial on scan 1 must be loaded on scan 2",
        )

    def test_rescan_with_same_content_does_not_duplicate_tool_calls(self):
        """Full rescan (mtime changed, content unchanged) must not grow tool_calls."""
        _write_jsonl(self._jsonl_path(), [
            _user_record(),
            _assistant_with_tool_use("a1", "msg_X", "2026-04-10T00:00:01Z", 42),
        ])

        scan_dir(self.proj_root, self.db)
        self.assertEqual(self._count_tools(), 1, "first scan inserts one tool_call")

        # Force mtime to move forward without changing content — triggers a
        # full rescan from offset 0 (the scan_dir "else" branch).
        future = time.time() + 10
        os.utime(self._jsonl_path(), (future, future))

        n2 = scan_dir(self.proj_root, self.db)
        self.assertEqual(
            n2["messages"], 0,
            "mtime-only changes with the same byte size should not reparse the file",
        )
        self.assertEqual(
            self._count_tools(), 1,
            "rescan must not duplicate tool_calls — INSERT needs to clear per-message first",
        )


if __name__ == "__main__":
    unittest.main()
