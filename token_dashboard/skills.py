"""Skill catalog: locate active SKILL.md files and map slugs to file sizes.

A skill on disk lives at one of:
  ~/.claude/skills/<name>/SKILL.md                     -> slug "<name>"
  ~/.claude/scheduled-tasks/<name>/SKILL.md            -> slug "<name>"
  ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md
      -> registers TWO slugs: "<plugin>:<name>" and "<name>"
      (Claude Code accepts either form in the Skill tool.)

Sizes are in chars; token estimate is chars // 4 (the same approximation
`scanner._extract_results` uses for tool-result tokens).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

_PERSONAL_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "scheduled-tasks",
]

_PLUGIN_CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache"
_INSTALLED_PLUGINS_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
_USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

_VERSION_RE = re.compile(r"^\d+\.\d+")
_STRUCTURE_NAMES = {"skills", "plugins", "marketplaces", "cache", ".claude"}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _enabled_plugin_ids(settings: dict) -> Optional[set[str]]:
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return None
    return {str(k) for k, v in enabled.items() if v}


def _installed_plugin_skill_roots() -> list[Path]:
    """Return skills directories for installed, enabled plugins.

    Claude Code keeps downloaded marketplace source under
    ``~/.claude/plugins/marketplaces``. Those files are not active installs
    and should not affect the skill-listing budget. Active plugin installs
    are recorded in ``installed_plugins.json`` and point at cache paths.
    """
    data = _read_json(_INSTALLED_PLUGINS_PATH)
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return []

    enabled_ids = _enabled_plugin_ids(_read_json(_USER_SETTINGS_PATH))
    roots: set[Path] = set()
    for plugin_id, installs in plugins.items():
        if enabled_ids is not None and plugin_id not in enabled_ids:
            continue
        if not isinstance(installs, list):
            continue
        for install in installs:
            if not isinstance(install, dict):
                continue
            install_path = install.get("installPath")
            if not install_path:
                continue
            skills_dir = Path(install_path) / "skills"
            if skills_dir.is_dir():
                roots.add(skills_dir)
    return sorted(roots)


def default_roots() -> list[Path]:
    """Return active global roots.

    Prefer the installed-plugin registry when available. If the registry is
    missing or unreadable, fall back to the plugin cache root rather than the
    whole plugin tree so marketplace checkouts are never counted as active.
    """
    plugin_roots = _installed_plugin_skill_roots()
    return [*_PERSONAL_ROOTS, *(plugin_roots or [_PLUGIN_CACHE_ROOT])]


def _slugs_for(skill_md: Path) -> list[str]:
    """Return the slug(s) a Skill tool invocation could use to load this file.

    Paths vary by install source:
      marketplaces/<m>/plugins/<plugin>/skills/<skill>/SKILL.md
      cache/<m>/<plugin>/<version>/skills/<skill>/SKILL.md
      cache/temp_git_*/skills/<skill>/SKILL.md         (no plugin)
      skills/<skill>/SKILL.md                          (no plugin)
      scheduled-tasks/<skill>/SKILL.md                 (no plugin)

    Strategy: always register the bare skill name. Additionally, recognize
    the two plugin layouts Claude Code uses and register only
    ``<plugin>:<skill>``. Do not derive aliases from arbitrary path ancestors
    like ``Users`` or ``/``.
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
    plugin_name = None
    if skills_idx >= 2 and _VERSION_RE.match(parts[skills_idx - 1]):
        plugin_name = parts[skills_idx - 2]
    elif skills_idx >= 2 and parts[skills_idx - 2] == "plugins":
        # marketplaces/<marketplace>/plugins/<plugin>/skills/<skill>/SKILL.md
        plugin_name = parts[skills_idx - 1]

    if plugin_name and plugin_name not in _STRUCTURE_NAMES and not plugin_name.startswith("temp_git_"):
        slugs.add(f"{plugin_name}:{skill_name}")
    return sorted(slugs)


def _iter_skill_files(root: Path) -> Iterable[Path]:
    """Yield SKILL.md files below root, following symlinked skill dirs safely."""
    seen_dirs: set[str] = set()
    try:
        root = Path(root)
    except TypeError:
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        try:
            real = os.path.realpath(dirpath)
        except OSError:
            dirnames[:] = []
            continue
        if real in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(real)
        if "SKILL.md" in filenames:
            yield Path(dirpath) / "SKILL.md"


def scan_catalog(roots=None) -> Dict[str, dict]:
    """Return {slug: {path, chars, tokens}} for every SKILL.md found.

    When a slug resolves to multiple files (nested `skills/skills/`), keep the
    entry with the shallowest path — that's the canonical install.
    """
    roots = roots or default_roots()
    catalog: Dict[str, dict] = {}
    for root in roots:
        for md in _iter_skill_files(root):
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
        _cache["data"] = scan_catalog(default_roots() + extra)
        _cache["at"] = now
        _cache["key"] = key
    return _cache["data"]


def tokens_for(slug: str, catalog: Optional[Dict[str, dict]] = None) -> Optional[int]:
    cat = catalog if catalog is not None else cached_catalog()
    info = cat.get(slug)
    return info["tokens"] if info else None
