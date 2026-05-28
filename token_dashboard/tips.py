"""Rule-based tips engine — produces actionable suggestions from SQLite.

Tip dict shape:
  {
    "key": str,                       # stable identifier for dismiss
    "category": str,                  # short label shown as badge text
    "severity": "info"|"warning"|"cost",
    "title": str,
    "body": str,
    "scope": str,                     # what this tip is about (project, file, session, ...)
    "links": [{"label": str, "href": str}],   # drill-down anchors (dashboard or external)
    "estimated_savings_usd": float | None,    # optional, where computable
  }
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from .db import connect, cross_workspace_leaks


def _iso_days_ago(today_iso: str, n: int) -> str:
    d = datetime.fromisoformat(today_iso.replace("Z", ""))
    return (d - timedelta(days=n)).isoformat()


def _key(category: str, scope: str) -> str:
    return f"{category}:{scope}"


def _is_dismissed(db_path, key: str) -> bool:
    with connect(db_path) as c:
        r = c.execute("SELECT dismissed_at FROM dismissed_tips WHERE tip_key=?", (key,)).fetchone()
    if not r:
        return False
    return (time.time() - r["dismissed_at"]) < 14 * 86400


def dismiss_tip(db_path, key: str) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO dismissed_tips (tip_key, dismissed_at) VALUES (?, ?)",
            (key, time.time()),
        )
        c.commit()


def _session_link(session_id: Optional[str], label: str = "Open session") -> Optional[dict]:
    if not session_id:
        return None
    return {"label": label, "href": f"#/sessions/{session_id}"}


def _doc_link(label: str, href: str) -> dict:
    return {"label": label, "href": href}


def _make_tip(*, key, category, severity, title, body, scope, links=None, savings=None) -> dict:
    return {
        "key": key,
        "category": category,
        "severity": severity,
        "title": title,
        "body": body,
        "scope": scope,
        "links": [l for l in (links or []) if l],
        "estimated_savings_usd": savings,
    }


def cache_discipline_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT project_slug,
             SUM(cache_read_tokens) AS cr,
             SUM(input_tokens + cache_create_5m_tokens + cache_create_1h_tokens) AS rebuild
        FROM messages
       WHERE type='assistant' AND timestamp >= ?
       GROUP BY project_slug
       HAVING (cr + rebuild) > 100000
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since,)):
            total = (row["cr"] or 0) + (row["rebuild"] or 0)
            hit = (row["cr"] or 0) / total if total else 0
            if hit < 0.40:
                key = _key("cache", row["project_slug"])
                if _is_dismissed(db_path, key):
                    continue
                worst_session = c.execute(
                    """SELECT session_id FROM messages
                        WHERE type='assistant' AND project_slug=? AND timestamp >= ?
                        GROUP BY session_id
                        ORDER BY SUM(cache_create_5m_tokens + cache_create_1h_tokens) DESC
                        LIMIT 1""",
                    (row["project_slug"], since),
                ).fetchone()
                links = [
                    _session_link(worst_session["session_id"] if worst_session else None,
                                  "Worst session in this project"),
                    _doc_link("Anthropic: prompt caching",
                              "https://platform.claude.com/docs/en/build-with-claude/prompt-caching"),
                ]
                out.append(_make_tip(
                    key=key, category="cache", severity="warning",
                    title=f"Low cache hit rate in {row['project_slug']}",
                    body=(f"Cache hit rate is {hit*100:.0f}% over the last 7 days. "
                          "Pauses over 5 minutes invalidate the prompt cache, so sessions that "
                          "restart context frequently rebuild it. Consider longer-lived sessions "
                          "or fewer /clear resets."),
                    scope=row["project_slug"],
                    links=links,
                ))
    return out


def repeated_target_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        for row in c.execute("""
          SELECT target, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions
            FROM tool_calls
           WHERE tool_name IN ('Read','Edit','Write') AND timestamp >= ?
           GROUP BY target HAVING n > 10
           ORDER BY n DESC LIMIT 10
        """, (since,)):
            key = _key("repeat-file", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            worst = c.execute(
                """SELECT session_id FROM tool_calls
                    WHERE tool_name IN ('Read','Edit','Write')
                      AND target=? AND timestamp >= ?
                    GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1""",
                (row["target"], since),
            ).fetchone()
            out.append(_make_tip(
                key=key, category="repeat-file", severity="info",
                title=f"{row['target']} read {row['n']} times",
                body=(f"This file was opened {row['n']} times across {row['sessions']} sessions "
                      "in the past 7 days. A summary in CLAUDE.md or one read per session would "
                      "avoid repeats."),
                scope=row["target"],
                links=[_session_link(worst["session_id"] if worst else None, "Heaviest session")],
            ))
        for row in c.execute("""
          SELECT target, COUNT(*) AS n
            FROM tool_calls
           WHERE tool_name='Bash' AND timestamp >= ?
           GROUP BY target HAVING n > 15
           ORDER BY n DESC LIMIT 10
        """, (since,)):
            key = _key("repeat-bash", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            worst = c.execute(
                """SELECT session_id FROM tool_calls
                    WHERE tool_name='Bash' AND target=? AND timestamp >= ?
                    GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1""",
                (row["target"], since),
            ).fetchone()
            out.append(_make_tip(
                key=key, category="repeat-bash", severity="info",
                title=f"`{row['target']}` ran {row['n']} times",
                body=f"This bash command ran {row['n']} times in the past 7 days. Consider a watch flag or shell alias.",
                scope=row["target"],
                links=[_session_link(worst["session_id"] if worst else None, "Heaviest session")],
            ))
    return out


def right_size_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT COUNT(*) AS n,
             SUM(input_tokens+cache_create_5m_tokens+cache_create_1h_tokens) AS in_tok,
             SUM(output_tokens) AS out_tok
        FROM messages
       WHERE type='assistant' AND model LIKE '%opus%'
         AND output_tokens < 500 AND is_sidechain = 0
         AND timestamp >= ?
    """
    with connect(db_path) as c:
        row = c.execute(sql, (since,)).fetchone()
    if not row or (row["n"] or 0) < 10:
        return []
    api_opus   = ((row["in_tok"] or 0) * 15 + (row["out_tok"] or 0) * 75) / 1_000_000
    api_sonnet = ((row["in_tok"] or 0) *  3 + (row["out_tok"] or 0) * 15) / 1_000_000
    savings = api_opus - api_sonnet
    if savings < 1.0:
        return []
    key = _key("right-size", "opus-short-turns-7d")
    if _is_dismissed(db_path, key):
        return []
    return [_make_tip(
        key=key, category="right-size", severity="cost",
        title=f"{row['n']} short Opus turns might fit on Sonnet",
        body=(f"Opus turns under 500 output tokens cost ~${api_opus:.2f} in the last 7 days. "
              f"Sonnet would have cost ~${api_sonnet:.2f}."),
        scope="opus-short-turns-7d",
        links=[
            {"label": "Browse short prompts", "href": "#/prompts?sort=tokens"},
            _doc_link("Anthropic: choose the right model",
                      "https://code.claude.com/docs/en/costs"),
        ],
        savings=round(savings, 2),
    )]


