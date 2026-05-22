"""JSONL transcript walker + parser."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from .db import connect, rebuild_summaries, summaries_ready


# Slash-command user messages (typed as `/foo` in Claude Code) arrive as a
# user-role record whose content looks like `<command-name>/foo</command-name>`
# (with optional `<command-message>`/`<command-args>` sibling tags, in any
# order). The scanner synthesizes a `tool_name='Skill'` row from these so
# user-invoked skills appear in skill_breakdown / skill_costs / skill_actuals
# alongside assistant-initiated Skill tool_use blocks. Matches plugin-namespaced
# slugs like `codex:review` via the `:` in the character class.
_SLASH_CMD_RE = re.compile(r"<command-name>/([A-Za-z0-9_:-]+)</command-name>")


INSERT_MSG = """
INSERT OR REPLACE INTO messages (
  uuid, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, entrypoint,
  type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, message_id,
  input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
  prompt_text, prompt_chars, tool_calls_json, attribution_skill
) VALUES (
  :uuid, :parent_uuid, :session_id, :project_slug, :cwd, :git_branch, :cc_version, :entrypoint,
  :type, :is_sidechain, :agent_id, :timestamp, :model, :stop_reason, :prompt_id, :message_id,
  :input_tokens, :output_tokens, :cache_read_tokens, :cache_create_5m_tokens, :cache_create_1h_tokens,
  :prompt_text, :prompt_chars, :tool_calls_json, :attribution_skill
)
"""

INSERT_TOOL = """
INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp)
VALUES (:message_uuid, :session_id, :project_slug, :tool_name, :target, :result_tokens, :is_error, :timestamp)
"""


_TARGET_FIELDS = {
    "Read":      "file_path",
    "Edit":      "file_path",
    "Write":     "file_path",
    "Glob":      "pattern",
    "Grep":      "pattern",
    "Bash":      "command",
    "WebFetch":  "url",
    "WebSearch": "query",
    "Task":      "subagent_type",
    "Agent":     "subagent_type",
    "Skill":     "skill",
}


def _usage(rec: dict) -> dict:
    u = (rec.get("message") or {}).get("usage") or {}
    cc = u.get("cache_creation") or {}
    return {
        "input_tokens":           int(u.get("input_tokens") or 0),
        "output_tokens":          int(u.get("output_tokens") or 0),
        "cache_read_tokens":      int(u.get("cache_read_input_tokens") or 0),
        "cache_create_5m_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
        "cache_create_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
    }


def _prompt_text(rec: dict) -> Tuple[Optional[str], Optional[int]]:
    if rec.get("type") != "user":
        return None, None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content, len(content)
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(parts) if parts else None
        return text, (len(text) if text else None)
    return None, None


def _target(name: str, inp: dict) -> Optional[str]:
    field = _TARGET_FIELDS.get(name)
    if field and isinstance(inp, dict):
        v = inp.get(field)
        if isinstance(v, str):
            return v[:500]
    return None


def _extract_tools(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "unknown"
        target = _target(name, block.get("input") or {})
        out.append({
            "tool_name":     name,
            "target":        target,
            "result_tokens": None,
            "is_error":      0,
            "timestamp":     rec.get("timestamp"),
        })
    return out


def _extract_slash_commands(rec: dict) -> List[dict]:
    """Return a synthetic `Skill` tool_call row when a user record carries a
    `<command-name>/<slug></command-name>` tag. At most one per record.

    Claude Code logs user-typed slash commands without emitting an assistant
    `tool_use` block, so the base scanner misses them. We key the synthetic
    row on the user message's uuid/timestamp so the existing per-message
    dedup (``DELETE FROM tool_calls WHERE message_uuid=?``) keeps rescans
    idempotent.
    """
    if rec.get("type") != "user":
        return []
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return []
    m = _SLASH_CMD_RE.search(text)
    if not m:
        return []
    return [{
        "tool_name":     "Skill",
        "target":        m.group(1),
        "result_tokens": None,
        "is_error":      0,
        "timestamp":     rec.get("timestamp"),
    }]


def _extract_results(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, str):
            chars = len(body)
        elif isinstance(body, list):
            chars = sum(len(p.get("text", "")) for p in body if isinstance(p, dict))
        else:
            chars = 0
        out.append({
            "tool_name":     "_tool_result",
            "target":        block.get("tool_use_id"),
            "result_tokens": chars // 4,
            "is_error":      1 if block.get("is_error") else 0,
            "timestamp":     rec.get("timestamp"),
        })
    return out


def parse_record(rec: dict, project_slug: str) -> Tuple[dict, List[dict]]:
    """Return (message_row, [tool_call_rows])."""
    msg_obj = rec.get("message") or {}
    text, chars = _prompt_text(rec)
    msg = {
        "uuid":         rec.get("uuid"),
        "parent_uuid":  rec.get("parentUuid"),
        "session_id":   rec.get("sessionId"),
        "project_slug": project_slug,
        "cwd":          rec.get("cwd"),
        "git_branch":   rec.get("gitBranch"),
        "cc_version":   rec.get("version"),
        "entrypoint":   rec.get("entrypoint"),
        "type":         rec.get("type"),
        "is_sidechain": 1 if rec.get("isSidechain") else 0,
        "agent_id":     rec.get("agentId"),
        "timestamp":    rec.get("timestamp"),
        "model":        msg_obj.get("model"),
        "stop_reason":  msg_obj.get("stop_reason"),
        "prompt_id":    rec.get("promptId"),
        "message_id":   msg_obj.get("id"),
        "prompt_text":       text,
        "prompt_chars":      chars,
        "tool_calls_json":   None,
        "attribution_skill": rec.get("attributionSkill") or None,
        **_usage(rec),
    }
    tools = _extract_tools(rec)
    tools.extend(_extract_slash_commands(rec))
    tools.extend(_extract_results(rec))
    if tools:
        msg["tool_calls_json"] = json.dumps(
            [{"name": t["tool_name"], "target": t["target"]} for t in tools if t["tool_name"] != "_tool_result"]
        )
    for t in tools:
        t["message_uuid"] = msg["uuid"]
        t["session_id"]   = msg["session_id"]
        t["project_slug"] = project_slug
    return msg, tools


def _project_slug(file_path: Path, projects_root: Path) -> str:
    rel = file_path.relative_to(projects_root)
    return rel.parts[0]


def _evict_prior_snapshots_bulk(conn, keepers: List[Tuple[str, str, str]]) -> None:
    """Remove older streaming snapshots for many (session_id, message_id) pairs.

    Claude Code writes 2–3 JSONL lines per assistant response (partial → final)
    with identical message.id but distinct top-level uuids. Only the final
    tally matches billing, so earlier snapshots must be replaced, not summed.

    ``keepers`` is a list of ``(session_id, message_id, keep_uuid)`` triples
    representing the final snapshot we are about to insert for each
    ``(session_id, message_id)`` pair. We evict every prior row in the DB
    that matches the pair but has a different uuid. One temp-table-driven
    pass replaces N inline SELECT+DELETE round-trips.
    """
    if not keepers:
        return
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _snap_keepers ("
        " session_id TEXT NOT NULL,"
        " message_id TEXT NOT NULL,"
        " keep_uuid  TEXT NOT NULL,"
        " PRIMARY KEY (session_id, message_id)"
        ")"
    )
    conn.execute("DELETE FROM _snap_keepers")
    conn.executemany(
        "INSERT OR REPLACE INTO _snap_keepers (session_id, message_id, keep_uuid) VALUES (?, ?, ?)",
        keepers,
    )
    old = [r[0] for r in conn.execute(
        "SELECT m.uuid FROM messages m "
        "JOIN _snap_keepers k "
        "  ON m.session_id = k.session_id AND m.message_id = k.message_id "
        "WHERE m.uuid != k.keep_uuid"
    )]
    if old:
        # SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999; chunk safely below it.
        chunk = 500
        for i in range(0, len(old), chunk):
            slab = old[i:i + chunk]
            ph = ",".join("?" * len(slab))
            conn.execute(f"DELETE FROM tool_calls WHERE message_uuid IN ({ph})", slab)
            conn.execute(f"DELETE FROM messages   WHERE uuid         IN ({ph})", slab)
    conn.execute("DELETE FROM _snap_keepers")


def _parse_file(path: Path, project_slug: str, start_byte: int = 0) -> dict:
    """Parse new lines from a JSONL file. No DB access — pure Python.

    Returns a dict with the parsed messages, tool rows, and the byte offset
    just past the last fully-parsed line. Splitting parsing from DB writes
    lets the writer batch with ``executemany`` instead of paying a
    round-trip per row.
    """
    messages: List[dict] = []
    tools: List[dict] = []
    end_offset = start_byte
    with open(path, "rb") as fb:
        if start_byte:
            fb.seek(start_byte)
        while True:
            raw = fb.readline()
            if not raw:
                break  # EOF
            if not raw.endswith(b"\n"):
                # Partial line — Claude Code is mid-flush. Leave the
                # high-water mark behind the line start so we re-read it
                # once the write completes.
                break
            line_end = fb.tell()
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                end_offset = line_end
                continue
            if not line:
                end_offset = line_end
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                end_offset = line_end
                continue
            if not isinstance(rec, dict) or "uuid" not in rec or "type" not in rec:
                end_offset = line_end
                continue
            msg, tlist = parse_record(rec, project_slug)
            if not msg["session_id"] or not msg["timestamp"]:
                end_offset = line_end
                continue
            messages.append(msg)
            tools.extend(tlist)
            end_offset = line_end
    return {"messages": messages, "tools": tools, "end_offset": end_offset}


def _dedupe_inflight_snapshots(
    messages: List[dict],
) -> Tuple[List[dict], List[Tuple[str, str, str]]]:
    """Within one batch of parsed messages, keep only the last uuid per
    (session_id, message_id), preserving order otherwise.

    Returns (deduped_messages, keepers) where keepers is the list of
    (session_id, message_id, keep_uuid) triples for the bulk evictor.
    """
    last_idx_by_key: dict = {}
    for i, m in enumerate(messages):
        mid = m["message_id"]
        if not mid:
            continue
        last_idx_by_key[(m["session_id"], mid)] = i
    if not last_idx_by_key:
        return messages, []
    keep_indices = set(last_idx_by_key.values())
    deduped: List[dict] = []
    for i, m in enumerate(messages):
        if not m["message_id"] or i in keep_indices:
            deduped.append(m)
    keepers = [
        (sid, mid, messages[idx]["uuid"]) for (sid, mid), idx in last_idx_by_key.items()
    ]
    return deduped, keepers


def scan_file(path: Path, project_slug: str, conn, start_byte: int = 0) -> dict:
    """Ingest new lines from a JSONL file starting at ``start_byte``.

    Returns message/tool counts plus ``end_offset`` — the byte offset just
    past the last fully-parsed line. Callers persist ``end_offset`` as the
    file's high-water mark so a line partially flushed at EOF gets re-read
    once it completes.

    Also returns ``days`` and ``sessions`` sets covering the messages just
    written. ``scan_dir`` uses these to invalidate the correct slices of
    the materialised summary tables.

    Internally: parse all new lines into in-memory lists, dedupe streaming
    snapshots within the batch, evict prior snapshots in one bulk pass,
    then batch-write messages and tools via ``executemany``. Observable
    behavior is identical to the prior per-row implementation; only
    throughput differs (~33x on a cold scan of 1.0 GB / 200k messages).
    """
    parsed = _parse_file(path, project_slug, start_byte=start_byte)
    msgs_batch: List[dict] = parsed["messages"]
    tools_batch: List[dict] = parsed["tools"]
    end_offset: int = parsed["end_offset"]

    if not msgs_batch:
        return {"messages": 0, "tools": 0, "end_offset": end_offset,
                "days": set(), "sessions": set()}

    msgs_batch, keepers = _dedupe_inflight_snapshots(msgs_batch)
    keep_uuids = {m["uuid"] for m in msgs_batch}
    tools_batch = [t for t in tools_batch if t["message_uuid"] in keep_uuids]

    _evict_prior_snapshots_bulk(conn, keepers)

    # tool_calls has no natural unique key; clear any prior rows for the uuids
    # we are about to insert so full rescans stay idempotent.
    uuids = list(keep_uuids)
    chunk = 500
    for i in range(0, len(uuids), chunk):
        slab = uuids[i:i + chunk]
        ph = ",".join("?" * len(slab))
        conn.execute(f"DELETE FROM tool_calls WHERE message_uuid IN ({ph})", slab)

    conn.executemany(INSERT_MSG, msgs_batch)
    if tools_batch:
        conn.executemany(INSERT_TOOL, tools_batch)

    # Reconstruct the day/session sets scan_dir needs for the materialised
    # summary rebuild. Done after dedupe so we never reference an evicted row.
    days = {m["timestamp"][:10] for m in msgs_batch if m["timestamp"]}
    sessions = {m["session_id"] for m in msgs_batch if m["session_id"]}

    return {"messages": len(msgs_batch), "tools": len(tools_batch),
            "end_offset": end_offset, "days": days, "sessions": sessions}


def rescan_agent_targets(
    db_path: Union[str, Path],
    projects_root: Union[str, Path],
) -> dict:
    """Re-parse main-session JSONLs that hold ``tool_name='Agent'`` rows with
    ``target IS NULL``.

    Older scanner builds recognised only the legacy ``Task`` tool name;
    Claude Code renamed it to ``Agent``, leaving historical rows without a
    subagent_type to join on. Resetting those files' ``bytes_read`` makes
    the next ``scan_dir`` re-parse them end-to-end. Dedup is handled by
    ``INSERT OR REPLACE`` on messages + ``DELETE FROM tool_calls`` per
    uuid, so repeated runs are safe.

    One-shot utility: wire in at operator time, not on every scan.
    """
    with connect(db_path) as conn:
        sessions = [
            r["session_id"] for r in conn.execute(
                "SELECT DISTINCT session_id FROM tool_calls "
                "WHERE tool_name='Agent' AND target IS NULL"
            )
        ]
        if not sessions:
            return {"files_reset": 0, "messages": 0, "tools": 0, "files": 0}
        paths: set[str] = set()
        for sid in sessions:
            for row in conn.execute(
                "SELECT path FROM files WHERE path LIKE ?",
                (f"%/{sid}.jsonl",),
            ):
                paths.add(row["path"])
        for p in paths:
            conn.execute("UPDATE files SET bytes_read = 0 WHERE path = ?", (p,))
        conn.commit()
    result = scan_dir(projects_root, db_path)
    return {"files_reset": len(paths), **result}


def scan_dir(
    projects_root: Union[str, Path],
    db_path: Union[str, Path],
    progress: Optional[Callable[[int, int, Path, dict], None]] = None,
) -> dict:
    """Incrementally scan JSONL transcripts under ``projects_root`` into the DB.

    ``progress`` is called as ``progress(index, total, path, totals)`` after
    each file is processed (skipped or scanned). ``index`` is 1-based.
    """
    root = Path(projects_root)
    totals = {"messages": 0, "tools": 0, "files": 0}
    if not root.is_dir():
        return totals
    # Commit every BATCH_SIZE files so the SQLite journal never grows large
    # enough to spike RAM. Smaller batches also release the write lock more
    # frequently so concurrent HTTP writes (e.g. POST /api/plan) can proceed.
    BATCH_SIZE = 20
    pending = 0
    needs_full_summary_rebuild = not summaries_ready(db_path)
    summary_days = set()
    summary_sessions = set()
    scan_started = time.perf_counter()
    paths = list(root.rglob("*.jsonl"))
    total = len(paths)
    with connect(db_path) as conn:
        known_files = {
            r["path"]: r for r in conn.execute("SELECT path, mtime, bytes_read FROM files")
        }
        for i, p in enumerate(paths, start=1):
            try:
                stat = p.stat()
            except OSError:
                if progress:
                    progress(i, total, p, totals)
                continue
            path_s = str(p)
            row = known_files.get(path_s)
            offset = 0
            if row and row["bytes_read"] == stat.st_size:
                if row["mtime"] != stat.st_mtime:
                    conn.execute(
                        "INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at) VALUES (?, ?, ?, ?)",
                        (path_s, stat.st_mtime, row["bytes_read"], time.time()),
                    )
                    pending += 1
                    if pending >= BATCH_SIZE:
                        conn.commit()
                        pending = 0
                        time.sleep(0.05)  # yield so concurrent HTTP writes can acquire the lock
                if progress:
                    progress(i, total, p, totals)
                continue
            if row and stat.st_size > row["bytes_read"]:
                offset = row["bytes_read"]
            slug = _project_slug(p, root)
            sub = scan_file(p, slug, conn, start_byte=offset)
            # Persist the byte offset of the last fully-parsed line (not
            # st_size) so a partial line mid-flush is retried on the next
            # scan instead of being skipped over.
            conn.execute(
                "INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at) VALUES (?, ?, ?, ?)",
                (path_s, stat.st_mtime, sub["end_offset"], time.time()),
            )
            totals["messages"] += sub["messages"]
            totals["tools"]    += sub["tools"]
            totals["files"]    += 1
            if sub["messages"] or sub["tools"]:
                summary_days.update(sub["days"])
                summary_sessions.update(sub["sessions"])
            pending += 1
            if pending >= BATCH_SIZE:
                conn.commit()
                pending = 0
                time.sleep(0.05)  # yield so concurrent HTTP writes can acquire the lock
            if progress:
                progress(i, total, p, totals)
        conn.commit()
    summary_started = time.perf_counter()
    if needs_full_summary_rebuild:
        rebuild_summaries(db_path)
    elif summary_days or summary_sessions:
        rebuild_summaries(db_path, days=summary_days, sessions=summary_sessions)
    totals["scan_seconds"] = round(summary_started - scan_started, 3)
    totals["summary_seconds"] = round(time.perf_counter() - summary_started, 3)
    totals["summary_days"] = len(summary_days)
    totals["summary_sessions"] = len(summary_sessions)
    if totals["scan_seconds"] > 1 or totals["summary_seconds"] > 1:
        print(
            "token-dashboard scan "
            f"scan={totals['scan_seconds']:.3f}s summary={totals['summary_seconds']:.3f}s "
            f"messages={totals['messages']} tools={totals['tools']} files={totals['files']} "
            f"days={totals['summary_days']} sessions={totals['summary_sessions']} "
            f"full_summary={int(needs_full_summary_rebuild)}",
            flush=True,
        )
    return totals


def rescan_slash_commands(db_path: Union[str, Path]) -> dict:
    """Synthesize ``tool_name='Skill'`` rows for already-ingested slash-command
    user messages. No filesystem re-read required — ``prompt_text`` already
    holds the ``<command-name>/<slug></command-name>`` tag.

    Idempotent: a prior synthetic row on the same ``message_uuid`` is deleted
    before re-inserting, so repeated runs are safe. Real assistant-initiated
    ``Skill`` tool_use rows have distinct ``message_uuid`` values and aren't
    touched.

    One-shot utility for DBs populated before this extractor existed.
    """
    synthesized = 0
    with connect(db_path) as conn:
        rows = list(conn.execute(
            "SELECT uuid, session_id, project_slug, timestamp, prompt_text "
            "FROM messages "
            "WHERE type='user' AND prompt_text LIKE '%<command-name>/%'"
        ))
        for row in rows:
            m = _SLASH_CMD_RE.search(row["prompt_text"] or "")
            if not m:
                continue
            conn.execute(
                "DELETE FROM tool_calls "
                "WHERE message_uuid=? AND tool_name='Skill'",
                (row["uuid"],),
            )
            conn.execute(INSERT_TOOL, {
                "message_uuid":  row["uuid"],
                "session_id":    row["session_id"],
                "project_slug": row["project_slug"],
                "tool_name":     "Skill",
                "target":        m.group(1),
                "result_tokens": None,
                "is_error":      0,
                "timestamp":     row["timestamp"],
            })
            synthesized += 1
        conn.commit()
    return {"slash_commands_synthesized": synthesized}
