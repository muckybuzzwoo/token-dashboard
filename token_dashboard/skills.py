"""Skill catalog: locate SKILL.md files and map slugs to file sizes.

A skill on disk lives at one of:
  ~/.claude/skills/<name>/SKILL.md                     -> slug "<name>"
  ~/.claude/scheduled-tasks/<name>/SKILL.md            -> slug "<name>"
  ~/.claude/plugins/marketplaces/*/plugins/<plugin>/skills/<name>/SKILL.md
      -> registers TWO slugs: "<plugin>:<name>" and "<name>"
      (Claude Code accepts either form in the Skill tool.)

Sizes are in chars; token estimate is chars // 4 (the same approximation
`scanner._extract_results` uses for tool-result tokens).

Active-set filtering
--------------------
`~/.claude/plugins/installed_plugins.json` is Claude Code's source of truth
for which plugins are actually loaded. Plenty of SKILL.md files live on disk
under `~/.claude/plugins/` as marketplace clones (downloaded metadata) without
ever being installed -- they don't consume context and shouldn't show up in
budget tips. We honour the manifest: only plugin paths listed there contribute
to the catalog.

Each catalog entry carries its scope so callers can reason about per-context
activity:
  scope="user-global"     -> active in every session (user-skills, scheduled-
                             tasks, scope=user manifest entries)
  scope="project-global"  -> active only when cwd is under project_path
                             (manifest scope=project entries)
  scope="project-local"   -> active only when cwd is under project_path
                             (cwd/.claude/skills/ discovered from DB cwds)
  scope="unknown"         -> legacy fallback when manifest is missing/unreadable;
                             treated as user-global by active-set helpers
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


_USER_SKILLS_ROOT = Path.home() / ".claude" / "skills"
_SCHEDULED_TASKS_ROOT = Path.home() / ".claude" / "scheduled-tasks"
_PLUGINS_MANIFEST = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
_LEGACY_PLUGINS_ROOT = Path.home() / ".claude" / "plugins"
_USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


# Legacy module-level roots kept for backward compatibility with tests that
# monkey-patch this list. Production code goes through `_default_roots()`
# instead, which honours the installed_plugins.json manifest.
_DEFAULT_ROOTS = [
    _USER_SKILLS_ROOT,
    _SCHEDULED_TASKS_ROOT,
    _LEGACY_PLUGINS_ROOT,
]


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


# ── Active-roots resolution ──────────────────────────────────────────────────


def _read_enabled_plugin_ids(settings_path=None) -> Optional[set]:
    """Return the set of enabled plugin IDs, or None when enabledPlugins is absent.

    None means "apply no filter" — all installed plugins are treated as active.
    This preserves backward compatibility with settings.json files that don't
    have the enabledPlugins key (the common case).
    """
    path = settings_path or _USER_SETTINGS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return None
    return {str(k) for k, v in enabled.items() if v}


def _read_installed_plugin_entries(
    manifest_path: Optional[Path] = None,
    settings_path: Optional[Path] = None,
) -> Optional[List[dict]]:
    """Read installed_plugins.json and return active-plugin metadata.

    Returns ``[]`` for a *valid-but-empty* manifest (user has no plugins
    installed -- legitimate state, not a fallback condition). Returns
    ``None`` when the manifest is missing, unreadable, or structurally
    broken -- callers should distinguish these because the empty case must
    NOT trigger a legacy blanket scan (that would re-introduce marketplace
    clones we explicitly want excluded).

    When ``settings_path`` / ``~/.claude/settings.json`` contains an
    ``enabledPlugins`` dict, entries for plugins set to ``false`` are
    excluded. Missing key = no filter = all installed plugins are active.

    ``ValueError`` covers both ``json.JSONDecodeError`` (malformed JSON) and
    ``UnicodeDecodeError`` (corrupt byte sequences) -- both are subclasses
    of ``ValueError``.

    The manifest schema (Claude Code v2) is::

        {
          "version": 2,
          "plugins": {
            "<plugin>@<marketplace>": [
              {"installPath": "...", "scope": "user"|"project",
               "projectPath": "...", ...},
              ...
            ]
          }
        }

    The same plugin can appear with multiple entries (e.g. once with
    scope=user and again with scope=project for a specific repo). We return
    every entry so callers can preserve the scope semantics for each install.
    """
    path = manifest_path or _PLUGINS_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        return None
    enabled_ids = _read_enabled_plugin_ids(settings_path)
    out: List[dict] = []
    for plugin_id, entries in plugins.items():
        if enabled_ids is not None and plugin_id not in enabled_ids:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ip = entry.get("installPath")
            if not ip:
                continue
            scope = entry.get("scope", "user")
            project_path = entry.get("projectPath")
            # Defensive: a scope=project entry without projectPath would be
            # invisible to is_active_in_cwd (no path to compare against).
            # Claude Code loads such plugins in every session, so degrade to
            # scope=user rather than silently dropping the skill from budget
            # and dead-skills reasoning.
            if scope == "project" and not project_path:
                scope = "user"
            out.append({
                "install_path": Path(ip),
                "scope": scope,
                "project_path": project_path,
            })
    return out


def _default_roots(
    manifest_path: Optional[Path] = None,
    settings_path: Optional[Path] = None,
) -> List[dict]:
    """Return ``[{root, scope, project_path}, ...]`` for currently-installed skill roots.

    Sources, in order:
      1. ``~/.claude/skills/``           — scope=user-global
      2. ``~/.claude/scheduled-tasks/``  — scope=user-global
      3. Every ``installPath`` from ``installed_plugins.json``, tagged with
         scope=user-global (for scope=user entries) or scope=project-global
         (for scope=project entries, carrying projectPath). Entries for plugins
         disabled via ``enabledPlugins`` in ``settings.json`` are excluded.
      4. Legacy fallback when the manifest is missing or empty:
         ``~/.claude/plugins/`` blanket-scanned with scope=unknown.

    The unknown-scope fallback preserves pre-manifest behaviour on installs
    that don't yet write the JSON file. Active-set helpers treat unknown as
    user-global so the tip still fires; it just can't differentiate scopes.
    """
    roots: List[dict] = [
        {"root": _USER_SKILLS_ROOT, "scope": "user-global", "project_path": None},
        {"root": _SCHEDULED_TASKS_ROOT, "scope": "user-global", "project_path": None},
    ]
    entries = _read_installed_plugin_entries(manifest_path, settings_path)
    if entries is None:
        # Manifest missing/unreadable/structurally broken → legacy blanket
        # scan so the dashboard still works on installs without the JSON
        # manifest. A user-uninstall-everything state returns [] (not None)
        # and correctly skips this fallback.
        roots.append({
            "root": _LEGACY_PLUGINS_ROOT,
            "scope": "unknown",
            "project_path": None,
        })
    else:
        for e in entries:
            scope = "user-global" if e["scope"] == "user" else "project-global"
            roots.append({
                "root": e["install_path"],
                "scope": scope,
                "project_path": e["project_path"],
            })
    return roots


def _safe_scan_root(root: Path) -> Optional[Path]:
    """Resolve a root and reject obviously-unsafe scan starting points.

    A misbehaving plugin installer (or a tampered manifest) could set
    ``installPath`` to ``/`` or ``C:\\``. ``Path.rglob`` would then walk the
    entire filesystem, stalling every tip endpoint for the duration. Reject
    any path whose resolved form is a filesystem root (no parent except
    itself) -- this eliminates the DoS surface without restricting legitimate
    project locations outside the user's home (e.g. ``D:\\repos\\foo``).

    Returns the resolved Path on success, or None when the root is unsafe
    or unresolvable -- callers should skip silently.
    """
    try:
        resolved = root.resolve(strict=False)
    except OSError:
        return None
    # A filesystem root has the property `parent == self`.
    if resolved.parent == resolved:
        return None
    return resolved


def _iter_skill_files(root: Path) -> Iterable[Path]:
    """Yield SKILL.md files under root, following directory symlinks safely.

    Uses os.walk(followlinks=True) with a seen-real-paths set to prevent
    infinite loops on circular symlinks. Path.rglob() does not follow
    symlinks on all platforms/Python versions.
    """
    seen: set = set()
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=True):
        try:
            real = os.path.realpath(dirpath)
        except OSError:
            dirnames[:] = []
            continue
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        if "SKILL.md" in filenames:
            yield Path(dirpath) / "SKILL.md"


def _normalise_roots(roots) -> List[dict]:
    """Accept either bare Paths (legacy) or root-dicts and return root-dicts.

    Bare-path entries get scope='unknown' / project_path=None — that's the
    historical "no scope information" baseline.
    """
    norm: List[dict] = []
    for r in roots:
        if isinstance(r, dict):
            norm.append(r)
        else:
            norm.append({"root": Path(r), "scope": "unknown", "project_path": None})
    return norm


def scan_catalog(roots=None) -> Dict[str, dict]:
    """Return ``{slug: {path, chars, tokens, scope, project_path}}`` for every SKILL.md.

    ``roots`` accepts either:
      - ``None`` — uses ``_default_roots()`` (production path).
      - A list of ``Path`` — legacy form; all matches tagged scope='unknown'.
      - A list of ``{root, scope, project_path}`` dicts — explicit scope tagging.

    When a slug resolves to multiple files (nested ``skills/skills/``), keep
    the entry with the shallowest path — that's the canonical install.
    """
    if roots is None:
        roots = _default_roots()
    norm = _normalise_roots(roots)
    catalog: Dict[str, dict] = {}
    for spec in norm:
        root = _safe_scan_root(spec["root"])
        if root is None or not root.is_dir():
            continue
        for md in _iter_skill_files(root):
            try:
                chars = md.stat().st_size
            except OSError:
                continue
            entry = {
                "path": str(md),
                "chars": chars,
                "tokens": chars // 4,
                "scope": spec["scope"],
                "project_path": spec["project_path"],
            }
            for slug in _slugs_for(md):
                prev = catalog.get(slug)
                if prev is None or len(md.parts) < len(Path(prev["path"]).parts):
                    catalog[slug] = entry
    return catalog


def _project_skill_roots_from_cwds(cwds: Iterable[str]) -> List[dict]:
    """Return root-dicts for the innermost ``.claude/skills/`` per cwd.

    Matches Claude Code's resolution rule: walk up from cwd and use the first
    ``.claude/skills/`` found, so a nested repo uses its own skills, not a
    parent's. Each discovered root is tagged scope='project-local' with
    project_path = the directory that contains the ``.claude/`` folder.
    """
    seen: dict[Path, dict] = {}
    for cwd in cwds:
        if not cwd:
            continue
        p = Path(cwd)
        for ancestor in (p, *p.parents):
            candidate = ancestor / ".claude" / "skills"
            if candidate.is_dir():
                if candidate not in seen:
                    seen[candidate] = {
                        "root": candidate,
                        "scope": "project-local",
                        "project_path": str(ancestor),
                    }
                break
    return [seen[k] for k in sorted(seen)]


def _cwds_from_db(db_path) -> list[str]:
    from .db import connect
    with connect(db_path) as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT cwd FROM messages WHERE cwd IS NOT NULL"
        )]


def is_active_in_cwd(
    scope: str,
    project_path: Optional[str],
    cwd: Optional[str],
) -> bool:
    """True if a skill with this scope/project_path is loaded when working in cwd.

    Rules:
      * ``user-global`` and ``unknown`` are always active.
      * ``project-global`` and ``project-local`` are active only when cwd is
        equal to project_path or a descendant of it.
      * Missing project_path or cwd defaults to inactive for project-scoped
        skills (caller must supply cwd to evaluate them).

    Comparison uses ``Path.relative_to`` against the resolved paths so that
    different separators / trailing slashes / case normalisation (Windows)
    don't break the check.
    """
    if scope in ("user-global", "unknown"):
        return True
    if scope in ("project-global", "project-local"):
        if not project_path or not cwd:
            return False
        try:
            Path(cwd).resolve().relative_to(Path(project_path).resolve())
            return True
        except (OSError, ValueError):
            return False
    return False


_cache: dict = {"at": 0.0, "data": {}, "key": None}
_TTL_SECONDS = 60.0


def cached_catalog(db_path=None) -> Dict[str, dict]:
    """scan_catalog() with a simple in-process TTL cache.

    When ``db_path`` is provided, extra roots are derived from the distinct
    cwds in ``messages`` so project-local ``.claude/skills/`` directories are
    included (tagged scope='project-local').
    """
    now = time.time()
    key = str(db_path) if db_path else None
    if now - _cache["at"] > _TTL_SECONDS or _cache["key"] != key:
        base = _default_roots()
        extra = _project_skill_roots_from_cwds(_cwds_from_db(db_path)) if db_path else []
        _cache["data"] = scan_catalog(base + extra)
        _cache["at"] = now
        _cache["key"] = key
    return _cache["data"]


def tokens_for(slug: str, catalog: Optional[Dict[str, dict]] = None) -> Optional[int]:
    cat = catalog if catalog is not None else cached_catalog()
    info = cat.get(slug)
    return info["tokens"] if info else None
