"""SQLite schema, connection, and shared query helpers."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  mtime       REAL    NOT NULL,
  bytes_read  INTEGER NOT NULL,
  scanned_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  uuid                    TEXT PRIMARY KEY,
  parent_uuid             TEXT,
  session_id              TEXT NOT NULL,
  project_slug            TEXT NOT NULL,
  cwd                     TEXT,
  git_branch              TEXT,
  cc_version              TEXT,
  entrypoint              TEXT,
  type                    TEXT NOT NULL,
  is_sidechain            INTEGER NOT NULL DEFAULT 0,
  agent_id                TEXT,
  timestamp               TEXT NOT NULL,
  model                   TEXT,
  stop_reason             TEXT,
  prompt_id               TEXT,
  message_id              TEXT,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  prompt_text             TEXT,
  prompt_chars            INTEGER,
  tool_calls_json         TEXT,
  attribution_skill       TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project_slug);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_msgid     ON messages(session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent    ON messages(parent_uuid);
CREATE INDEX IF NOT EXISTS idx_messages_agent     ON messages(agent_id);
CREATE INDEX IF NOT EXISTS idx_messages_date      ON messages(substr(timestamp,1,10));
CREATE INDEX IF NOT EXISTS idx_messages_type_model ON messages(type, model);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp_session ON messages(timestamp, session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp_project ON messages(timestamp, project_slug);
CREATE INDEX IF NOT EXISTS idx_messages_type_timestamp_model ON messages(type, timestamp, model);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_uuid  TEXT    NOT NULL,
  session_id    TEXT    NOT NULL,
  project_slug  TEXT    NOT NULL,
  tool_name     TEXT    NOT NULL,
  target        TEXT,
  result_tokens INTEGER,
  is_error      INTEGER NOT NULL DEFAULT 0,
  timestamp     TEXT    NOT NULL,
  tool_use_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_target  ON tool_calls(target);
CREATE INDEX IF NOT EXISTS idx_tools_timestamp_name ON tool_calls(timestamp, tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_use_id  ON tool_calls(tool_use_id);

CREATE TABLE IF NOT EXISTS plan (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS dismissed_tips (
  tip_key       TEXT PRIMARY KEY,
  dismissed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_daily (
  day                    TEXT PRIMARY KEY,
  turns                  INTEGER NOT NULL DEFAULT 0,
  input_tokens           INTEGER NOT NULL DEFAULT 0,
  output_tokens          INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS summary_projects (
  day                    TEXT NOT NULL,
  project_slug           TEXT NOT NULL,
  sample_cwd             TEXT,
  turns                  INTEGER NOT NULL DEFAULT 0,
  input_tokens           INTEGER NOT NULL DEFAULT 0,
  output_tokens          INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, project_slug)
);

CREATE TABLE IF NOT EXISTS summary_models (
  day                    TEXT NOT NULL,
  model                  TEXT NOT NULL,
  turns                  INTEGER NOT NULL DEFAULT 0,
  input_tokens           INTEGER NOT NULL DEFAULT 0,
  output_tokens          INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, model)
);

CREATE TABLE IF NOT EXISTS summary_tools (
  day           TEXT NOT NULL,
  tool_name     TEXT NOT NULL,
  calls         INTEGER NOT NULL DEFAULT 0,
  result_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, tool_name)
);

CREATE TABLE IF NOT EXISTS summary_sessions (
  session_id              TEXT PRIMARY KEY,
  project_slug            TEXT NOT NULL,
  sample_cwd              TEXT,
  started                 TEXT NOT NULL,
  ended                   TEXT NOT NULL,
  turns                   INTEGER NOT NULL DEFAULT 0,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_summary_sessions_ended ON summary_sessions(ended);
CREATE INDEX IF NOT EXISTS idx_summary_sessions_project ON summary_sessions(project_slug);
"""


def default_db_path() -> Path:
    return Path.home() / ".claude" / "token-dashboard.db"


def default_claude_dir() -> Path:
    return Path.home() / ".claude"


def init_db(path: Union[str, Path]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA journal_mode=WAL")
        _migrate_add_message_id(c)
        _migrate_add_attribution_skill(c)
        _migrate_add_tool_use_id(c)
        c.executescript(SCHEMA)


def _migrate_add_message_id(conn) -> None:
    """Add messages.message_id for streaming-snapshot dedup.

    Why: pre-migration rows were summed from all streaming snapshots (over-count).
    How to apply: if the old table exists without the column, add it and clear
    messages/tool_calls/files so the next scan replays JSONLs cleanly. Source
    of truth is on disk; rescanning is cheap.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "message_id" in cols:
        return
    conn.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tool_calls")
    conn.execute("DELETE FROM files")
    conn.commit()


def _migrate_add_attribution_skill(conn) -> None:
    """Add messages.attribution_skill to track slash-command activity.

    Claude Code tags every assistant message produced inside an active
    slash-command session with a top-level ``attributionSkill`` field
    (e.g. ``"claude-md-management:claude-md-improver"``). Without this
    column the skills view only sees explicit ``Skill`` tool calls and
    misses all the assistant turns that ran under a slash command.

    Migration strategy: clear messages/tool_calls/files so the next scan
    replays every JSONL and populates the new column. Also clear
    summary_meta so the materialised summary tables get rebuilt from
    scratch — otherwise the dashboard would serve stale daily/per-project
    totals between the migration and the next ``scan_dir`` call.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "attribution_skill" in cols:
        return
    conn.execute("ALTER TABLE messages ADD COLUMN attribution_skill TEXT")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tool_calls")
    conn.execute("DELETE FROM files")
    # Force a full summary rebuild on the next scan: clearing summary_meta
    # makes summaries_ready() return False, which triggers rebuild_summaries()
    # without arguments (full pass) instead of an incremental delta.
    has_summary_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='summary_meta'"
    ).fetchone()
    if has_summary_meta:
        conn.execute("DELETE FROM summary_meta")
    conn.commit()


