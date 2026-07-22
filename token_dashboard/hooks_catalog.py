"""Hooks / commands / agents catalog -- the smaller 'operational' gallery."""
from __future__ import annotations

import re
from pathlib import Path

from .jsonutil import read_json_dict as _read_json

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
COMMANDS_DIR = CLAUDE_DIR / "commands"

# Optional drive-letter prefix so absolute Windows paths (e.g. "C:/Users/...")
# keep their drive when extracted -- without it, Path(path).is_file() checks
# the wrong drive (whichever one happens to be the current working directory's).
_SCRIPT_RE = re.compile(r"((?:[A-Za-z]:)?/[^\s\"']+\.(?:sh|py|mjs|js|ps1))")


def scan_hooks() -> list:
    settings = _read_json(SETTINGS_JSON)
    out = []
    seen = set()
    for event, entries in (settings.get("hooks") or {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                m = _SCRIPT_RE.search(cmd)
                path = m.group(1) if m else None
                key = path or cmd
                if key in seen:
                    continue
                seen.add(key)
                exists = Path(path).is_file() if path else True
                out.append({
                    "name": Path(path).name if path else f"{event} (inline)",
                    "event": event,
                    "file_path": path,
                    "status": "ok" if exists else "broken — script missing",
                })
    return out


def scan_commands() -> list:
    out = []
    if COMMANDS_DIR.is_dir():
        for f in sorted(COMMANDS_DIR.glob("*.md")):
            out.append({"name": f.stem, "file_path": f.as_posix(), "source": "user"})
    from .plugins import cached_plugins
    for p in cached_plugins():
        if not p["install_path"]:
            continue
        cmd_dir = Path(p["install_path"]) / "commands"
        if cmd_dir.is_dir():
            for f in sorted(cmd_dir.rglob("*.md")):
                out.append({"name": f.stem, "file_path": f.as_posix(), "source": p["name"]})
    return out


def scan_agents() -> list:
    out = []
    from .plugins import cached_plugins
    for p in cached_plugins():
        if not p["install_path"]:
            continue
        agents_dir = Path(p["install_path"]) / "agents"
        if agents_dir.is_dir():
            for f in sorted(agents_dir.glob("*.md")):
                out.append({"name": f.stem, "file_path": f.as_posix(), "source": p["name"]})
    return out
