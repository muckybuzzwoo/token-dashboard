"""Plugin catalog: installed plugins, enabled state, component inventory."""
from __future__ import annotations

import time
from pathlib import Path

from .jsonutil import read_json_dict as _read_json

CLAUDE_DIR = Path.home() / ".claude"
INSTALLED_JSON = CLAUDE_DIR / "plugins" / "installed_plugins.json"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"


def _plugin_manifest(install_path: Path, plugin_name: str) -> dict:
    """This plugin's own plugin.json, falling back to its marketplace.json entry."""
    direct = _read_json(install_path / ".claude-plugin" / "plugin.json")
    if direct.get("description"):
        return direct
    mp = _read_json(install_path / ".claude-plugin" / "marketplace.json")
    for p in mp.get("plugins", []):
        if p.get("name") == plugin_name:
            return p
    return {}


def _component_counts(install_path: Path) -> dict:
    counts = {"skills": 0, "agents": 0, "commands": 0, "tokens": 0}
    skills_dir = install_path / "skills"
    if skills_dir.is_dir():
        for p in skills_dir.iterdir():
            md = p / "SKILL.md"
            if p.is_dir() and md.is_file():
                counts["skills"] += 1
                try:
                    counts["tokens"] += md.stat().st_size // 4
                except OSError:
                    pass
    agents_dir = install_path / "agents"
    if agents_dir.is_dir():
        counts["agents"] = sum(1 for _ in agents_dir.glob("*.md"))
    commands_dir = install_path / "commands"
    if commands_dir.is_dir():
        counts["commands"] = sum(1 for _ in commands_dir.rglob("*.md"))
    return counts


def scan_plugins() -> list:
    installed = _read_json(INSTALLED_JSON).get("plugins", {})
    enabled_map = _read_json(SETTINGS_JSON).get("enabledPlugins", {})
    out = []
    for key, entries in installed.items():
        if not entries:
            continue
        latest = entries[0]
        install_path = Path(latest.get("installPath", ""))
        name, _, source = key.partition("@")
        manifest = _plugin_manifest(install_path, name)
        out.append({
            "key": key,
            "name": name,
            "source": source,
            "version": latest.get("version", "?"),
            "install_path": install_path.as_posix() if install_path.parts else None,
            "description": manifest.get("description", ""),
            "enabled": enabled_map.get(key, True),
            "components": _component_counts(install_path),
        })
    out.sort(key=lambda p: p["name"])
    return out


_cache: dict = {"at": 0.0, "data": []}
_TTL_SECONDS = 60.0


def cached_plugins() -> list:
    now = time.time()
    if now - _cache["at"] > _TTL_SECONDS:
        _cache["data"] = scan_plugins()
        _cache["at"] = now
    return _cache["data"]
