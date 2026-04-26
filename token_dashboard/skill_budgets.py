"""Skill budget-vs-actual tracking.

Parses user-declared output-token budgets from SKILL.md body text and
measures each skill's actual output-token footprint per invocation.

Two declaration formats are supported (no frontmatter field exists across
the catalog today):
  1. Inline:   ``Execute these steps in order. Complete in <N,NNN output tokens.``
  2. Section:  ``## Token Budget\n< N output tokens.``

Attribution window for "actual": sum of ``output_tokens`` across assistant
messages after this Skill tool_call and before the next Skill call in the
same session (or end of session). Starting another skill naturally
terminates the previous skill's accounting.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .db import connect


_INLINE = re.compile(r"Complete in\s*<\s*([\d,]+)\s+output\s+tokens", re.I)
_SECTION = re.compile(
    r"^##\s*Token\s+Budget\s*$[\r\n]+\s*<\s*([\d,]+)\s+output\s+tokens",
    re.I | re.M,
)


def parse_budget_from_text(text: str) -> Optional[int]:
    """Return declared output-token budget, or None if nothing parsed.

    Inline form wins if both patterns appear in the same file (in the
    sampled corpus they are mutually exclusive, but the inline line sits
    at the top and is the more prescriptive form).
    """
    for rx in (_INLINE, _SECTION):
        m = rx.search(text)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


_budget_cache: dict[tuple[str, float], Optional[int]] = {}


def budget_for(slug: str, catalog=None) -> Optional[int]:
    """Look up a skill's declared budget via the catalog, cache by (path, mtime).

    Missing slug, unreadable file, or unparsed body → None. No exceptions.
    """
    from .skills import cached_catalog

    cat = catalog if catalog is not None else cached_catalog()
    info = cat.get(slug)
    if not info:
        return None
    path = Path(info["path"])
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = (info["path"], mtime)
    if key in _budget_cache:
        return _budget_cache[key]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _budget_cache[key] = None
        return None
    val = parse_budget_from_text(text)
    _budget_cache[key] = val
    return val


def _range_clause(since, until):
    where, args = [], []
    if since:
        where.append("timestamp >= ?")
        args.append(since)
    if until:
        where.append("timestamp < ?")
        args.append(until)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _percentile(sorted_xs: list[int], p: int) -> int:
    if not sorted_xs:
        return 0
    k = (len(sorted_xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_xs) - 1)
    if lo == hi:
        return sorted_xs[lo]
    return int(sorted_xs[lo] * (hi - k) + sorted_xs[hi] * (k - lo))


# User-role messages that are system-injected (not real typing) and must not
# terminate the attribution window. Skill/agent invocations inject the body
# of SKILL.md/AGENT.md as a 20k+ user-role message; other Claude Code
# machinery injects the bracketed tags below. Empirically this covers ~25%
# of non-empty user messages; the other ~75% are the user actually typing.
_META_USER_PREFIXES = (
    "Base directory for this skill:",
    "Base directory for this agent:",
    "<system-reminder>",
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "[Request interrupted",
)


def skill_actuals(db_path, since=None, until=None) -> dict[str, dict]:
    """Return ``{slug: {p50, p95, count}}`` of output_tokens per invocation.

    Window boundaries, in priority order:
      1. next Skill call in the same session,
      2. next real-user-typed main-chain message (``prompt_chars > 0`` and
         ``prompt_text`` does NOT start with any system-injection prefix),
      3. end of session.

    Sidechain assistant output (subagents, auto-compaction) is excluded —
    it is not emitted by the skill itself and would otherwise leak in when
    an auto-compact agent fires during the window.

    Note on what ``output_tokens`` counts: the Anthropic API ``output_tokens``
    field includes tool_use JSON blocks and thinking blocks, not just
    user-visible text. Skills that declare "Complete in <N output tokens"
    usually mean text-only output, so a 2-5× gap between declared budget
    and measured p50 can reflect tool_use overhead rather than a bloated
    skill.
    """
    rng, args = _range_clause(since, until)
    # Build "m.prompt_text NOT LIKE ?" chain for the meta-prefix filter.
    not_like = " AND ".join(
        ["u.prompt_text NOT LIKE ?"] * len(_META_USER_PREFIXES)
    )
    like_args = [p + "%" for p in _META_USER_PREFIXES]
    sql = f"""
      WITH calls AS (
        SELECT session_id,
               target     AS skill,
               timestamp  AS start_ts,
               LEAD(timestamp) OVER (
                 PARTITION BY session_id ORDER BY timestamp
               ) AS next_skill_ts
          FROM tool_calls
         WHERE tool_name = 'Skill'
           AND target IS NOT NULL
           AND target != ''
           {rng}
      ),
      bounds AS (
        SELECT c.session_id, c.skill, c.start_ts, c.next_skill_ts,
               (SELECT MIN(u.timestamp) FROM messages u
                 WHERE u.session_id   = c.session_id
                   AND u.type         = 'user'
                   AND u.is_sidechain = 0
                   AND u.prompt_chars IS NOT NULL
                   AND u.prompt_chars > 0
                   AND u.prompt_text  IS NOT NULL
                   AND u.timestamp    > c.start_ts
                   AND {not_like}
               ) AS next_user_ts
          FROM calls c
      )
      SELECT b.skill,
             COALESCE(SUM(m.output_tokens), 0) AS output_tokens
        FROM bounds b
        LEFT JOIN messages m
          ON m.session_id   = b.session_id
         AND m.type         = 'assistant'
         AND m.is_sidechain = 0
         AND m.timestamp    > b.start_ts
         AND (b.next_skill_ts IS NULL OR m.timestamp < b.next_skill_ts)
         AND (b.next_user_ts  IS NULL OR m.timestamp < b.next_user_ts)
       GROUP BY b.skill, b.session_id, b.start_ts
    """
    args = [*args, *like_args]
    samples: dict[str, list[int]] = {}
    with connect(db_path) as conn:
        for row in conn.execute(sql, args):
            samples.setdefault(row["skill"], []).append(row["output_tokens"] or 0)
    out: dict[str, dict] = {}
    for slug, xs in samples.items():
        xs.sort()
        out[slug] = {
            "p50":   _percentile(xs, 50),
            "p95":   _percentile(xs, 95),
            "count": len(xs),
        }
    return out