def _migrate_add_tool_use_id(conn) -> None:
    """Add tool_calls.tool_use_id linking tool_use blocks to their _tool_result rows.

    Why: tool_calls stores the original tool's argument in `target` (e.g. the
    Bash command), while the corresponding _tool_result row stores the
    `tool_use_id` in `target`. There was no direct join between the two, so
    `result_tokens` could not be attributed back to the command that produced
    it. This blocks Bash-bloat-by-command detection.

    How to apply: if the old table exists without the column, add it and clear
    messages/tool_calls/files so the next scan replays JSONLs with the new
    field populated. Source of truth is on disk; rescanning is cheap.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
    if "tool_use_id" in cols:
        return
    conn.execute("ALTER TABLE tool_calls ADD COLUMN tool_use_id TEXT")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tool_calls")
    conn.execute("DELETE FROM files")
    has_summary_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='summary_meta'"
    ).fetchone()
    if has_summary_meta:
        conn.execute("DELETE FROM summary_meta")
    conn.commit()


@contextmanager
def connect(path: Union[str, Path]):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size=-65536")  # 64 MB page cache ceiling
    try:
        yield conn
    finally:
        conn.close()


def get_setting(db_path: Union[str, Path], key: str, default: Optional[str] = None) -> Optional[str]:
    with connect(db_path) as c:
        row = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_setting(db_path: Union[str, Path], key: str, value: str) -> None:
    with connect(db_path) as c:
        c.execute("INSERT OR REPLACE INTO settings (k, v) VALUES (?, ?)", (key, value))
        c.commit()


def clear_scan_data(db_path: Union[str, Path]) -> None:
    """Clear cached transcript-derived rows without deleting user settings."""
    with connect(db_path) as c:
        c.execute("DELETE FROM tool_calls")
        c.execute("DELETE FROM messages")
        c.execute("DELETE FROM files")
        c.commit()


def _range_clause(since, until, col: str = "timestamp"):
    where, args = [], []
    if since:
        where.append(f"{col} >= ?"); args.append(since)
    if until:
        where.append(f"{col} < ?"); args.append(until)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _date_range_clause(since, until, col: str = "substr(timestamp, 1, 10)"):
    where, args = [], []
    if since:
        where.append(f"{col} >= ?"); args.append(since[:10])
    if until:
        where.append(f"{col} < ?"); args.append(until[:10])
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _day_aligned(ts) -> bool:
    """True when a range boundary sits exactly on a UTC calendar-day edge.

    The summary_daily / summary_projects / summary_models / summary_tools fast
    paths bucket by whole UTC days, so their totals are only correct when
    since/until land on a day boundary: None, a date-only string, or a midnight
    timestamp. Rolling ranges such as the Overview 1d/2d/3d filters send a
    full-precision ``toISOString()`` with a sub-day time component; those must
    bypass the day-bucketed fast path (its ``since[:10]`` truncation would fold
    in the whole partial calendar day — up to ~100% overcount on a 1-day range).
    """
    if not ts or len(ts) <= 10:
        return True
    return ts[11:19] == "00:00:00"


def _summary_ready(conn) -> bool:
    row = conn.execute("SELECT v FROM summary_meta WHERE k='last_rebuild'").fetchone()
    return row is not None


def summaries_ready(db_path) -> bool:
    with connect(db_path) as c:
        return _summary_ready(c)


def rebuild_summaries(db_path, days=None, sessions=None) -> None:
    """Rebuild aggregate tables used by overview endpoints.

    These summaries keep refresh-time overview queries bounded by days,
    projects, tools, models, and sessions instead of raw message volume.
    """
    days = {d for d in (days or set()) if d}
    sessions = {s for s in (sessions or set()) if s}
    full = not days and not sessions
    with connect(db_path) as c:
        if full:
            c.execute("DELETE FROM summary_meta")
            c.execute("DELETE FROM summary_daily")
            c.execute("DELETE FROM summary_projects")
            c.execute("DELETE FROM summary_models")
            c.execute("DELETE FROM summary_tools")
            c.execute("DELETE FROM summary_sessions")
            day_filter = ""
            day_args = ()
            session_filter = ""
            session_args = ()
        else:
            day_args = tuple(sorted(days))
            session_args = tuple(sorted(sessions))
            day_ph = ",".join("?" * len(day_args))
            session_ph = ",".join("?" * len(session_args))
            day_filter = f" AND substr(timestamp, 1, 10) IN ({day_ph})" if day_args else " AND 0"
            session_filter = f" AND session_id IN ({session_ph})" if session_args else " AND 0"
            if day_args:
                c.execute(f"DELETE FROM summary_daily WHERE day IN ({day_ph})", day_args)
                c.execute(f"DELETE FROM summary_projects WHERE day IN ({day_ph})", day_args)
                c.execute(f"DELETE FROM summary_models WHERE day IN ({day_ph})", day_args)
                c.execute(f"DELETE FROM summary_tools WHERE day IN ({day_ph})", day_args)
            if session_args:
                c.execute(f"DELETE FROM summary_sessions WHERE session_id IN ({session_ph})", session_args)

        c.execute("""
          INSERT INTO summary_daily (
            day, turns, input_tokens, output_tokens, cache_read_tokens,
            cache_create_5m_tokens, cache_create_1h_tokens
          )
          SELECT substr(timestamp, 1, 10) AS day,
                 SUM(CASE WHEN type='user' AND prompt_text IS NOT NULL THEN 1 ELSE 0 END) AS turns,
                 COALESCE(SUM(input_tokens),0),
                 COALESCE(SUM(output_tokens),0),
                 COALESCE(SUM(cache_read_tokens),0),
                 COALESCE(SUM(cache_create_5m_tokens),0),
                 COALESCE(SUM(cache_create_1h_tokens),0)
            FROM messages
           WHERE timestamp IS NOT NULL
        """ + day_filter + """
           GROUP BY day
        """, day_args)

        c.execute("""
          INSERT INTO summary_projects (
            day, project_slug, sample_cwd, turns, input_tokens, output_tokens,
            cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens
          )
          SELECT substr(timestamp, 1, 10) AS day,
                 project_slug,
                 MIN(cwd),
                 SUM(CASE WHEN type='user' AND prompt_text IS NOT NULL THEN 1 ELSE 0 END) AS turns,
                 COALESCE(SUM(input_tokens),0),
                 COALESCE(SUM(output_tokens),0),
                 COALESCE(SUM(cache_read_tokens),0),
                 COALESCE(SUM(cache_create_5m_tokens),0),
                 COALESCE(SUM(cache_create_1h_tokens),0)
            FROM messages
           WHERE timestamp IS NOT NULL
        """ + day_filter + """
           GROUP BY day, project_slug
        """, day_args)

        c.execute("""
          INSERT INTO summary_models (
            day, model, turns, input_tokens, output_tokens, cache_read_tokens,
            cache_create_5m_tokens, cache_create_1h_tokens
          )
          SELECT substr(timestamp, 1, 10) AS day,
                 COALESCE(model, 'unknown') AS model,
                 COUNT(*) AS turns,
                 COALESCE(SUM(input_tokens),0),
                 COALESCE(SUM(output_tokens),0),
                 COALESCE(SUM(cache_read_tokens),0),
                 COALESCE(SUM(cache_create_5m_tokens),0),
                 COALESCE(SUM(cache_create_1h_tokens),0)
            FROM messages
           WHERE type='assistant' AND timestamp IS NOT NULL
        """ + day_filter + """
           GROUP BY day, COALESCE(model, 'unknown')
        """, day_args)

        c.execute("""
          INSERT INTO summary_tools (day, tool_name, calls, result_tokens)
          SELECT substr(timestamp, 1, 10) AS day,
                 tool_name,
                 COUNT(*) AS calls,
                 COALESCE(SUM(result_tokens),0) AS result_tokens
            FROM tool_calls
           WHERE tool_name != '_tool_result' AND timestamp IS NOT NULL
        """ + day_filter + """
           GROUP BY day, tool_name
        """, day_args)

        c.execute("""
          INSERT INTO summary_sessions (
            session_id, project_slug, sample_cwd, started, ended, turns,
            input_tokens, output_tokens, cache_read_tokens,
            cache_create_5m_tokens, cache_create_1h_tokens
          )
          SELECT session_id,
                 MIN(project_slug),
                 MIN(cwd),
                 MIN(timestamp),
                 MAX(timestamp),
                 SUM(CASE WHEN type='user' AND prompt_text IS NOT NULL THEN 1 ELSE 0 END) AS turns,
                 COALESCE(SUM(input_tokens),0),
                 COALESCE(SUM(output_tokens),0),
                 COALESCE(SUM(cache_read_tokens),0),
                 COALESCE(SUM(cache_create_5m_tokens),0),
                 COALESCE(SUM(cache_create_1h_tokens),0)
            FROM messages
           WHERE 1=1
        """ + session_filter + """
           GROUP BY session_id
        """, session_args)

        c.execute(
            "INSERT OR REPLACE INTO summary_meta (k, v) VALUES ('last_rebuild', strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )
        c.commit()


def _session_range_clause(since, until):
    where, args = [], []
    if since:
        where.append("ended >= ?"); args.append(since)
    if until:
        where.append("started < ?"); args.append(until)
    return ((" WHERE " + " AND ".join(where)) if where else "", args)


def _encode_slug(path: str) -> str:
    """Claude Code's project-slug encoding: each of `:`, `\\`, `/`, space → one `-`."""
    return re.sub(r"[:\\/ ]", "-", path)


