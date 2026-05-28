"""Schema-migration tests for the additive ALTER TABLE / DELETE pattern.

Each migration follows the same recipe — detect the missing column on an old
table, ALTER it in place, and clear messages/tool_calls/files so the next scan
replays JSONLs with the new field populated.
"""
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db, _migrate_add_tool_use_id


class ToolUseIdMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "old.db")

    def _create_pre_migration_db(self):
        """Create a tool_calls table without the tool_use_id column, mimicking an
        older install."""
        with sqlite3.connect(self.db) as c:
            c.execute("""
              CREATE TABLE messages (uuid TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                project_slug TEXT NOT NULL, type TEXT NOT NULL, timestamp TEXT NOT NULL)
            """)
            c.execute("""
              CREATE TABLE tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_uuid TEXT NOT NULL,
                session_id TEXT NOT NULL,
                project_slug TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                target TEXT,
                result_tokens INTEGER,
                is_error INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
              )
            """)
            c.execute("""
              CREATE TABLE files (path TEXT PRIMARY KEY, mtime REAL NOT NULL,
                bytes_read INTEGER NOT NULL, scanned_at REAL NOT NULL)
            """)
            # Seed each table with one row so we can assert clearing.
            c.execute(
                "INSERT INTO messages VALUES ('u1','s1','p','assistant','2026-04-15T00:00:00Z')"
            )
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, "
                "timestamp) VALUES ('u1','s1','p','Bash','2026-04-15T00:00:00Z')"
            )
            c.execute("INSERT INTO files VALUES ('a.jsonl', 1.0, 100, 1.0)")
            c.commit()

    def test_migration_adds_column_and_clears_tables(self):
        self._create_pre_migration_db()
        with sqlite3.connect(self.db) as c:
            cols_before = {row[1] for row in c.execute("PRAGMA table_info(tool_calls)")}
            self.assertNotIn("tool_use_id", cols_before)
            _migrate_add_tool_use_id(c)

            cols_after = {row[1] for row in c.execute("PRAGMA table_info(tool_calls)")}
            self.assertIn("tool_use_id", cols_after)
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0,
                "messages should be cleared so the next scan replays JSONLs",
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 0,
                "tool_calls should be cleared",
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0,
                "files (scan-offset tracker) should be cleared so JSONLs re-read from offset 0",
            )

    def test_migration_is_idempotent_on_new_schema(self):
        """Running the migration twice — or on a fresh init_db DB — is a no-op."""
        init_db(self.db)
        # Seed a row to verify the second call doesn't clear it.
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) "
                "VALUES ('u1','s1','p','assistant','2026-04-15T00:00:00Z')"
            )
            c.commit()

        with sqlite3.connect(self.db) as c:
            _migrate_add_tool_use_id(c)
            count = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.assertEqual(count, 1, "idempotent: row must survive a no-op migration call")

    def test_fresh_init_db_has_tool_use_id_column(self):
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            cols = {row[1] for row in c.execute("PRAGMA table_info(tool_calls)")}
        self.assertIn("tool_use_id", cols)


if __name__ == "__main__":
    unittest.main()