def outlier_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Tool-result bloat + subagent token outliers.

    Tool-result threshold is 10k tokens — Claude Code displays an official
    warning at that level (Anthropic MCP doc). Severity escalates to `warning`
    when the average size is above 50k.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        big = c.execute("""
          SELECT COUNT(*) AS n, AVG(result_tokens) AS avg_t, MAX(result_tokens) AS max_t
            FROM tool_calls
           WHERE tool_name='_tool_result' AND result_tokens > 10000 AND timestamp >= ?
        """, (since,)).fetchone()
        if big and (big["n"] or 0) >= 5:
            avg_t = int(big["avg_t"] or 0)
            severity = "warning" if avg_t > 50_000 else "info"
            scope = "result-10k+"
            key = _key("tool-bloat", scope)
            if not _is_dismissed(db_path, key):
                worst = c.execute(
                    """SELECT session_id FROM tool_calls
                        WHERE tool_name='_tool_result' AND result_tokens > 10000
                          AND timestamp >= ?
                        ORDER BY result_tokens DESC LIMIT 1""",
                    (since,),
                ).fetchone()
                out.append(_make_tip(
                    key=key, category="tool-bloat", severity=severity,
                    title=f"{big['n']} tool results over 10k tokens this week",
                    body=(f"Average size {avg_t:,} tokens, biggest {int(big['max_t'] or 0):,}. "
                          "Claude Code warns above 10k. Pipe long Bash output to `head/tail` and "
                          "ask for narrower file reads or use a preprocess hook."),
                    scope=scope,
                    links=[
                        _session_link(worst["session_id"] if worst else None,
                                      "Session with biggest result"),
                        _doc_link("Anthropic: reduce MCP tool overhead",
                                  "https://code.claude.com/docs/en/mcp"),
                    ],
                ))
        for row in c.execute("""
          SELECT agent_id, COUNT(*) AS n,
                 AVG(input_tokens+output_tokens) AS mean_t,
                 MAX(input_tokens+output_tokens) AS max_t
            FROM messages
           WHERE is_sidechain=1 AND agent_id IS NOT NULL AND timestamp >= ?
           GROUP BY agent_id HAVING n >= 10
        """, (since,)):
            if (row["max_t"] or 0) > 6 * (row["mean_t"] or 1) and (row["max_t"] or 0) > 50_000:
                key = _key("subagent-outlier", row["agent_id"])
                if _is_dismissed(db_path, key):
                    continue
                worst = c.execute(
                    """SELECT session_id FROM messages
                        WHERE is_sidechain=1 AND agent_id=? AND timestamp >= ?
                        ORDER BY input_tokens+output_tokens DESC LIMIT 1""",
                    (row["agent_id"], since),
                ).fetchone()
                out.append(_make_tip(
                    key=key, category="subagent-outlier", severity="info",
                    title=f"Subagent {row['agent_id']} has cost outliers",
                    body=(f"Largest invocation used {int(row['max_t']):,} tokens vs mean "
                          f"{int(row['mean_t']):,}. Worth checking what those did differently."),
                    scope=row["agent_id"],
                    links=[_session_link(worst["session_id"] if worst else None,
                                         "Largest invocation")],
                ))
    return out