def _walk_to_root(cwd: str, slug: str) -> Optional[str]:
    """If any ancestor of cwd encodes to slug, return that ancestor's basename."""
    if not cwd or not slug:
        return None
    trimmed = cwd.rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    parts = trimmed.split(sep)
    for i in range(len(parts), 0, -1):
        if _encode_slug(sep.join(parts[:i])) == slug:
            name = parts[i - 1]
            if name:
                return name
    return None


def project_name_for(cwd: Optional[str], fallback_slug: str) -> str:
    """Pretty project name from a single cwd + slug (best-effort).

    For the multi-cwd case, prefer `best_project_name`.
    """
    name = _walk_to_root(cwd or "", fallback_slug or "")
    if name:
        return name
    if cwd:
        trimmed = cwd.rstrip("/\\")
        sep = "\\" if "\\" in trimmed else "/"
        tail = trimmed.split(sep)[-1]
        if tail:
            return tail
    if fallback_slug:
        parts = [p for p in re.split(r"-+", fallback_slug) if p]
        if parts:
            return parts[-1]
    return fallback_slug or ""


def best_project_name(cwds, slug: str) -> str:
    """Pick a pretty name from a list of cwds.

    Prefer a cwd whose walk-up matches `slug` (a true descendant of the project
    root). If none match, fall back to `project_name_for` on the first cwd,
    then to the slug's last segment.
    """
    cwds = [c for c in (cwds or []) if c]
    for cwd in cwds:
        name = _walk_to_root(cwd, slug)
        if name:
            return name
    return project_name_for(cwds[0] if cwds else None, slug)


