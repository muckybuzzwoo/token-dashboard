"""MCP server catalog: local servers from ~/.claude.json + servers bundled by
installed plugins (their .mcp.json).

Account-level claude.ai connectors (Gmail, Calendar, Slack ...) are intentionally
NOT listed. They have no local config file, so they can't be discovered reliably
-- the only local trace is a stale, incomplete "ever connected" name list. The
dashboard shows only what it can read from disk, so nothing here needs
hand-maintenance: plug a server in locally or install a plugin that ships one and
it shows up on the next scan.
"""
from __future__ import annotations

from pathlib import Path

from .jsonutil import read_json_dict as _read_json

CLAUDE_JSON = Path.home() / ".claude.json"


def _entry(name: str, cfg: dict, kind: str, source: str, file_path: str | None) -> dict:
    return {
        "name": name,
        "kind": kind,
        "source": source,
        "transport": "http" if "url" in cfg else "stdio",
        "command_or_url": cfg.get("url") or cfg.get("command", ""),
        "description": "",
        "status": "configured",
        "file_path": file_path,
    }


def _local_servers() -> list:
    data = _read_json(CLAUDE_JSON)
    seen: dict = {}
    for name, cfg in (data.get("mcpServers") or {}).items():
        seen.setdefault(name, cfg)
    for proj in (data.get("projects") or {}).values():
        for name, cfg in (proj.get("mcpServers") or {}).items():
            seen.setdefault(name, cfg)
    fp = CLAUDE_JSON.as_posix()
    return [_entry(n, c, "local", "user", fp) for n, c in seen.items()]


def _plugin_servers() -> list:
    """MCP servers shipped by installed, enabled plugins (their .mcp.json).

    Manifest-driven like the skill catalog: only plugins in installed_plugins.json
    (never marketplace catalog clones) and only enabled ones, so the page reflects
    what's actually active rather than everything the marketplace offers.
    """
    from .plugins import cached_plugins
    out = []
    for p in cached_plugins():
        if not p["install_path"] or not p["enabled"]:
            continue
        mcp_file = Path(p["install_path"]) / ".mcp.json"
        raw = _read_json(mcp_file)
        # Plugins ship a flat {server_name: cfg} map; tolerate the wrapped
        # {"mcpServers": {...}} form too. Either way each value is a config dict.
        wrapped = raw.get("mcpServers")
        servers = wrapped if isinstance(wrapped, dict) else raw
        fp = mcp_file.as_posix()
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                out.append(_entry(name, cfg, "plugin", p["name"], fp))
    return out


def scan_mcp() -> list:
    seen = set()
    out = []
    # Local servers first so a locally-configured server wins a name collision
    # with a plugin-shipped one of the same name.
    for entry in _local_servers() + _plugin_servers():
        if entry["name"] in seen:
            continue
        seen.add(entry["name"])
        out.append(entry)
    out.sort(key=lambda s: s["name"])
    return out