# ── New tip: skill-listing budget ─────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def _read_skill_description(path: str) -> str:
    """Return the `description:` value from a SKILL.md frontmatter, or empty string."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = _FRONTMATTER_RE.match(text)
    block = m.group(1) if m else text[:2000]
    d = _DESC_RE.search(block)
    return (d.group(1).strip() if d else "")


# Default context-window-1%-equivalent in characters.
# Claude Code default budget is 1% of context. With a 200k context window
# ~2000 tokens ≈ ~8000 chars. We use chars as a tokenizer-free proxy.
_SKILL_BUDGET_CHARS = 8000


def skill_listing_budget_tips(db_path, today_iso: Optional[str] = None,
                              budget_chars: int = _SKILL_BUDGET_CHARS) -> List[dict]:
    """Flag installed skills whose description footprint exceeds the listing budget.

    Cross-references the on-disk skill catalog (descriptions) with
    invocation counts from `tool_calls` so the worst offenders (unused or
    rarely used) can be pointed at directly.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 30)

    from .skills import cached_catalog
    catalog = cached_catalog(db_path)
    if not catalog:
        return []

    # Aggregate per slug. A SKILL.md may register multiple slugs (bare + plugin:bare).
    # Dedup by path so we count each file's description once.
    seen_paths: dict[str, int] = {}
    for info in catalog.values():
        seen_paths[info["path"]] = len(_read_skill_description(info["path"]))
    total_chars = sum(seen_paths.values())
    if total_chars <= budget_chars:
        return []

    with connect(db_path) as c:
        used = {r["target"]: r["n"] for r in c.execute(
            """SELECT target, COUNT(*) AS n
                 FROM tool_calls
                WHERE tool_name='Skill' AND target IS NOT NULL AND target != ''
                  AND timestamp >= ?
                GROUP BY target""",
            (since,),
        )}

    # Rank skills by least-used × largest description; those are the cheapest to drop.
    ranked = []
    for slug, info in catalog.items():
        ranked.append((used.get(slug, 0), -len(_read_skill_description(info["path"])), slug))
    ranked.sort()
    worst = [slug for _, _, slug in ranked[:5]]

    key = _key("skill-budget", "overall")
    if _is_dismissed(db_path, key):
        return []

    over_pct = (total_chars / budget_chars - 1) * 100
    body = (
        f"Installed skill descriptions total ~{total_chars:,} chars vs the default "
        f"~{budget_chars:,}-char budget (1% of context). Claude Code drops descriptions "
        "of the least-used skills first, so those skills stop auto-triggering. "
        f"Least-recently-used candidates: {', '.join(worst)}."
        if worst else
        f"Installed skill descriptions total ~{total_chars:,} chars vs ~{budget_chars:,}."
    )
    return [_make_tip(
        key=key, category="skill-budget", severity="warning",
        title=f"Skill-listing budget exceeded by ~{over_pct:.0f}%",
        body=body,
        scope="overall",
        links=[
            {"label": "Open Skills tab", "href": "#/skills"},
            _doc_link("Anthropic: extend Claude with skills",
                      "https://code.claude.com/docs/en/skills"),
            _doc_link("Issue #57599: budget exceeded warning",
                      "https://github.com/anthropics/claude-code/issues/57599"),
        ],
    )]