def overview_totals(db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' AND prompt_text IS NOT NULL THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages WHERE 1=1 {rng}
    """
    with connect(db_path) as c:
        if _summary_ready(c) and _day_aligned(since) and _day_aligned(until):
            day_rng, day_args = _date_range_clause(since, until, col="day")
            sess_where, sess_args = _session_range_clause(since, until)
            totals = dict(c.execute(f"""
              SELECT COALESCE(SUM(turns),0)                  AS turns,
                     COALESCE(SUM(input_tokens),0)           AS input_tokens,
                     COALESCE(SUM(output_tokens),0)          AS output_tokens,
                     COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
                     COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
                     COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
                FROM summary_daily
               WHERE 1=1 {day_rng}
            """, day_args).fetchone())
            totals["sessions"] = c.execute(
                f"SELECT COUNT(*) FROM summary_sessions{sess_where}", sess_args
            ).fetchone()[0]
            return totals
        return dict(c.execute(sql, args).fetchone())


def expensive_prompts(db_path, limit: int = 50, sort: str = "tokens") -> list:
    """A typed user prompt and the main-thread assistant work it triggered.

    sort="tokens" (default) → largest billable first.
    sort="recent"           → newest first.

    Linkage is by session + time window, NOT by ``parent_uuid``: newer Claude
    Code versions insert ``attachment`` records between the typed prompt and the
    assistant turn, so ``assistant.parent_uuid`` no longer points at the prompt
    (and assistant rows carry no ``prompt_id``). For each session we take the
    typed prompt (``prompt_text`` set, main thread) — collapsing the injected
    ``list[text]`` user messages that share a ``prompt_id`` to the earliest row —
    then attribute every main-thread assistant message between this prompt and
    the next one to it. Sidechain (subagent) rows are excluded; their spend is
    the Subagents tab's concern. ``billable_tokens`` is therefore the full turn's
    cost (incl. its tool loop), not just the first response. The displayed
    ``model`` is the first assistant in the window that *has* a model, so a
    leading API-error row (``model`` NULL) neither sets the label nor drops the
    prompt via the outer ``model IS NOT NULL`` filter.
    """
    order = "timestamp DESC" if sort == "recent" else "billable_tokens DESC"
    # Window predicate shared by the three correlated aggregates below.
    win = ("a.session_id=pr.session_id AND a.type='assistant' "
           "AND COALESCE(a.is_sidechain,0)=0 AND a.timestamp>=pr.timestamp "
           "AND (pr.next_ts IS NULL OR a.timestamp<pr.next_ts)")
    sql = f"""
      WITH typed AS (
        SELECT session_id, project_slug, uuid, prompt_text, prompt_chars, timestamp,
               ROW_NUMBER() OVER (PARTITION BY session_id, COALESCE(prompt_id, uuid)
                                  ORDER BY timestamp, uuid) AS rn
          FROM messages
         WHERE type='user' AND prompt_text IS NOT NULL AND COALESCE(is_sidechain,0)=0
      ),
      prompts AS (
        SELECT session_id, project_slug, uuid, prompt_text, prompt_chars, timestamp,
               LEAD(timestamp) OVER (PARTITION BY session_id ORDER BY timestamp, uuid) AS next_ts
          FROM typed WHERE rn=1
      )
      SELECT * FROM (
        SELECT pr.uuid AS user_uuid, pr.session_id, pr.project_slug, pr.timestamp,
               pr.prompt_text, pr.prompt_chars,
               (SELECT a.model FROM messages a
                 WHERE {win} AND a.model IS NOT NULL ORDER BY a.timestamp LIMIT 1) AS model,
               COALESCE((SELECT SUM(COALESCE(a.input_tokens,0)+COALESCE(a.output_tokens,0)
                          +COALESCE(a.cache_create_5m_tokens,0)+COALESCE(a.cache_create_1h_tokens,0))
                 FROM messages a WHERE {win}), 0) AS billable_tokens,
               COALESCE((SELECT SUM(COALESCE(a.cache_read_tokens,0))
                 FROM messages a WHERE {win}), 0) AS cache_read_tokens
          FROM prompts pr
      )
       WHERE model IS NOT NULL
       ORDER BY {order}
       LIMIT ?
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (limit,))]


