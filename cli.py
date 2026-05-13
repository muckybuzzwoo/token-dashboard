"""Token Dashboard CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from token_dashboard.db import (
    default_claude_dir,
    default_db_path,
    get_setting,
    init_db,
    overview_totals,
)
from token_dashboard.scanner import rescan_agent_targets, rescan_slash_commands, scan_dir
from token_dashboard.tips import all_tips


def _db_path(args) -> str:
    return args.db or os.environ.get("TOKEN_DASHBOARD_DB") or str(default_db_path())


def _projects_override(args) -> Optional[str]:
    return args.projects_dir or os.environ.get("CLAUDE_PROJECTS_DIR")


def _projects(args, db_path: Optional[str] = None) -> str:
    override = _projects_override(args)
    if override:
        return override
    if db_path:
        claude_dir = get_setting(db_path, "claude_dir")
        if claude_dir:
            return str(Path(claude_dir).expanduser() / "projects")
    return str(default_claude_dir() / "projects")


def _progress_printer():
    """Single-line stderr progress. Throttled to ~5 updates/sec so big scans
    don't spam the terminal but still prove the process is alive."""
    state = {"last": 0.0}
    is_tty = sys.stderr.isatty()

    def cb(i, total, path, totals):
        now = time.monotonic()
        final = (i == total)
        if not final and (now - state["last"] < 0.2):
            return
        state["last"] = now
        name = path.name
        if len(name) > 48:
            name = name[:45] + "..."
        line = (
            f"scanning {i}/{total}  "
            f"files={totals['files']} msgs={totals['messages']} tools={totals['tools']}  "
            f"{name}"
        )
        if is_tty:
            sys.stderr.write("\r\x1b[2K" + line)
            if final:
                sys.stderr.write("\n")
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    return cb


def _today_range():
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return start, end


def cmd_scan(args):
    db = _db_path(args)
    init_db(db)
    n = scan_dir(_projects(args, db), db, progress=_progress_printer())
    print(f"Token Dashboard: scanned {n['files']} files, {n['messages']} messages, {n['tools']} tool calls")


def cmd_rescan_agent_targets(args):
    db = _db_path(args)
    init_db(db)
    n = rescan_agent_targets(db, _projects(args))
    print(
        f"Token Dashboard: reset {n['files_reset']} files, "
        f"re-parsed {n['messages']} messages, {n['tools']} tool calls"
    )


def cmd_rescan_slash_commands(args):
    db = _db_path(args)
    init_db(db)
    n = rescan_slash_commands(db)
    print(
        f"Token Dashboard: synthesized {n['slash_commands_synthesized']} "
        f"Skill rows from historical slash-command messages"
    )


def cmd_today(args):
    db = _db_path(args)
    init_db(db)
    s, e = _today_range()
    t = overview_totals(db, since=s, until=e)
    print("Token Dashboard — today")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")
    print(f"  cache rd: {t['cache_read_tokens']:>12,}    cache cr: {t['cache_create_5m_tokens']+t['cache_create_1h_tokens']:>12,}")


def cmd_stats(args):
    db = _db_path(args)
    init_db(db)
    t = overview_totals(db)
    print("Token Dashboard — all time")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")


def cmd_tips(args):
    db = _db_path(args)
    init_db(db)
    tips = all_tips(db)
    if not tips:
        print("Token Dashboard: no suggestions")
        return
    for tip in tips:
        print(f"[{tip['category']}] {tip['title']}")
        print(f"  {tip['body']}\n")


def cmd_dashboard(args):
    db = _db_path(args)
    init_db(db)
    if not args.no_scan:
        scan_dir(_projects(args, db), db, progress=_progress_printer())
    from token_dashboard.server import run

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    url = f"http://{host}:{port}/"
    if not args.no_open:
        webbrowser.open(url)
    print(f"Token Dashboard listening on {url}")
    run(host, port, db, _projects_override(args))


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="SQLite path (default ~/.claude/token-dashboard.db)")
    common.add_argument("--projects-dir", help="JSONL root (default ~/.claude/projects)")

    p = argparse.ArgumentParser(prog="token-dashboard", description="Local Claude Code usage dashboard", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan",  parents=[common]).set_defaults(func=cmd_scan)
    sub.add_parser("rescan-agent-targets", parents=[common]).set_defaults(func=cmd_rescan_agent_targets)
    sub.add_parser("rescan-slash-commands", parents=[common]).set_defaults(func=cmd_rescan_slash_commands)
    sub.add_parser("today", parents=[common]).set_defaults(func=cmd_today)
    sub.add_parser("stats", parents=[common]).set_defaults(func=cmd_stats)
    sub.add_parser("tips",  parents=[common]).set_defaults(func=cmd_tips)
    d = sub.add_parser("dashboard", parents=[common])
    d.add_argument("--no-scan", action="store_true")
    d.add_argument("--no-open", action="store_true")
    d.set_defaults(func=cmd_dashboard)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