# ── New tip: CLAUDE.md size ───────────────────────────────────────────────────

_CLAUDE_MD_MAX_LINES = 200


def _distinct_active_cwds(db_path, since_iso: str) -> list[str]:
    with connect(db_path) as c:
        return [r[0] for r in c.execute(
            """SELECT cwd FROM messages
                WHERE cwd IS NOT NULL AND timestamp >= ?
                GROUP BY cwd
                HAVING COUNT(*) >= 5""",
            (since_iso,),
        )]


def claude_md_size_tips(db_path, today_iso: Optional[str] = None,
                         max_lines: int = _CLAUDE_MD_MAX_LINES) -> List[dict]:
    """Flag project CLAUDE.md files that exceed Anthropic's recommended ~200 lines."""
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 30)

    out = []
    seen_paths: set[str] = set()
    for cwd in _distinct_active_cwds(db_path, since):
        if not cwd:
            continue
        try:
            cwd_path = Path(cwd)
        except (TypeError, ValueError):
            continue
        # Walk up at most 6 levels so we catch monorepos with nested CLAUDE.md
        for ancestor in (cwd_path, *list(cwd_path.parents)[:5]):
            candidate = ancestor / "CLAUDE.md"
            spath = str(candidate)
            if spath in seen_paths:
                break
            try:
                if not candidate.is_file():
                    continue
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            seen_paths.add(spath)
            lines = text.count("\n") + (0 if text.endswith("\n") else 1)
            if lines > max_lines:
                scope = spath
                key = _key("claude-md-size", scope)
                if _is_dismissed(db_path, key):
                    break
                # Rough cost: chars-per-line * 0.25 token/char, charged each turn
                approx_tokens = (len(text) // 4)
                out.append(_make_tip(
                    key=key, category="claude-md-size", severity="info",
                    title=f"CLAUDE.md is {lines} lines — Anthropic suggests under {max_lines}",
                    body=(f"`{ancestor.name}/CLAUDE.md` weighs ~{approx_tokens:,} tokens and is "
                          "loaded every turn in this project. Move detailed workflows into "
                          "on-demand skills, keep only essentials here."),
                    scope=scope,
                    links=[
                        _doc_link("Anthropic: manage costs (CLAUDE.md size)",
                                  "https://code.claude.com/docs/en/costs"),
                    ],
                ))
                break  # one tip per cwd ancestry
    return out


def cross_workspace_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag when an agent in workspace X touches files in workspace Y > 50 times in 7d.

    Suggests moving the cross-referenced info into the source workspace's
    CLAUDE.md or memory so agents don't keep crossing.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    leaks = cross_workspace_leaks(db_path, limit=10, since=since)
    out: List[dict] = []
    for leak in leaks:
        if (leak["calls"] or 0) < 50:
            continue
        scope = f"{leak['source']}->{leak['target']}"
        key = _key("cross-workspace", scope)
        if _is_dismissed(db_path, key):
            continue
        body = (
            f"Sessions in {leak['source']} touched files in {leak['target']} "
            f"{leak['calls']} times across {leak['sessions']} sessions in the past 7 days."
        )
        if leak["top_files"]:
            top = leak["top_files"][0]
            body += f" Top file: {top['path']} ({top['n']} reads)."
        body += (
            " If this info is load-bearing, summarize it into the source"
            " workspace's CLAUDE.md or a memory entry so agents stop crossing."
        )
        out.append(_make_tip(
            key=key, category="cross-workspace", severity="info",
            title=f"{leak['source']} -> {leak['target']}: {leak['calls']} cross-workspace calls",
            body=body,
            scope=scope,
            links=[{"label": "Open Workspaces view", "href": "#/workspaces"}],
        ))
    return out


def all_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    return [
        *cache_discipline_tips(db_path, today_iso),
        *repeated_target_tips(db_path, today_iso),
        *right_size_tips(db_path, today_iso),
        *outlier_tips(db_path, today_iso),
        *skill_listing_budget_tips(db_path, today_iso),
        *claude_md_size_tips(db_path, today_iso),
        *cross_workspace_tips(db_path, today_iso),
    ]
