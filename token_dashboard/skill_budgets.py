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


def skill_actuals(db_path, since=None, until=None) -> dict[str, dict]:
    """Return ``{slug: {p50, p95, count}}`` of output_tokens per invocation.

    Window = assistant-message output_tokens strictly after a Skill tool_call
    and strictly before the next Skill call in the same session (or session
    end if this is the last). LEFT JOIN so skills with zero subsequent output
    still contribute a 0 sample.
    """
    rng, args = _range_clause(since, until)
    # The CTE's `rng` filters which Skill calls enter the window set. The
    # outer JOIN's `rng` would filter the measured assistant messages — we
    # deliberately do NOT filter those: if a Skill call lands inside the
    # window, its full post-invocation output should be counted, even if
    # some of those messages fall outside the range.
    sql = f"""
      WITH calls AS (
        SELECT session_id,
               target     AS skill,
               timestamp  AS start_ts,
               LEAD(timestamp) OVER (
                 PARTITION BY session_id ORDER BY timestamp
               ) AS end_ts
          FROM tool_calls
         WHERE tool_name = 'Skill'
           AND target IS NOT NULL
           AND target != ''
           {rng}
      )
      SELECT c.skill,
             COALESCE(SUM(m.output_tokens), 0) AS output_tokens
        FROM calls c
        LEFT JOIN messages m
          ON m.session_id = c.session_id
         AND m.type       = 'assistant'
         AND m.timestamp  > c.start_ts
         AND (c.end_ts IS NULL OR m.timestamp < c.end_ts)
       GROUP BY c.skill, c.session_id, c.start_ts
    """
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