def project_summary(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT project_slug,
             MIN(cwd) AS sample_cwd,
             COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' AND prompt_text IS NOT NULL THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens), 0)  AS input_tokens,
             COALESCE(SUM(output_tokens), 0) AS output_tokens,
             SUM(input_tokens)+SUM(output_tokens)
               +SUM(cache_create_5m_tokens)+SUM(cache_create_1h_tokens) AS billable_tokens,
             SUM(cache_read_tokens) AS cache_read_tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY project_slug
       ORDER BY billable_tokens DESC
    """
    with connect(db_path) as c:
        if _summary_ready(c) and _day_aligned(since) and _day_aligned(until):
            day_rng, day_args = _date_range_clause(since, until, col="day")
            sess_where, sess_args = _session_range_clause(since, until)
            session_sql = f"""
              SELECT project_slug, COUNT(*) AS sessions
                FROM summary_sessions
                {sess_where}
               GROUP BY project_slug
            """
            rows = [dict(r) for r in c.execute(f"""
              SELECT p.project_slug,
                     MIN(p.sample_cwd) AS sample_cwd,
                     COALESCE(s.sessions, 0) AS sessions,
                     COALESCE(SUM(p.turns), 0) AS turns,
                     COALESCE(SUM(p.input_tokens), 0) AS input_tokens,
                     COALESCE(SUM(p.output_tokens), 0) AS output_tokens,
                     COALESCE(SUM(p.input_tokens),0)+COALESCE(SUM(p.output_tokens),0)
                       +COALESCE(SUM(p.cache_create_5m_tokens),0)
                       +COALESCE(SUM(p.cache_create_1h_tokens),0) AS billable_tokens,
                     COALESCE(SUM(p.cache_read_tokens), 0) AS cache_read_tokens
                FROM summary_projects p
                LEFT JOIN ({session_sql}) s ON s.project_slug = p.project_slug
               WHERE 1=1 {day_rng}
               GROUP BY p.project_slug
               ORDER BY billable_tokens DESC
            """, (*sess_args, *day_args))]
            for r in rows:
                r["project_name"] = project_name_for(r.pop("sample_cwd", None), r["project_slug"])
            return rows
        rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["project_name"] = project_name_for(r.pop("sample_cwd", None), r["project_slug"])
    return rows


def tool_token_breakdown(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT tool_name,
             COUNT(*) AS calls,
             COALESCE(SUM(result_tokens),0) AS result_tokens
        FROM tool_calls
       WHERE tool_name != '_tool_result' {rng}
       GROUP BY tool_name
       ORDER BY calls DESC
    """
    with connect(db_path) as c:
        if _summary_ready(c) and _day_aligned(since) and _day_aligned(until):
            day_rng, day_args = _date_range_clause(since, until, col="day")
            return [dict(r) for r in c.execute(f"""
              SELECT tool_name,
                     COALESCE(SUM(calls),0) AS calls,
                     COALESCE(SUM(result_tokens),0) AS result_tokens
                FROM summary_tools
               WHERE 1=1 {day_rng}
               GROUP BY tool_name
               ORDER BY calls DESC
            """, day_args)]
        return [dict(r) for r in c.execute(sql, args)]


def recent_sessions(db_path, limit: int = 20, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT session_id, project_slug,
             MIN(cwd) AS sample_cwd,
             MIN(timestamp) AS started, MAX(timestamp) AS ended,
             SUM(CASE WHEN type='user' AND prompt_text IS NOT NULL THEN 1 ELSE 0 END) AS turns,
             SUM(input_tokens)+SUM(output_tokens) AS tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY session_id
       ORDER BY ended DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        if _summary_ready(c):
            sess_where, sess_args = _session_range_clause(since, until)
            rows = [dict(r) for r in c.execute(f"""
              SELECT session_id, project_slug, sample_cwd,
                     started, ended, turns,
                     input_tokens + output_tokens AS tokens
                FROM summary_sessions
                {sess_where}
               ORDER BY ended DESC
               LIMIT ?
            """, (*sess_args, limit))]
            for r in rows:
                r["project_name"] = project_name_for(r.pop("sample_cwd", None), r["project_slug"])
            return rows
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        for r in rows:
            r["project_name"] = project_name_for(r.pop("sample_cwd", None), r["project_slug"])
    return rows


def session_model_tokens(db_path, session_ids) -> dict:
    """Per-(session, model) token sums for the given sessions.

    Returns {session_id: [ {model, input_tokens, ...}, ... ]}. Reads from the
    raw messages table (summary_sessions has no per-model breakdown), so the
    caller can compute accurate cost across sessions that mix models.
    """
    ids = [s for s in session_ids if s]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    sql = f"""
      SELECT session_id,
             COALESCE(model, 'unknown') AS model,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE session_id IN ({ph}) AND type = 'assistant'
       GROUP BY session_id, COALESCE(model, 'unknown')
    """
    out: dict = {}
    with connect(db_path) as c:
        for r in c.execute(sql, ids):
            out.setdefault(r["session_id"], []).append(dict(r))
    return out


def session_turns(db_path, session_id: str) -> list:
    sql = """
      SELECT uuid, parent_uuid, type, timestamp, model, is_sidechain, agent_id,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, prompt_chars, tool_calls_json, project_slug, cwd
        FROM messages
       WHERE session_id = ?
       ORDER BY timestamp ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (session_id,))]


def daily_token_breakdown(db_path, since=None, until=None) -> list:
    """One row per day: stacked bar data for input/output/cache_read/cache_create."""
    rng, args = _date_range_clause(since, until)
    sql = f"""
      SELECT substr(timestamp, 1, 10) AS day,
             COALESCE(SUM(input_tokens),0)      AS input_tokens,
             COALESCE(SUM(output_tokens),0)     AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)
               + COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_tokens
        FROM messages
       WHERE timestamp IS NOT NULL {rng}
       GROUP BY day
       ORDER BY day ASC
    """
    with connect(db_path) as c:
        if _summary_ready(c):
            day_rng, day_args = _date_range_clause(since, until, col="day")
            return [dict(r) for r in c.execute(f"""
              SELECT day,
                     input_tokens,
                     output_tokens,
                     cache_read_tokens,
                     cache_create_5m_tokens + cache_create_1h_tokens AS cache_create_tokens
                FROM summary_daily
               WHERE 1=1 {day_rng}
               ORDER BY day ASC
            """, day_args)]
        return [dict(r) for r in c.execute(sql, args)]


def skill_breakdown(db_path, since=None, until=None) -> list:
    """Per-skill counts, split between user-initiated and Claude-initiated.

    Two distinct counts per row, both attributable to the same skill/command:

    - ``manual_sessions``: distinct sessions whose messages carry an
      ``attribution_skill`` value — i.e. the user typed ``/skill-name``
      and the session ran under that skill.
    - ``tool_invocations``: ``Skill`` tool-use blocks that Claude emitted
      inside a session, typically from Task/Agent-dispatched subagents.

    The fork also synthesises a ``Skill`` tool_calls row for every typed
    slash command (so historical DBs without the ``attribution_skill``
    column still see slash-command activity). Those synthesised rows live
    on user-type messages; real ``Skill`` tool_use rows live on
    assistant-type messages. The ``tool_inv`` CTE filters synthesised rows
    out via an ``EXISTS`` on ``messages.type='assistant'`` so the same
    slash command never counts in both columns.

    Token attribution per skill is not included: a Skill's content is
    loaded via a system-reminder on the next turn, not as the tool_result
    body. ``skill_costs`` / ``skill_actuals`` cover that separately.
    """
    rng_tc, args_tc = _range_clause(since, until)
    rng_ms, args_ms = _range_clause(since, until)
    # Simulate FULL OUTER JOIN via UNION (FULL OUTER JOIN requires SQLite ≥ 3.39).
    sql = f"""
      WITH tool_inv AS (
        SELECT tc.target AS skill,
               COUNT(*) AS tool_invocations,
               COUNT(DISTINCT tc.session_id) AS tool_sessions,
               MAX(tc.timestamp) AS last_used_tool
          FROM tool_calls tc
         WHERE tc.tool_name = 'Skill'
           AND tc.target IS NOT NULL
           AND tc.target != ''
           AND EXISTS (
             SELECT 1 FROM messages m
              WHERE m.uuid = tc.message_uuid AND m.type = 'assistant'
           )
           {rng_tc}
         GROUP BY tc.target
      ),
      manual_inv AS (
        SELECT attribution_skill AS skill,
               COUNT(DISTINCT session_id) AS manual_sessions,
               MAX(timestamp) AS last_used_manual
          FROM messages
         WHERE attribution_skill IS NOT NULL
           AND attribution_skill != ''
           {rng_ms}
         GROUP BY attribution_skill
      )
      SELECT skill, manual_sessions, tool_invocations, sessions, last_used FROM (
        SELECT
          COALESCE(t.skill, m.skill) AS skill,
          COALESCE(m.manual_sessions, 0)   AS manual_sessions,
          COALESCE(t.tool_invocations, 0)  AS tool_invocations,
          COALESCE(m.manual_sessions, 0) + COALESCE(t.tool_sessions, 0) AS sessions,
          MAX(COALESCE(t.last_used_tool, ''), COALESCE(m.last_used_manual, '')) AS last_used
        FROM tool_inv t LEFT JOIN manual_inv m ON t.skill = m.skill
        UNION
        SELECT
          m.skill,
          m.manual_sessions,
          0,
          m.manual_sessions,
          m.last_used_manual
        FROM manual_inv m LEFT JOIN tool_inv t ON m.skill = t.skill
        WHERE t.skill IS NULL
      )
      ORDER BY (manual_sessions + tool_invocations) DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args_tc + args_ms)]


