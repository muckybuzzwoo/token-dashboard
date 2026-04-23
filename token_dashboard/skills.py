"""Skill catalog: locate SKILL.md files and map slugs to file sizes.

A skill on disk lives at one of:
  ~/.claude/skills/<name>/SKILL.md                     -> slug "<name>"
  ~/.claude/scheduled-tasks/<name>/SKILL.md            -> slug "<name>"
  ~/.claude/plugins/marketplaces/*/plugins/<plugin>/skills/<name>/SKILL.md
      -> registers TWO slugs: "<plugin>:<name>" and "<name>"
      (Claude Code accepts either form in the Skill tool.)

Sizes are in chars; token estimate is chars // 4 (the same approximation
`scanner._extract_results` uses for tool-result tokens).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Iterable, Optional

_DEFAULT_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "scheduled-tasks",
    Path.home() / ".claude" / "plugins",
]


import re

_VERSION_RE = re.compile(r"^\d+\.\d+")
_STRUCTURE_NAMES = {"skills", "plugins", "marketplaces", "cache", ".claude"}


def _slugs_for(skill_md: Path) -> list[str]:
    """Return the slug(s) a Skill tool invocation could use to load this file.

    Paths vary by install source:
      marketplaces/<m>/plugins/<plugin>/skills/<skill>/SKILL.md
      cache/<m>/<plugin>/<version>/skills/<skill>/SKILL.md
      cache/temp_git_*/skills/<skill>/SKILL.md         (no plugin)
      skills/<skill>/SKILL.md                          (no plugin)
      scheduled-tasks/<skill>/SKILL.md                 (no plugin)

    Strategy: always register the bare skill name. Additionally, walk up from
    `skills/` and register `<ancestor>:<skill>` for every ancestor segment
    that plausibly names a plugin (not a structural/version/temp-dir token).
    Unused slugs are harmless — the user only invokes real ones.
    """
    parts = skill_md.parts
    if "SKILL.md" not in parts or skill_md.name != "SKILL.md":
        return []
    skill_name = skill_md.parent.name
    slugs = {skill_name}
    # Locate the `skills` folder that contains this skill.
    try:
        skills_idx = len(parts) - 1 - parts[::-1].index("skills")
    except ValueError:
        return list(slugs)
    for seg in parts[:skills_idx]:
        if not seg or seg in _STRUCTURE_NAMES:
            continue
        if _VERSION_RE.match(seg):
            continue
        if seg.startswith("temp_git_"):
            continue
        if seg.endswith(":") or ":" in seg:  # drive letters like "C:"
            continue
        slugs.add(f"{seg}:{skill_name}")
    return sorted(slugs)


def scan_catalog(roots=None) -> Dict[str, dict]:
    """Return {slug: {path, chars, tokens}} for every SKILL.md found.

    When a slug resolves to multiple files (nested `skills/skills/`), keep the
    entry with the shallowest path — that's the canonical install.
    """
    roots = roots or _DEFAULT_ROOTS
    catalog: Dict[str, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for md in root.rglob("SKILL.md"):
            try:
                chars = md.stat().st_size
            except OSError:
                continue
            entry = {"path": str(md), "chars": chars, "tokens": chars // 4}
            for slug in _slugs_for(md):
                prev = catalog.get(slug)
                if prev is None or len(md.parts) < len(Path(prev["path"]).parts):
                    catalog[slug] = entry
    return catalog


def _project_skill_roots_from_cwds(cwds: Iterable[str]) -> list[Path]:
    """Return the innermost `.claude/skills/` directory for each cwd.

    Matches Claude Code's resolution rule: walk up from cwd and use the first
    `.claude/skills/` found, so a nested repo uses its own skills, not a parent's.
    """
    roots: set[Path] = set()
    for cwd in cwds:
        if not cwd:
            continue
        p = Path(cwd)
        for ancestor in (p, *p.parents):
            candidate = ancestor / ".claude" / "skills"
            if candidate.is_dir():
                roots.add(candidate)
                break
    return sorted(roots)


def _cwds_from_db(db_path) -> list[str]:
    from .db import connect
    with connect(db_path) as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT cwd FROM messages WHERE cwd IS NOT NULL"
        )]


_cache: dict = {"at": 0.0, "data": {}, "key": None}
_TTL_SECONDS = 60.0


def cached_catalog(db_path=None) -> Dict[str, dict]:
    """scan_catalog() with a simple in-process TTL cache.

    When `db_path` is provided, extra roots are derived from the distinct cwds
    in `messages` so project-local `.claude/skills/` directories are included.
    """
    now = time.time()
    key = str(db_path) if db_path else None
    if now - _cache["at"] > _TTL_SECONDS or _cache["key"] != key:
        extra = _project_skill_roots_from_cwds(_cwds_from_db(db_path)) if db_path else []
        _cache["data"] = scan_catalog(_DEFAULT_ROOTS + extra)
        _cache["at"] = now
        _cache["key"] = key
    return _cache["data"]


def tokens_for(slug: str, catalog: Optional[Dict[str, dict]] = None) -> Optional[int]:
    cat = catalog if catalog is not None else cached_catalog()
    info = cat.get(slug)
    return info["tokens"] if info else None
