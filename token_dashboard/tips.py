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

import json
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
                # Source-tool attribution via tool_use_id join. Without it the
                # tip says "12 results over 10k" with no hint which tool to fix.
                by_source = c.execute(
                    """SELECT inv.tool_name AS source, COUNT(*) AS n
                         FROM tool_calls tr
                         JOIN tool_calls inv
                           ON inv.tool_use_id = tr.tool_use_id
                          AND inv.tool_name != '_tool_result'
                        WHERE tr.tool_name = '_tool_result'
                          AND tr.result_tokens > 10000
                          AND tr.timestamp >= ?
                          AND tr.tool_use_id IS NOT NULL
                        GROUP BY inv.tool_name
                        ORDER BY n DESC LIMIT 3""",
                    (since,),
                ).fetchall()
                if by_source:
                    sources_str = ", ".join(f"{r['source']} ({r['n']})" for r in by_source)
                    source_sentence = f" Mostly from {sources_str}."
                else:
                    source_sentence = ""
                out.append(_make_tip(
                    key=key, category="tool-bloat", severity=severity,
                    title=f"{big['n']} tool results over 10k tokens this week",
                    body=(f"Average size {avg_t:,} tokens, biggest {int(big['max_t'] or 0):,}."
                          f"{source_sentence} "
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
_DISABLE_MODEL_RE = re.compile(
    r"^disable-model-invocation:\s*(true|yes|1)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_DESCRIPTION_HIDDEN_OVERRIDES = frozenset({"user-invocable-only", "name-only", "off"})
_USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def _read_skill_frontmatter(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else text[:2000]


def _read_skill_description(path: str) -> str:
    """Return the `description:` value from a SKILL.md frontmatter, or empty string."""
    block = _read_skill_frontmatter(path)
    d = _DESC_RE.search(block)
    return (d.group(1).strip() if d else "")


def _skill_disables_model_invocation(path: str) -> bool:
    return bool(_DISABLE_MODEL_RE.search(_read_skill_frontmatter(path)))


def _read_skill_overrides(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    overrides = data.get("skillOverrides")
    if not isinstance(overrides, dict):
        return {}
    return {str(k): str(v) for k, v in overrides.items()}


def _settings_paths_from_db(db_path) -> list:
    """Find project settings.local.json files from cwds recorded in the DB."""
    paths: set = set()
    with connect(db_path) as c:
        cwds = [r[0] for r in c.execute(
            "SELECT DISTINCT cwd FROM messages WHERE cwd IS NOT NULL"
        )]
    for cwd in cwds:
        if not cwd:
            continue
        p = Path(cwd)
        for ancestor in (p, *p.parents):
            settings = ancestor / ".claude" / "settings.local.json"
            if settings.is_file():
                paths.add(settings)
                break
    return sorted(paths)


def _skill_overrides(db_path) -> dict:
    """Return merged global + project skillOverrides. Project settings win."""
    merged = _read_skill_overrides(_USER_SETTINGS_PATH)
    for path in _settings_paths_from_db(db_path):
        merged.update(_read_skill_overrides(path))
    return merged


def _description_visible(slugs: set, overrides: dict) -> bool:
    if not overrides:
        return True
    for slug in slugs:
        if overrides.get(slug) in _DESCRIPTION_HIDDEN_OVERRIDES:
            return False
    return True


# Default context-window-1%-equivalent in characters.
# Claude Code default budget is 1% of context. With a 200k context window
# ~2000 tokens ≈ ~8000 chars. We use chars as a tokenizer-free proxy.
_SKILL_BUDGET_CHARS = 8000


def _most_active_cwd(db_path, since_iso: str) -> Optional[str]:
    """Return the cwd with the most messages since `since_iso`, or None."""
    with connect(db_path) as c:
        row = c.execute(
            """SELECT cwd, COUNT(*) AS n FROM messages
                WHERE cwd IS NOT NULL AND cwd != '' AND timestamp >= ?
                GROUP BY cwd ORDER BY n DESC LIMIT 1""",
            (since_iso,),
        ).fetchone()
    return row["cwd"] if row else None


def skill_listing_budget_tips(db_path, today_iso: Optional[str] = None,
                              budget_chars: int = _SKILL_BUDGET_CHARS) -> List[dict]:
    """Flag installed skills whose description footprint exceeds the listing budget.

    Scope-aware: the catalog now distinguishes user-global skills (loaded in
    every session) from project-scoped ones (loaded only when working under a
    specific project path). We compute the *effective* per-session footprint
    by intersecting the catalog with the user's most-active recent cwd — this
    matches what Claude Code actually loads when the user fires up a session
    in that project.

    Cross-references the on-disk skill catalog (descriptions) with
    invocation counts from `tool_calls` so the worst offenders (unused or
    rarely used) can be pointed at directly.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 30)

    from .skills import cached_catalog, is_active_in_cwd
    catalog = cached_catalog(db_path)
    if not catalog:
        return []

    top_cwd = _most_active_cwd(db_path, since)
    overrides = _skill_overrides(db_path)

    # Group slugs by the SKILL.md they resolve to: a single file commonly
    # registers two slugs (bare + "<plugin>:<bare>"). Each path is one skill
    # and must be counted once regardless of how many slug aliases it has.
    per_path: dict[str, dict] = {}
    for slug, info in catalog.items():
        entry = per_path.setdefault(info["path"], {
            "slugs": [],
            "scope": info["scope"],
            "project_path": info["project_path"],
        })
        entry["slugs"].append(slug)
    for p, entry in per_path.items():
        frontmatter = _read_skill_frontmatter(p)
        if (bool(_DISABLE_MODEL_RE.search(frontmatter))
                or not _description_visible(set(entry["slugs"]), overrides)):
            entry["desc_chars"] = 0  # hidden — do NOT add to ranked candidates either
        else:
            d = _DESC_RE.search(frontmatter)
            entry["desc_chars"] = len(d.group(1).strip() if d else "")
        entry["active_here"] = is_active_in_cwd(
            entry["scope"], entry["project_path"], top_cwd,
        )

    # Effective footprint = sum of descriptions for skills active in the
    # most-active cwd. That's what consumes Claude Code's per-session budget.
    active_paths = {p: e for p, e in per_path.items() if e["active_here"]}
    effective_chars = sum(e["desc_chars"] for e in active_paths.values())
    if effective_chars <= budget_chars:
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

    for p, entry in active_paths.items():
        entry["usage"] = sum(used.get(s, 0) for s in entry["slugs"])

    def _display_slug(slugs: list[str]) -> str:
        # Prefer the plugin-qualified form (Claude Code's canonical install
        # identifier); fall back to the bare name when none exists.
        plugin_form = sorted(s for s in slugs if ":" in s)
        return plugin_form[0] if plugin_form else sorted(slugs)[0]

    # Cheapest-to-drop = least-used, then largest description. Only consider
    # skills active in the current context — uninstalling a skill that isn't
    # even loaded here wouldn't help this session's budget.
    ranked_paths = sorted(
        [(p, e) for p, e in active_paths.items() if e["desc_chars"] > 0],
        key=lambda kv: (kv[1]["usage"], -kv[1]["desc_chars"]),
    )
    worst = [_display_slug(entry["slugs"]) for _, entry in ranked_paths[:5]]

    key = _key("skill-budget", "overall")
    if _is_dismissed(db_path, key):
        return []

    over_pct = (effective_chars / budget_chars - 1) * 100

    # Compose scope summary. When the manifest is missing (all scope='unknown'),
    # skip the breakdown — we can't differentiate honestly.
    n_user = sum(1 for e in active_paths.values() if e["scope"] == "user-global")
    n_proj = sum(1 for e in active_paths.values() if e["scope"] in ("project-global", "project-local"))
    n_unknown = sum(1 for e in active_paths.values() if e["scope"] == "unknown")
    cwd_label = Path(top_cwd).name if top_cwd else None

    scope_phrase = ""
    if n_unknown == 0 and cwd_label and (n_proj or n_user):
        if n_proj and n_user:
            scope_phrase = (
                f" In your most-active context (`{cwd_label}`), {n_user} global "
                f"skill(s) plus {n_proj} project-scoped skill(s) load per session."
            )
        elif n_user:
            scope_phrase = f" All {n_user} active skills are user-global (load in every session)."
        elif n_proj:
            scope_phrase = (
                f" {n_proj} project-scoped skill(s) load when working in `{cwd_label}`."
            )

    if worst:
        body = (
            f"Skill descriptions loaded per session total ~{effective_chars:,} chars "
            f"vs the default ~{budget_chars:,}-char budget (1% of context)."
            f"{scope_phrase} Claude Code drops descriptions of the least-used "
            "skills first, so those skills stop auto-triggering. "
            f"Least-recently-used candidates: {', '.join(worst)}."
        )
    else:
        body = (
            f"Skill descriptions loaded per session total ~{effective_chars:,} chars "
            f"vs ~{budget_chars:,}.{scope_phrase}"
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


# ── New tip: session approached the context window limit ────────────────────

# Sessions that accumulate a lot of NEW content per turn (input + cache_create,
# excluding cache_read which is multi-counted across breakpoints) are heavy
# regardless of the model's window size. 100k of net-new content in a single
# turn is a strong signal of a context-heavy session.
_CONTEXT_PRESSURE_PEAK_NEW = 100_000


def context_pressure_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag sessions that accreted heavy new content per turn.

    We measure ``input_tokens + cache_create_5m_tokens + cache_create_1h_tokens``
    — the net-new tokens added to the session's prompt for that turn. We
    deliberately do NOT include ``cache_read_tokens`` because Anthropic
    multi-counts cache reads across breakpoints (a single turn can report
    cache_read > model context window), making it a poor "context usage" proxy.

    Net-new per turn is conservative but trustworthy: at >100k tokens of new
    content in a single turn, the prompt is heavy in absolute terms.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT session_id,
             MAX(input_tokens + cache_create_5m_tokens
                 + cache_create_1h_tokens) AS peak_new
        FROM messages
       WHERE type='assistant' AND timestamp >= ?
       GROUP BY session_id
       HAVING peak_new >= ?
       ORDER BY peak_new DESC
       LIMIT 3
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since, _CONTEXT_PRESSURE_PEAK_NEW)):
            sid = row["session_id"]
            key = _key("context-pressure", sid)
            if _is_dismissed(db_path, key):
                continue
            peak = int(row["peak_new"] or 0)
            out.append(_make_tip(
                key=key, category="context-pressure", severity="info",
                title=f"Session {sid[:8]}… added {peak:,} new tokens in a single turn",
                body=(f"Peak net-new tokens in one assistant turn: {peak:,} "
                      "(uncached input + freshly cached content). High values "
                      "indicate a session that's accreting context fast — "
                      "consider `/clear` between unrelated tasks or splitting "
                      "long work across sessions."),
                scope=sid,
                links=[
                    _session_link(sid, "Open session"),
                    _doc_link("Anthropic: manage context",
                              "https://code.claude.com/docs/en/costs"),
                ],
            ))
    return out


# ── New tip: repeated identical Bash errors ──────────────────────────────────

def repeated_bash_errors_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag Bash commands that errored ≥3 times with identical command text.

    Repeated identical failures are usually a sign the agent retried instead
    of investigating the root cause — Anthropic's "root cause discipline" line
    from the system prompt. Uses the tool_use_id join introduced for the
    bash-bloat tip.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 3)  # short window: this is an active-debug signal
    sql = """
      SELECT bash.target          AS cmd,
             COUNT(*)             AS n,
             MAX(bash.session_id) AS sample_session
        FROM tool_calls bash
        JOIN tool_calls tr
          ON tr.tool_use_id = bash.tool_use_id
         AND tr.session_id  = bash.session_id
         AND tr.tool_name   = '_tool_result'
       WHERE bash.tool_name = 'Bash'
         AND bash.tool_use_id IS NOT NULL
         AND bash.target IS NOT NULL
         AND tr.is_error = 1
         AND bash.timestamp >= ?
       GROUP BY bash.target
       HAVING n >= 3
       ORDER BY n DESC
       LIMIT 5
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since,)):
            cmd = row["cmd"]
            key = _key("bash-errors", (cmd or "")[:120])
            if _is_dismissed(db_path, key):
                continue
            display = (cmd[:80] + "…") if len(cmd) > 80 else cmd
            out.append(_make_tip(
                key=key, category="bash-errors", severity="info",
                title=f"`{display}` failed {row['n']} times",
                body=("This Bash command produced an error result more than "
                      f"{row['n']} times in the past 3 days. Repeated identical "
                      "failures usually mean the underlying cause isn't being "
                      "investigated. Check the error message in the latest "
                      "session and address the root cause rather than retrying."),
                scope=cmd[:200],
                links=[_session_link(row["sample_session"], "Latest occurrence")],
            ))
    return out


# ── New tip: high web-fetch volume ───────────────────────────────────────────

# Generic detection: any tool whose name suggests fetching web content.
# Covers Anthropic's WebFetch and the most common MCP web fetchers (Jina,
# Firecrawl, browser-* MCPs). The match is by-name so no specific tool brand
# is hard-coded.
_WEB_FETCH_TOOL_RE = re.compile(
    r"""(?:
        ^WebFetch$
      | ^mcp__[^_]+__(?:.*(?:fetch|read_url|scrape|crawl|browser_navigate|browser_take_screenshot).*)
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _is_web_fetch_tool(name: Optional[str]) -> bool:
    return bool(name and _WEB_FETCH_TOOL_RE.match(name))


def web_fetch_volume_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag sessions with very high web-fetch volume.

    Each fetch round-trips a (potentially huge) page through tool_result, which
    is expensive in context. >15 fetches in one session suggests either:
    cacheable references that could go into CLAUDE.md or a memory entry, OR
    a tool with no result-trimming.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        # Pull all per-session tool_name counts, then filter in Python so the
        # regex stays in one place.
        rows = c.execute(
            """SELECT session_id, tool_name, COUNT(*) AS n
                 FROM tool_calls
                WHERE (tool_name = 'WebFetch' OR tool_name LIKE 'mcp__%')
                  AND timestamp >= ?
                GROUP BY session_id, tool_name""",
            (since,),
        ).fetchall()

    by_session: dict[str, int] = {}
    for row in rows:
        if _is_web_fetch_tool(row["tool_name"]):
            by_session[row["session_id"]] = by_session.get(row["session_id"], 0) + row["n"]

    for sid, n in sorted(by_session.items(), key=lambda kv: -kv[1])[:5]:
        if n < 15:
            continue
        key = _key("web-fetch-volume", sid)
        if _is_dismissed(db_path, key):
            continue
        out.append(_make_tip(
            key=key, category="web-fetch-volume", severity="info",
            title=f"Session {sid[:8]}… made {n} web-fetch calls",
            body=(f"Heavy web-fetch use in one session ({n} calls in 7 days). "
                  "Each call inflates context with the fetched page. If the "
                  "same URLs come back repeatedly, summarize them once into "
                  "CLAUDE.md or a memory entry instead. If a single page is "
                  "huge, ask for a narrower selector."),
            scope=sid,
            links=[
                _session_link(sid, "Open session"),
                _doc_link("Anthropic: manage costs",
                          "https://code.claude.com/docs/en/costs"),
            ],
        ))
    return out


# ── New tip: Opus-only workspaces ────────────────────────────────────────────

def opus_only_workspace_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag projects where >90% of assistant turns ran on Opus.

    Opus is the most expensive tier (~5x Sonnet); routine work that doesn't
    need top-of-line reasoning (file reads, refactors, scaffolding) is
    cheaper on Sonnet or Haiku.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 14)
    sql = """
      SELECT project_slug,
             COUNT(*) AS total,
             SUM(CASE WHEN lower(model) LIKE '%opus%' THEN 1 ELSE 0 END) AS opus_n
        FROM messages
       WHERE type='assistant' AND model IS NOT NULL AND timestamp >= ?
       GROUP BY project_slug
       HAVING total >= 50 AND opus_n * 1.0 / total > 0.9
       ORDER BY total DESC
       LIMIT 3
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since,)):
            project = row["project_slug"]
            key = _key("opus-only", project)
            if _is_dismissed(db_path, key):
                continue
            pct = (row["opus_n"] or 0) * 100 // (row["total"] or 1)
            out.append(_make_tip(
                key=key, category="opus-only", severity="cost",
                title=f"{project}: {pct}% of turns on Opus",
                body=(f"{row['opus_n']:,} of {row['total']:,} assistant turns in "
                      f"the past 14 days ran on Opus. For routine work (file "
                      "reads, refactors, scaffolding), Sonnet is ~5x cheaper at "
                      "comparable quality. Reserve Opus for hard reasoning."),
                scope=project,
                links=[
                    _doc_link("Anthropic: choose the right model",
                              "https://code.claude.com/docs/en/costs"),
                ],
            ))
    return out


# ── New tip: MCP server sprawl ───────────────────────────────────────────────

_MCP_SPRAWL_THRESHOLD = 12


def mcp_sprawl_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag when many distinct MCP servers are active in 7 days.

    Each connected MCP server costs context on every turn (its tool schemas
    are listed for Claude). Anthropic recommends disabling MCPs you don't
    actively use.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    servers: set[str] = set()
    with connect(db_path) as c:
        for row in c.execute(
            """SELECT DISTINCT tool_name FROM tool_calls
                WHERE tool_name LIKE 'mcp__%' AND timestamp >= ?""",
            (since,),
        ):
            name = row["tool_name"] or ""
            # tool_name shape: mcp__<server>__<tool>
            after = name[5:]
            sep = after.find("__")
            server = after[:sep] if sep > 0 else after
            if server:
                servers.add(server)

    if len(servers) < _MCP_SPRAWL_THRESHOLD:
        return out

    key = _key("mcp-sprawl", "overall")
    if _is_dismissed(db_path, key):
        return out

    sample = ", ".join(sorted(servers)[:10])
    if len(servers) > 10:
        sample += f", and {len(servers) - 10} more"
    out.append(_make_tip(
        key=key, category="mcp-sprawl", severity="info",
        title=f"{len(servers)} MCP servers active in the past 7 days",
        body=(f"Each connected MCP server adds its tool schemas to every turn. "
              "Disable servers you don't actively use (settings.json or your "
              f"client's MCP config). Active servers: {sample}."),
        scope="overall",
        links=[
            _doc_link("Anthropic: MCP overhead",
                      "https://code.claude.com/docs/en/mcp"),
        ],
    ))
    return out


# ── New tip: CLAUDE.md stack (multiple files in one ancestry) ────────────────

_CLAUDE_MD_STACK_MIN_COUNT = 3
_CLAUDE_MD_STACK_MAX_TOTAL_LINES = 400


def claude_md_stack_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag when a cwd has 3+ CLAUDE.md files in its ancestry totalling >400 lines.

    Sister tip to `claude_md_size_tips`: that one flags an individual file as
    too big; this one flags the *stack* (global + project + nested). Each
    layer adds context to every turn even when individually within limits.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 30)

    out = []
    seen_combos: set[tuple] = set()
    for cwd in _distinct_active_cwds(db_path, since):
        if not cwd:
            continue
        try:
            cwd_path = Path(cwd)
        except (TypeError, ValueError):
            continue
        stack: list[tuple[str, int]] = []
        for ancestor in (cwd_path, *list(cwd_path.parents)[:5]):
            candidate = ancestor / "CLAUDE.md"
            try:
                if not candidate.is_file():
                    continue
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.count("\n") + (0 if text.endswith("\n") else 1)
            stack.append((str(candidate), lines))
        if len(stack) < _CLAUDE_MD_STACK_MIN_COUNT:
            continue
        total_lines = sum(n for _, n in stack)
        if total_lines < _CLAUDE_MD_STACK_MAX_TOTAL_LINES:
            continue
        combo = tuple(p for p, _ in stack)
        if combo in seen_combos:
            continue
        seen_combos.add(combo)
        scope = "|".join(combo)
        key = _key("claude-md-stack", scope)
        if _is_dismissed(db_path, key):
            continue
        files_desc = ", ".join(
            f"{Path(p).parent.name or Path(p).anchor}/CLAUDE.md ({n}l)"
            for p, n in stack
        )
        out.append(_make_tip(
            key=key, category="claude-md-stack", severity="info",
            title=f"{len(stack)} CLAUDE.md files stack to {total_lines} lines",
            body=(f"Working in `{cwd_path.name}`, the agent reads "
                  f"{len(stack)} CLAUDE.md files every turn — combined "
                  f"{total_lines} lines. Stack: {files_desc}. Consider "
                  "consolidating overlapping guidance into a single layer."),
            scope=scope,
            links=[
                _doc_link("Anthropic: manage costs (CLAUDE.md size)",
                          "https://code.claude.com/docs/en/costs"),
            ],
        ))
    return out


# ── New tip: skills with overlong descriptions ───────────────────────────────

_SKILL_DESC_MAX_CHARS = 400
_SKILL_DESC_MIN_OFFENDERS = 3


def long_skill_descriptions_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag installed skills whose `description:` exceeds Anthropic's recommended length.

    Companion to `skill_listing_budget_tips`: that one flags total footprint;
    this one names the biggest individual offenders, useful when the user owns
    the skill and can shorten it.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    from .skills import cached_catalog
    catalog = cached_catalog(db_path)
    if not catalog:
        return []

    per_path: dict[str, list[str]] = {}
    for slug, info in catalog.items():
        per_path.setdefault(info["path"], []).append(slug)

    long_ones: list[tuple[int, str]] = []
    for path, slugs in per_path.items():
        desc_len = len(_read_skill_description(path))
        if desc_len > _SKILL_DESC_MAX_CHARS:
            plugin_form = sorted(s for s in slugs if ":" in s)
            display = plugin_form[0] if plugin_form else sorted(slugs)[0]
            long_ones.append((desc_len, display))

    if len(long_ones) < _SKILL_DESC_MIN_OFFENDERS:
        return []

    key = _key("long-skill-descriptions", "overall")
    if _is_dismissed(db_path, key):
        return []

    long_ones.sort(reverse=True)
    sample_lines = [
        f"{display} ({chars} chars)" for chars, display in long_ones[:5]
    ]
    extra = "" if len(long_ones) <= 5 else f" and {len(long_ones) - 5} more"
    return [_make_tip(
        key=key, category="long-skill-descriptions", severity="info",
        title=f"{len(long_ones)} skill descriptions exceed {_SKILL_DESC_MAX_CHARS} chars",
        body=(f"Anthropic recommends concise skill descriptions (≤{_SKILL_DESC_MAX_CHARS} "
              "chars) so the listing fits inside the budget. Longest: "
              + "; ".join(sample_lines) + extra + "."),
        scope="overall",
        links=[
            {"label": "Open Skills tab", "href": "#/skills"},
            _doc_link("Anthropic: skills authoring",
                      "https://code.claude.com/docs/en/skills"),
        ],
    )]


# ── New tip: Bash commands producing bloat without an output limiter ─────────

# Output-limiter patterns recognised across shells. The list aims to be a
# conservative *true-positive* indicator — i.e. when these patterns are
# present, we trust the user is already controlling output. False negatives
# (real limiters we don't recognise) just produce a noisier tip; false
# positives (matching strings inside a different context) only suppress a tip.
_LIMITER_RE = re.compile(
    r"""(?:
        \|\s*head\b
      | \|\s*tail\b
      | \|\s*less\b
      | \|\s*more\b
      | \|\s*wc\s+-l\b
      | \bgrep\s+-c\b
      | \buniq\s+-c\b
      | \bsort\s+-u\b
      | \bhead\s+-n?\s*\d
      | \btail\s+-n?\s*\d
      | \b--max-results\b
      | \b--max-count\s*=?\s*\d
      | \b-m\s*\d
      | Select-Object\s+-First\b
      | Select-Object\s+-Last\b
      | -TotalCount\b
      | -First\s+\d
      | -Tail\s+\d
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _has_output_limiter(cmd: str) -> bool:
    return bool(_LIMITER_RE.search(cmd or ""))


def bash_bloat_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag Bash commands that repeatedly produce >5k-token outputs without an
    output limiter.

    Joins each `Bash` tool_use row to its corresponding `_tool_result` row via
    `tool_use_id` (added in the same release as this tip). Groups by the
    command string, and emits the top offenders that have no `head`,
    `Select-Object -First`, etc. in the command text.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT bash.target            AS cmd,
             COUNT(*)               AS n,
             AVG(tr.result_tokens)  AS avg_t,
             MAX(tr.result_tokens)  AS max_t,
             MAX(bash.session_id)   AS sample_session
        FROM tool_calls bash
        JOIN tool_calls tr
          ON tr.tool_use_id = bash.tool_use_id
         AND tr.session_id  = bash.session_id
         AND tr.tool_name   = '_tool_result'
       WHERE bash.tool_name = 'Bash'
         AND bash.tool_use_id IS NOT NULL
         AND bash.target IS NOT NULL
         AND bash.timestamp >= ?
         AND tr.result_tokens > 5000
       GROUP BY bash.target
       HAVING n >= 2 AND avg_t > 5000
       ORDER BY avg_t * n DESC
       LIMIT 10
    """
    out: List[dict] = []
    with connect(db_path) as c:
        rows = list(c.execute(sql, (since,)))

    # Filter to commands without an apparent output limiter — those are the
    # actionable ones (user can fix the next invocation by piping to head etc.).
    for row in rows:
        cmd = row["cmd"]
        if _has_output_limiter(cmd):
            continue
        if len(out) >= 5:
            break
        key = _key("bash-bloat", (cmd or "")[:120])
        if _is_dismissed(db_path, key):
            continue
        avg_t = int(row["avg_t"] or 0)
        max_t = int(row["max_t"] or 0)
        display = (cmd[:80] + "…") if len(cmd) > 80 else cmd
        out.append(_make_tip(
            key=key, category="bash-bloat", severity="info",
            title=f"`{display}` averages {avg_t:,} tokens of output",
            body=(f"Ran {row['n']} times in the past 7 days, avg result "
                  f"{avg_t:,} tokens (max {max_t:,}). Piping the output through "
                  "`head`, `tail`, or `Select-Object -First` (PowerShell) would "
                  "shrink the result Claude has to read back."),
            scope=cmd[:200],
            links=[
                _session_link(row["sample_session"], "Session with this command"),
                _doc_link("Anthropic: reduce MCP tool overhead",
                          "https://code.claude.com/docs/en/mcp"),
            ],
        ))
    return out


# ── New tip: dead skills (zero invocations in 90d) ───────────────────────────

_DEAD_SKILLS_WINDOW_DAYS = 90
_DEAD_SKILLS_MIN_AGE_DAYS = 30
_DEAD_SKILLS_MIN_COUNT = 5


def dead_skills_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag installed skills with zero `Skill`-tool invocations in 90 days.

    Companion to `skill_listing_budget_tips` — that tip ranks least-used skills
    when the budget is exceeded; this one calls out skills that are
    *categorically* dead so they can be uninstalled, regardless of budget.

    Skills installed in the last 30 days are excluded (file mtime as install-age
    proxy — same semantics on macOS, Linux, and Windows).

    Project-scoped skills are excluded when their project hasn't been visited
    in the 90-day window — "no invocations" is meaningless for a project the
    user simply hasn't touched lately, and uninstalling it would break that
    project the moment it's reopened.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, _DEAD_SKILLS_WINDOW_DAYS)
    now = time.time()
    min_age = _DEAD_SKILLS_MIN_AGE_DAYS * 86400

    from .skills import cached_catalog, is_active_in_cwd
    catalog = cached_catalog(db_path)
    if not catalog:
        return []

    with connect(db_path) as c:
        used_slugs = {r["target"] for r in c.execute(
            """SELECT DISTINCT target FROM tool_calls
                WHERE tool_name='Skill' AND target IS NOT NULL AND target != ''
                  AND timestamp >= ?""",
            (since,),
        )}
        recent_cwds = [r["cwd"] for r in c.execute(
            """SELECT DISTINCT cwd FROM messages
                WHERE cwd IS NOT NULL AND cwd != '' AND timestamp >= ?""",
            (since,),
        )]

    def _active_somewhere_recently(scope: Optional[str], project_path: Optional[str]) -> bool:
        """True if the skill was loadable in at least one session in the window."""
        if scope in ("user-global", "unknown"):
            return True
        if not recent_cwds:
            # No session history at all → don't second-guess; treat as visited.
            return True
        return any(is_active_in_cwd(scope, project_path, cwd) for cwd in recent_cwds)

    # Group slugs by SKILL.md path so each file is judged once. A skill is dead
    # iff *none* of its registered slugs (bare and plugin-qualified) saw an
    # invocation.
    per_path: dict[str, dict] = {}
    for slug, info in catalog.items():
        entry = per_path.setdefault(info["path"], {
            "slugs": [],
            "scope": info["scope"],
            "project_path": info["project_path"],
        })
        entry["slugs"].append(slug)

    dead: list[str] = []
    for path, entry in per_path.items():
        slugs = entry["slugs"]
        if any(s in used_slugs for s in slugs):
            continue
        if not _active_somewhere_recently(entry["scope"], entry["project_path"]):
            continue  # project-scoped + project not visited → can't conclude dead
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            continue
        if now - mtime < min_age:
            continue  # recently installed — give it time
        # Display label: prefer plugin-qualified form.
        plugin_form = sorted(s for s in slugs if ":" in s)
        dead.append(plugin_form[0] if plugin_form else sorted(slugs)[0])

    if len(dead) < _DEAD_SKILLS_MIN_COUNT:
        return []

    key = _key("dead-skills", "overall")
    if _is_dismissed(db_path, key):
        return []

    dead.sort()
    sample = ", ".join(dead[:8])
    if len(dead) > 8:
        sample += f", and {len(dead) - 8} more"
    return [_make_tip(
        key=key, category="dead-skills", severity="info",
        title=f"{len(dead)} skills haven't been used in {_DEAD_SKILLS_WINDOW_DAYS} days",
        body=(f"These skills are installed but have zero invocations in the past "
              f"{_DEAD_SKILLS_WINDOW_DAYS} days. Uninstalling them frees up "
              "skill-listing budget and reduces context noise. "
              f"Candidates: {sample}."),
        scope="overall",
        links=[
            {"label": "Open Skills tab", "href": "#/skills"},
            _doc_link("Anthropic: extend Claude with skills",
                      "https://code.claude.com/docs/en/skills"),
        ],
    )]


# ── New tip: subagent sprawl ─────────────────────────────────────────────────

_SUBAGENT_SPRAWL_RATIO = 2.0
_SUBAGENT_SPRAWL_MIN_SIDE_TOKENS = 50_000


def subagent_sprawl_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    """Flag sessions where sidechain (subagent) tokens dominate main-chain tokens.

    Subagents have their own context — that's the point. But when they return
    very large payloads, the main chain absorbs the cost. A 2× sidechain:main
    ratio with >50k sidechain tokens in 7 days is a strong "subagents are doing
    too much per dispatch" signal.

    Excludes `acompact-*` agent ids (auto-compaction, not user-dispatched).
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT session_id,
             SUM(CASE WHEN is_sidechain=0 THEN input_tokens+output_tokens ELSE 0 END) AS main_t,
             SUM(CASE WHEN is_sidechain=1
                          AND (agent_id IS NULL OR agent_id NOT LIKE 'acompact-%')
                      THEN input_tokens+output_tokens ELSE 0 END) AS side_t
        FROM messages
       WHERE type='assistant' AND timestamp >= ?
       GROUP BY session_id
       HAVING side_t >= ? AND side_t > ? * (main_t + 1)
       ORDER BY side_t DESC
       LIMIT 5
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since, _SUBAGENT_SPRAWL_MIN_SIDE_TOKENS,
                                   _SUBAGENT_SPRAWL_RATIO)):
            sid = row["session_id"]
            key = _key("subagent-sprawl", sid)
            if _is_dismissed(db_path, key):
                continue
            ratio = (row["side_t"] or 0) / max(row["main_t"] or 1, 1)
            out.append(_make_tip(
                key=key, category="subagent-sprawl", severity="info",
                title=f"Subagent tokens dominate session {sid[:8]}…",
                body=(f"Sidechain (subagent) tokens: {int(row['side_t']):,}. "
                      f"Main-chain tokens: {int(row['main_t']):,}. "
                      f"That's {ratio:.1f}× the main chain. Subagents may be "
                      "returning more than the orchestrator needs — consider "
                      "asking for narrower summaries or splitting dispatches."),
                scope=sid,
                links=[
                    _session_link(sid, "Open session"),
                    {"label": "Subagents view", "href": "#/subagents"},
                    _doc_link("Anthropic: manage costs",
                              "https://code.claude.com/docs/en/costs"),
                ],
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
        *dead_skills_tips(db_path, today_iso),
        *subagent_sprawl_tips(db_path, today_iso),
        *bash_bloat_tips(db_path, today_iso),
        *context_pressure_tips(db_path, today_iso),
        *repeated_bash_errors_tips(db_path, today_iso),
        *web_fetch_volume_tips(db_path, today_iso),
        *opus_only_workspace_tips(db_path, today_iso),
        *mcp_sprawl_tips(db_path, today_iso),
        *claude_md_stack_tips(db_path, today_iso),
        *long_skill_descriptions_tips(db_path, today_iso),
    ]