def model_breakdown(db_path, since=None, until=None) -> list:
    """Per-model token totals + turn count. Caller computes cost via pricing."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    with connect(db_path) as c:
        if _summary_ready(c) and _day_aligned(since) and _day_aligned(until):
            day_rng, day_args = _date_range_clause(since, until, col="day")
            return [dict(r) for r in c.execute(f"""
              SELECT model,
                     COALESCE(SUM(turns),0) AS turns,
                     COALESCE(SUM(input_tokens),0) AS input_tokens,
                     COALESCE(SUM(output_tokens),0) AS output_tokens,
                     COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
                     COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
                     COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
                FROM summary_models
               WHERE 1=1 {day_rng}
               GROUP BY model
               ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
            """, day_args)]
        return [dict(r) for r in c.execute(sql, args)]


# --- cross-workspace + subagent attribution -------------------------------
# Source: upstream PR #22 (nateherkai/token-dashboard#22, drafted from fork,
# CLOSED upstream). Integrated 2026-05-28. Author-specific tier-routing
# bits (orchestrate / AEH / pareto-cascade-delivery / run-benchmark) were
# stripped pre-integration — see FORK_NOTES.md.


def _workspace_root_path(cwd: str, slug: str) -> Optional[str]:
    """Full ancestor path of cwd whose slug-encoding equals slug, or None.

    Unlike _walk_to_root (returns basename), this returns the full path so
    the caller can use it as a path prefix when classifying tool_call targets.
    """
    if not cwd or not slug:
        return None
    trimmed = cwd.rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    parts = trimmed.split(sep)
    for i in range(len(parts), 0, -1):
        candidate = sep.join(parts[:i])
        if _encode_slug(candidate) == slug:
            return candidate
    return None


def _normalize_path(p: str) -> str:
    """Lowercase, forward-slash-free, trailing-separator-stripped path for prefix matching."""
    return (p or "").replace("/", "\\").lower().rstrip("\\")


def _build_workspace_index(conn) -> list:
    """List of (root_path_normalized, workspace_name) sorted by length desc.

    Built once per request from distinct (cwd, project_slug) pairs. Used to
    classify tool_call target paths into a workspace.
    """
    by_slug: dict = {}
    for r in conn.execute(
        "SELECT DISTINCT cwd, project_slug FROM messages "
        "WHERE cwd IS NOT NULL AND project_slug IS NOT NULL"
    ):
        by_slug.setdefault(r["project_slug"], []).append(r["cwd"])
    seen = set()
    out = []
    candidates = []
    for slug, cwds in by_slug.items():
        name = best_project_name(cwds, slug)
        roots = set()
        for cwd in cwds:
            root = _workspace_root_path(cwd, slug)
            roots.add(root or cwd.rstrip("/\\"))
        for root in roots:
            norm = _normalize_path(root)
            if norm:
                candidates.append((norm, name))
    candidates.sort(key=lambda x: -len(x[0]))
    for prefix, name in candidates:
        if prefix in seen:
            continue
        seen.add(prefix)
        out.append((prefix, name))
    return out


def _classify_path(path: str, index: list) -> str:
    """Return workspace name for a path, or 'external' when no prefix matches."""
    if not path:
        return "external"
    norm = _normalize_path(path)
    for prefix, name in index:
        if norm == prefix or norm.startswith(prefix + "\\"):
            return name
    return "external"


_CROSS_WS_TOOLS = ("Read", "Edit", "Write", "NotebookEdit")


def workspaces_matrix(db_path, since=None, until=None) -> dict:
    """Cross-workspace tool-target flow data shaped for an ECharts Sankey.

    Only Read/Edit/Write/NotebookEdit calls are counted — they uniquely
    identify a file. Glob/Grep patterns are skipped (often workspace-free).

    Layout is bipartite: src nodes (left column, suffixed " (agent)") and
    dst nodes (right column, suffixed " (files)"). This guarantees a DAG
    even when the raw data has self-loops (agent reading its own workspace)
    or opposite-direction pairs (A->B and B->A both happen) — both would
    crash ECharts' Sankey, which requires acyclic input.
    """
    rng, args = _range_clause(since, until, col="t.timestamp")
    sql = f"""
      SELECT m.cwd AS src_cwd, m.project_slug AS src_slug,
             t.target AS target, COUNT(*) AS n
        FROM tool_calls t JOIN messages m ON t.message_uuid = m.uuid
       WHERE t.tool_name IN ({",".join("?" * len(_CROSS_WS_TOOLS))})
         AND t.target IS NOT NULL AND t.target != ''
         AND m.cwd IS NOT NULL AND m.project_slug IS NOT NULL
         {rng}
       GROUP BY src_cwd, src_slug, target
    """
    src_nodes: set = set()
    dst_nodes: set = set()
    matrix: dict = {}
    self_loop_calls = 0
    cross_calls = 0
    with connect(db_path) as c:
        index = _build_workspace_index(c)
        for row in c.execute(sql, (*_CROSS_WS_TOOLS, *args)):
            src = project_name_for(row["src_cwd"], row["src_slug"]) or row["src_slug"] or "unknown"
            dst = _classify_path(row["target"], index)
            n = row["n"]
            if src == dst:
                self_loop_calls += n
            else:
                cross_calls += n
            src_label = f"{src} (agent)"
            dst_label = f"{dst} (files)"
            src_nodes.add(src_label); dst_nodes.add(dst_label)
            key = (src_label, dst_label)
            matrix[key] = matrix.get(key, 0) + n
    return {
        "nodes": [{"name": n} for n in sorted(src_nodes) + sorted(dst_nodes)],
        "links": [{"source": s, "target": t, "value": v} for (s, t), v in matrix.items()],
        "total_calls": sum(matrix.values()),
        "self_loop_calls": self_loop_calls,
        "cross_workspace_calls": cross_calls,
        "tools_considered": list(_CROSS_WS_TOOLS),
    }


def cross_workspace_leaks(db_path, limit: int = 20, since=None, until=None) -> list:
    """Top (src_workspace -> dst_workspace) pairs where src != dst.

    Includes touched-session count and top files per pair so the UI can drill
    into the actual repeat-read targets.
    """
    rng, args = _range_clause(since, until, col="t.timestamp")
    sql = f"""
      SELECT m.cwd AS src_cwd, m.project_slug AS src_slug,
             m.session_id AS src_session, t.target AS target, COUNT(*) AS n
        FROM tool_calls t JOIN messages m ON t.message_uuid = m.uuid
       WHERE t.tool_name IN ({",".join("?" * len(_CROSS_WS_TOOLS))})
         AND t.target IS NOT NULL AND t.target != ''
         AND m.cwd IS NOT NULL AND m.project_slug IS NOT NULL
         {rng}
       GROUP BY src_cwd, src_slug, src_session, target
    """
    pair: dict = {}
    with connect(db_path) as c:
        index = _build_workspace_index(c)
        for row in c.execute(sql, (*_CROSS_WS_TOOLS, *args)):
            src = project_name_for(row["src_cwd"], row["src_slug"]) or row["src_slug"] or "unknown"
            dst = _classify_path(row["target"], index)
            if src == dst:
                continue
            key = (src, dst)
            pd = pair.setdefault(key, {"calls": 0, "sessions": set(), "files": {}})
            pd["calls"] += row["n"]
            pd["sessions"].add(row["src_session"])
            pd["files"][row["target"]] = pd["files"].get(row["target"], 0) + row["n"]
    out = []
    for (src, dst), pd in pair.items():
        top_files = sorted(pd["files"].items(), key=lambda x: -x[1])[:5]
        out.append({
            "source": src,
            "target": dst,
            "calls": pd["calls"],
            "sessions": len(pd["sessions"]),
            "top_files": [{"path": p, "n": n} for p, n in top_files],
        })
    out.sort(key=lambda x: -x["calls"])
    return out[:limit]


def subagent_breakdown(db_path, since=None, until=None) -> list:
    """Per-model breakdown split by main vs sidechain (subagent dispatched via Task).

    Surfaces the attribution that model_breakdown aggregates away — e.g., the
    fact that Sonnet usage is almost entirely from subagent dispatch.
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             is_sidechain,
             COUNT(*) AS messages,
             COUNT(DISTINCT session_id) AS sessions,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL AND model != '<synthetic>'
       {rng}
       GROUP BY model, is_sidechain
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


