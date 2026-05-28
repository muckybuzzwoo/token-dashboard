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


def _is_plausible_plugin_name(name: str) -> bool:
    """A plugin name must not be a structural marker, version dir, temp-git slug, or carry a colon."""
    return bool(
        name
        and name not in _STRUCTURE_NAMES
        and not name.startswith("temp_git_")
        and not _VERSION_RE.match(name)
        and ":" not in name
    )


def _plugin_name_from_path(parts: tuple) -> Optional[str]:
    """Identify the plugin name for a SKILL.md path, or None if not under a plugin.

    Anchored on the two documented on-disk layouts (instead of probing every
    ancestor, which on Windows leaks home-path segments like 'Users' / username
    and on either OS leaks the marketplace name as if it were a plugin):

      A) marketplaces install
         .../plugins/marketplaces/<marketplace>/plugins/<plugin>/skills/<skill>/SKILL.md
         → plugin sits at skills_idx - 1, with `plugins` at skills_idx - 2.

      B) cache install, versioned
         .../plugins/cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
         → plugin sits at skills_idx - 2, with a version dir at skills_idx - 1
         and `cache` at skills_idx - 4.

      C) cache install, unversioned (defensive — observed for some installs)
         .../plugins/cache/<marketplace>/<plugin>/skills/<skill>/SKILL.md
         → plugin sits at skills_idx - 1, with `cache` at skills_idx - 3.

      D) cache install, temp git checkout (no plugin available)
         .../plugins/cache/temp_git_*/skills/<skill>/SKILL.md → None
    """
    try:
        skills_idx = len(parts) - 1 - parts[::-1].index("skills")
    except ValueError:
        return None

    # Layout A — marketplaces/<m>/plugins/<plugin>/skills/
    if skills_idx >= 2 and parts[skills_idx - 2] == "plugins":
        candidate = parts[skills_idx - 1]
        if _is_plausible_plugin_name(candidate):
            return candidate

    # Layout B — cache/<m>/<plugin>/<version>/skills/
    if (skills_idx >= 4
            and _VERSION_RE.match(parts[skills_idx - 1])
            and parts[skills_idx - 4] == "cache"):
        candidate = parts[skills_idx - 2]
        if _is_plausible_plugin_name(candidate):
            return candidate

    # Layout C — cache/<m>/<plugin>/skills/  (no version dir)
    if skills_idx >= 3 and parts[skills_idx - 3] == "cache":
        candidate = parts[skills_idx - 1]
        if _is_plausible_plugin_name(candidate):
            return candidate

    return None


def _slugs_for(skill_md: Path) -> list[str]:
    """Return the slug(s) a Skill tool invocation could use to load this file.

    Claude Code accepts only two slug forms:
      "<skill-name>"            — bare, always registered
      "<plugin>:<skill-name>"   — only when the file lives under a marketplace
                                  or cache plugin directory (see
                                  `_plugin_name_from_path` for the recognised
                                  layouts)

    Earlier revisions registered every non-structural ancestor segment as a
    possible plugin prefix. That over-generated bogus slugs on Windows (the
    home path's "Users" and the username became "plugin" prefixes) and even on
    POSIX (the marketplace directory name was indistinguishable from the
    plugin directory name). Strict layout matching avoids both.
    """
    if skill_md.name != "SKILL.md":
        return []
    skill_name = skill_md.parent.name
    # Some plugins ship the SKILL.md at the plugin root (e.g.
    # `cache/<m>/<plugin>/<version>/SKILL.md` — no `skills/` subdirectory).
    # The immediate parent is then the version dir, which is not a usable
    # slug. Walk one more up to the plugin directory name.
    if _VERSION_RE.match(skill_name):
        grandparent = skill_md.parent.parent.name
        if grandparent and not _VERSION_RE.match(grandparent):
            skill_name = grandparent
    slugs = {skill_name}
    plugin = _plugin_name_from_path(skill_md.parts)
    if plugin and plugin != skill_name:
        slugs.add(f"{plugin}:{skill_name}")
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