_AGENT_KIND_SQL = """
  CASE
    WHEN is_sidechain = 0 THEN 'main'
    WHEN agent_id LIKE 'acompact-%' THEN 'compact'
    ELSE 'subagent'
  END
"""


def dispatch_tree(db_path, limit: int = 100, since=None, until=None) -> list:
    """Reconstruct dispatcher-prompt -> subagent thread relationships.

    Approach: the first sidechain message of a subagent thread has
    parent_uuid=NULL (the thread starts fresh), so we can't walk the message
    tree to find the dispatcher. Instead we join on Agent tool_call timing —
    every Agent tool call in main thread spawns a sidechain thread in the
    same session within milliseconds. Matching by (session_id, timestamp
    >= tc.timestamp, first one) recovers the link with >95% accuracy.

    Edge case: when a single main turn dispatches several Agent tools in
    parallel, the matching collapses them onto the first subagent thread;
    those rows show up as one dispatcher row even though multiple subagents
    were spawned.
    """
    rng, args = _range_clause(since, until, col="tc.timestamp")
    sql = f"""
      WITH dispatch AS (
        SELECT tc.session_id   AS session_id,
               tc.message_uuid AS dispatcher_uuid,
               tc.timestamp    AS dispatched_at,
               tc.target       AS subagent_type,
               m.model         AS dispatcher_model,
               m.project_slug  AS project_slug,
               (SELECT s.agent_id FROM messages s
                 WHERE s.session_id = tc.session_id
                   AND s.is_sidechain = 1
                   AND s.timestamp >= tc.timestamp
                   AND s.agent_id IS NOT NULL
                   AND s.agent_id NOT LIKE 'acompact-%'
                 ORDER BY s.timestamp ASC LIMIT 1) AS agent_id
          FROM tool_calls tc
          JOIN messages m ON m.uuid = tc.message_uuid
         WHERE tc.tool_name IN ('Agent','Task')
         {rng}
      ),
      agg AS (
        SELECT agent_id,
               COUNT(*) AS thread_msgs,
               GROUP_CONCAT(DISTINCT COALESCE(model,'unknown')) AS models,
               COALESCE(SUM(input_tokens + output_tokens),0)  AS io_tokens,
               COALESCE(SUM(cache_read_tokens),0)             AS cache_read_tokens,
               COALESCE(SUM(input_tokens),0)                  AS input_tokens,
               COALESCE(SUM(output_tokens),0)                 AS output_tokens,
               COALESCE(SUM(cache_create_5m_tokens),0)        AS cache_create_5m_tokens,
               COALESCE(SUM(cache_create_1h_tokens),0)        AS cache_create_1h_tokens
          FROM messages
         WHERE is_sidechain = 1 AND type = 'assistant'
           AND agent_id IS NOT NULL AND agent_id NOT LIKE 'acompact-%'
         GROUP BY agent_id
      )
      SELECT d.dispatcher_uuid, d.dispatcher_model, d.session_id,
             d.project_slug, d.dispatched_at, d.agent_id, d.subagent_type,
             agg.thread_msgs, agg.models, agg.io_tokens, agg.cache_read_tokens,
             agg.input_tokens, agg.output_tokens,
             agg.cache_create_5m_tokens, agg.cache_create_1h_tokens
        FROM dispatch d
        JOIN agg ON agg.agent_id = d.agent_id
       WHERE d.agent_id IS NOT NULL
       ORDER BY agg.io_tokens DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        slug_cache: dict = {}
        for r in rows:
            slug = r["project_slug"]
            if slug not in slug_cache:
                cwds = [r2["cwd"] for r2 in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                    (slug,),
                )]
                slug_cache[slug] = best_project_name(cwds, slug)
            r["project_name"] = slug_cache[slug]
            r["models"] = sorted((r["models"] or "").split(",")) if r["models"] else []
    return rows


def orchestration_breakdown(db_path, since=None, until=None) -> dict:
    """Per-entrypoint + per-agent-kind attribution, including SDK external runs.

    Why this exists: /api/by-model and /api/subagents both aggregate dimensions
    that hide real attribution. In particular:

    * Auto-compaction (CC's internal context-compression subagent, agent_id
      LIKE 'acompact-*') is a separate kind from user-dispatched Task subagents
      but the original breakdown lumps both as 'sidechain'.
    * External orchestration via claude_agent_sdk (e.g. AEH-style harnesses
      invoking Opus/Sonnet/Haiku from a Python script) writes its own JSONL
      with entrypoint='sdk-py'/'sdk-ts'/'sdk-cli'. These NEVER go through the
      Task tool so they have no is_sidechain marker — they show as main-thread
      messages from the orchestrator's perspective. Splitting on entrypoint
      surfaces them.

    Returns:
      by_kind: rows per (kind, model) — kind in {main, compact, subagent}
      by_entrypoint: rows per (entrypoint, model) covering all assistant turns
      sdk_runs: SDK-entrypoint sessions grouped by cwd — the "external
        orchestration" clusters
    """
    rng, args = _range_clause(since, until)
    by_kind_sql = f"""
      SELECT {_AGENT_KIND_SQL} AS kind,
             COALESCE(model, 'unknown') AS model,
             COUNT(*) AS messages,
             COUNT(DISTINCT session_id) AS sessions,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL AND model != '<synthetic>'
       {rng}
       GROUP BY kind, model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    by_ep_sql = f"""
      SELECT COALESCE(entrypoint, 'unknown') AS entrypoint,
             COALESCE(model, 'unknown') AS model,
             COUNT(*) AS messages,
             COUNT(DISTINCT session_id) AS sessions,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL AND model != '<synthetic>'
       {rng}
       GROUP BY entrypoint, model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    sdk_runs_sql = f"""
      SELECT entrypoint, cwd, project_slug,
             COUNT(DISTINCT session_id) AS sessions,
             COUNT(*) AS messages,
             COALESCE(SUM(input_tokens + output_tokens),0) AS io_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             GROUP_CONCAT(DISTINCT COALESCE(model, 'unknown')) AS models
        FROM messages
       WHERE type = 'assistant' AND entrypoint LIKE 'sdk-%'
       {rng}
       GROUP BY entrypoint, cwd, project_slug
       ORDER BY io_tokens DESC
       LIMIT 25
    """
    with connect(db_path) as c:
        by_kind = [dict(r) for r in c.execute(by_kind_sql, args)]
        by_entrypoint = [dict(r) for r in c.execute(by_ep_sql, args)]
        sdk_runs = []
        slug_cache: dict = {}
        for row in c.execute(sdk_runs_sql, args):
            d = dict(row)
            slug = d["project_slug"]
            if slug not in slug_cache:
                cwds = [r2["cwd"] for r2 in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                    (slug,),
                )]
                slug_cache[slug] = best_project_name(cwds, slug)
            d["workspace"] = slug_cache[slug]
            d["models"] = sorted((d["models"] or "").split(",")) if d["models"] else []
            sdk_runs.append(d)
    return {
        "by_kind": by_kind,
        "by_entrypoint": by_entrypoint,
        "sdk_runs": sdk_runs,
    }


def top_subagent_sessions(db_path, limit: int = 20, since=None, until=None) -> list:
    """Sessions ranked by total subagent (sidechain) tokens spent."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT session_id,
             project_slug,
             COUNT(*) AS subagent_msgs,
             COALESCE(SUM(input_tokens + output_tokens),0) AS io_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             GROUP_CONCAT(DISTINCT COALESCE(model,'unknown')) AS models
        FROM messages
       WHERE is_sidechain = 1 AND type = 'assistant' AND model IS NOT NULL
       {rng}
       GROUP BY session_id
       ORDER BY io_tokens DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        slug_cache: dict = {}
        for r in rows:
            slug = r["project_slug"]
            if slug not in slug_cache:
                cwds = [row["cwd"] for row in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                    (slug,),
                )]
                slug_cache[slug] = best_project_name(cwds, slug)
            r["project_name"] = slug_cache[slug]
            r["models"] = sorted((r["models"] or "").split(",")) if r["models"] else []
    return rows
