"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import datetime
import http.server
import json
import mimetypes
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

from .db import (
    clear_scan_data, default_claude_dir, get_setting, set_setting,
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    session_model_tokens,
    daily_token_breakdown, model_breakdown, skill_breakdown,
    workspaces_matrix, cross_workspace_leaks,
    subagent_breakdown, top_subagent_sessions,
    orchestration_breakdown, dispatch_tree,
)
from .pricing import load_pricing, cost_for, get_plan, set_plan
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .skills import cached_catalog
from .plugins import cached_plugins
from .mcp_catalog import scan_mcp
from .hooks_catalog import scan_hooks, scan_commands, scan_agents


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"

EVENTS: "queue.Queue[dict]" = queue.Queue()
# Keep cache resets and concurrent scans from interleaving with background scans.
SCAN_LOCK = threading.Lock()

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (settings, plan, tip key)
MAX_LIMIT = 1000

# Simple in-process response cache. TTL is slightly under the scan interval so
# cached data is never more than one scan cycle stale. Cleared after each scan.
_CACHE: "dict[str, dict]" = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300.0  # 5 min safety net; scan loop clears explicitly on new data


def _cache_get(key: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and time.time() - entry["ts"] < _CACHE_TTL:
            return entry["data"]
    return None


def _cache_set(key: str, data) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}


def _cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _bundle_cache_key(since, until) -> str:
    # Truncate to date so the key is stable within a day, enabling pre-warming.
    s = since[:10] if since else ""
    u = until[:10] if until else ""
    return f"/api/overview-bundle?since={s}&until={u}"


def _overview_bundle(db_path: str, since, until, pricing: dict) -> dict:
    """Run all overview-page queries and return a single combined payload."""
    by_model = model_breakdown(db_path, since, until)
    totals = overview_totals(db_path, since, until)
    cost_usd = 0.0
    for m in by_model:
        c = cost_for(m["model"], m, pricing)
        m["cost_usd"] = c["usd"]
        m["cost_estimated"] = c["estimated"]
        if c["usd"] is not None:
            cost_usd += c["usd"]
    totals["cost_usd"] = round(cost_usd, 4)
    return {
        "totals": totals,
        "projects": project_summary(db_path, since, until),
        "sessions": recent_sessions(db_path, limit=10, since=since, until=until),
        "tools": tool_token_breakdown(db_path, since, until),
        "daily": daily_token_breakdown(db_path, since, until),
        "byModel": by_model,
    }


_WARM_DAYS = [7, 30, 90, None]  # None = all time
_WARM_DEFAULT_DAYS = 30          # the range the UI lands on first


def _do_refresh(db_path: str, projects_dir: str, pricing: dict) -> None:
    """One-shot scan + cache-clear + warm, used by the manual /api/refresh endpoint."""
    if not SCAN_LOCK.acquire(blocking=False):
        EVENTS.put({"type": "scan-skip", "reason": "already-running", "ts": time.time()})
        return
    try:
        n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
        _cache_clear()
        EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
    except Exception as e:
        EVENTS.put({"type": "error", "message": str(e)})
    finally:
        SCAN_LOCK.release()


def _warm_one(db_path: str, pricing: dict, days) -> None:
    try:
        since = (
            (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            if days else None
        )
        key = _bundle_cache_key(since, None)
        if _cache_get(key) is None:
            _cache_set(key, _overview_bundle(db_path, since, None, pricing))
    except Exception:
        pass


def _warm_bundle(db_path: str, pricing: dict) -> None:
    """Pre-warm all time-range bundles serially.

    Serial — was 4 parallel threads each on its own SQLite connection, which
    caused severe contention with scan writes on cold boot.
    """
    for days in _WARM_DAYS:
        _warm_one(db_path, pricing, days)


def _send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, status: int, msg: str) -> None:
    _send_json(handler, {"error": msg}, status=status)


def _clamp_limit(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _empty_rtk_payload(available: bool) -> dict:
    return {
        "available": available,
        "install_url": "https://github.com/rtk-ai/rtk",
        "summary": None,
        "daily": [],
        "weekly": [],
        "monthly": [],
    }


def _rtk_payload(home=None) -> dict:
    home_path = Path(home) if home is not None else Path.home()
    rtk_bin = str(home_path / ".local" / "bin" / "rtk")
    if not Path(rtk_bin).is_file():
        return _empty_rtk_payload(False)
    env = dict(os.environ)
    env["PATH"] = str(home_path / ".local" / "bin") + ":" + env.get("PATH", "")
    try:
        r = subprocess.run(
            [rtk_bin, "gain", "--format", "json", "--all"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            data["available"] = True
            data["install_url"] = "https://github.com/rtk-ai/rtk"
            return data
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return _empty_rtk_payload(True)



def _resolve_static(root: Path, rel: str) -> Optional[Path]:
    """Resolve a request path under ``root``, or None if it escapes or is not a file.

    Containment uses ``relative_to``: a plain ``startswith(str(root))`` check
    would accept a sibling directory whose name shares the root's prefix
    (e.g. ``root`` + "-secret"), since the separator isn't part of the compare.
    """
    p = (root / rel.lstrip("/")).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


def _serve_static(handler, rel: str) -> None:
    p = _resolve_static(WEB_ROOT, rel)
    if p is None:
        handler.send_response(404)
        handler.end_headers()
        return
    body = p.read_bytes()
    ctype, _ = mimetypes.guess_type(str(p))
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _mcp_usage_calls(server_name: str, tools) -> Optional[int]:
    """Total tool-call count for one MCP server.

    MCP tool names are ``mcp__<server>__<tool>``; match the server segment
    exactly so a short name ("git") does not absorb another server's calls
    ("github"). ``tools`` is a tool_token_breakdown list. Returns None when the
    server has no recorded calls.
    """
    norm = server_name.lower().replace(" ", "_")
    total = 0
    for t in tools:
        parts = t["tool_name"].lower().split("__")
        if len(parts) >= 3 and parts[0] == "mcp" and parts[1] == norm:
            total += t["calls"]
    return total or None


def _claude_dir(db_path: str) -> Path:
    saved = get_setting(db_path, "claude_dir")
    return Path(saved).expanduser() if saved else default_claude_dir()


def _claude_dirs(db_path: str) -> list[str]:
    active = str(_claude_dir(db_path))
    raw = get_setting(db_path, "claude_dirs")
    dirs = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            dirs = [str(p) for p in parsed if isinstance(p, str) and p]
    out = []
    for path in [active, *dirs]:
        if path not in out:
            out.append(path)
    return out


def _remember_claude_dir(db_path: str, claude_dir: Path) -> None:
    path = str(claude_dir)
    dirs = [path, *[p for p in _claude_dirs(db_path) if p != path]]
    set_setting(db_path, "claude_dirs", json.dumps(dirs))


def _projects_dir(db_path: str, projects_override: Optional[str] = None) -> Path:
    if projects_override:
        return Path(projects_override).expanduser()
    return _claude_dir(db_path) / "projects"


def _validate_claude_dir(raw) -> Tuple[Optional[Path], Optional[str]]:
    if not isinstance(raw, str) or not raw.strip():
        return None, "claude_dir is required"
    path = Path(raw.strip()).expanduser()
    if not path.exists():
        return None, f"{path} does not exist"
    if not path.is_dir():
        return None, f"{path} is not a directory"
    projects = path / "projects"
    if projects.exists() and not projects.is_dir():
        return None, f"{projects} exists but is not a directory"
    return path, None


def _settings_payload(db_path: str, projects_override: Optional[str] = None) -> dict:
    claude_dir = _claude_dir(db_path)
    projects_dir = _projects_dir(db_path, projects_override)
    return {
        "claude_dir": str(claude_dir),
        "projects_dir": str(projects_dir),
        "projects_overridden": bool(projects_override),
        "claude_dirs": _claude_dirs(db_path),
    }


def build_handler(db_path: str, projects_dir: Optional[str] = None):
    pricing = load_pricing(PRICING_JSON)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query or "")
            path = url.path
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            cache_key = self.path  # includes query string
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview-bundle":
                bundle_key = _bundle_cache_key(since, until)
                cached = _cache_get(bundle_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = _overview_bundle(db_path, since, until, pricing)
                _cache_set(bundle_key, data)
                return _send_json(self, data)
            if path == "/api/overview":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                totals = overview_totals(db_path, since, until)
                cost_usd = 0.0
                for m in model_breakdown(db_path, since, until):
                    c = cost_for(m["model"], m, pricing)
                    if c["usd"] is not None:
                        cost_usd += c["usd"]
                totals["cost_usd"] = round(cost_usd, 4)
                _cache_set(cache_key, totals)
                return _send_json(self, totals)
            if path == "/api/prompts":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                sort = qs.get("sort", ["tokens"])[0]
                rows = expensive_prompts(db_path, limit=limit, sort=sort)
                for r in rows:
                    c = cost_for(r["model"], {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": r["cache_read_tokens"],
                        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                    }, pricing)
                    r["estimated_cost_usd"] = c["usd"]
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path == "/api/projects":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = project_summary(db_path, since, until)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/tools":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = tool_token_breakdown(db_path, since, until)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/sessions":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                )
                by_model = session_model_tokens(db_path, [s["session_id"] for s in data])
                for s in data:
                    total = 0.0
                    estimated = False
                    matched = False
                    for mt in by_model.get(s["session_id"], []):
                        c = cost_for(mt["model"], mt, pricing)
                        if c["usd"] is not None:
                            total += c["usd"]
                            matched = True
                            estimated = estimated or c["estimated"]
                        else:
                            estimated = True
                    s["cost_usd"] = round(total, 4) if matched else None
                    s["cost_estimated"] = estimated
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/daily":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = daily_token_breakdown(db_path, since, until)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/skills":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = skill_breakdown(db_path, since, until)
                catalog = cached_catalog(db_path)
                # Lazy import so deleting skill_budgets.py keeps the server bootable.
                from .skill_budgets import (
                    budget_for,
                    skill_actuals,
                    skill_costs,
                    skill_subagent_costs,
                )
                actuals = skill_actuals(db_path, since, until)
                costs = skill_costs(db_path, pricing, since, until)
                sub = skill_subagent_costs(db_path, pricing, since, until)
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
                    r["description"] = info["description"] if info else ""
                    r["budget_output_tokens"] = budget_for(r["skill"], catalog)
                    a = actuals.get(r["skill"])
                    r["p50_output_tokens"] = a["p50"] if a else None
                    r["p95_output_tokens"] = a["p95"] if a else None
                    r["over_budget"] = bool(
                        r["budget_output_tokens"]
                        and a
                        and a["p50"] > r["budget_output_tokens"] * 1.2
                    )
                    c = costs.get(r["skill"])
                    r["total_cost_usd"] = c["cost_usd"] if c else None
                    r["cost_estimated"] = bool(c and c["cost_estimated"])
                    s = sub.get(r["skill"])
                    r["subagent_cost_usd"] = s["cost_usd"] if s else None
                    r["subagent_output_tokens"] = s["output_tokens"] if s else 0
                    r["total_with_subagents_usd"] = (
                        (r["total_cost_usd"] or 0.0) + (r["subagent_cost_usd"] or 0.0)
                        if (r["total_cost_usd"] is not None or r["subagent_cost_usd"] is not None)
                        else None
                    )
                    if s and s["cost_estimated"]:
                        r["cost_estimated"] = True
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path == "/api/by-model":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = model_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                sid = path.rsplit("/", 1)[1]
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = session_turns(db_path, sid)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/workspaces":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = workspaces_matrix(db_path, since, until)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/cross-workspace-leaks":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = cross_workspace_leaks(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                )
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/subagents":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = subagent_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                top = top_subagent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                )
                orch = orchestration_breakdown(db_path, since, until)
                for bucket in ("by_kind", "by_entrypoint"):
                    for r in orch[bucket]:
                        c = cost_for(r["model"], r, pricing)
                        r["cost_usd"] = c["usd"]
                        r["cost_estimated"] = c["estimated"]
                tree = dispatch_tree(
                    db_path, limit=_clamp_limit(qs.get("limit", ["50"])[0], 50),
                    since=since, until=until,
                )
                for r in tree:
                    child_models = r["models"] or []
                    if child_models:
                        c = cost_for(child_models[0], r, pricing)
                        r["child_cost_usd"] = c["usd"]
                        r["child_cost_estimated"] = c["estimated"]
                    else:
                        r["child_cost_usd"] = None
                        r["child_cost_estimated"] = True
                data = {
                    "breakdown": rows,
                    "top_sessions": top,
                    "by_kind": orch["by_kind"],
                    "by_entrypoint": orch["by_entrypoint"],
                    "sdk_runs": orch["sdk_runs"],
                    "dispatch_tree": tree,
                }
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/tips":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = all_tips(db_path)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/plugins":
                return _send_json(self, cached_plugins())
            if path == "/api/mcp":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = scan_mcp()
                tools = tool_token_breakdown(db_path, since, until)
                for r in rows:
                    r["usage_calls"] = _mcp_usage_calls(r["name"], tools)
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path == "/api/hooks":
                return _send_json(self, scan_hooks())
            if path == "/api/commands":
                return _send_json(self, scan_commands())
            if path == "/api/agents":
                return _send_json(self, scan_agents())
            if path == "/api/settings":
                return _send_json(self, _settings_payload(db_path, projects_dir))
            if path == "/api/scan":
                with SCAN_LOCK:
                    n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
                return _send_json(self, n)
            if path == "/api/rtk":
                return _send_json(self, _rtk_payload())
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                while True:
                    try:
                        evt = EVENTS.get(timeout=15)
                        chunk = f"data: {json.dumps(evt, default=str)}\n\n".encode()
                    except queue.Empty:
                        chunk = b": ping\n\n"
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_error(self, 400, "invalid Content-Length")
            if length < 0 or length > MAX_POST_BYTES:
                return _send_error(self, 413, f"body too large (max {MAX_POST_BYTES} bytes)")
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return _send_error(self, 400, "invalid JSON")
            if not isinstance(body, dict):
                return _send_error(self, 400, "body must be a JSON object")
            try:
                if url.path == "/api/plan":
                    set_plan(db_path, body.get("plan", "api"))
                    _cache_clear()
                    return _send_json(self, {"ok": True})
                if url.path == "/api/settings":
                    if "plan" in body:
                        set_plan(db_path, body.get("plan", "api"))
                    if "claude_dir" in body:
                        claude_dir, err = _validate_claude_dir(body.get("claude_dir"))
                        if err:
                            return _send_error(self, 400, err)
                        with SCAN_LOCK:
                            set_setting(db_path, "claude_dir", str(claude_dir))
                            _remember_claude_dir(db_path, claude_dir)
                            if body.get("reset_scan_data") is True:
                                clear_scan_data(db_path)
                    _cache_clear()
                    return _send_json(self, {"ok": True, **_settings_payload(db_path, projects_dir)})
                if url.path == "/api/tips/dismiss":
                    dismiss_tip(db_path, body.get("key", ""))
                    _cache_clear()
                    return _send_json(self, {"ok": True})
                if url.path == "/api/refresh":
                    threading.Thread(
                        target=_do_refresh, args=(db_path, projects_dir, pricing), daemon=True
                    ).start()
                    return _send_json(self, {"ok": True})
            except Exception as e:
                return _send_error(self, 503, str(e))
            self.send_response(404)
            self.end_headers()

    return H


def _scan_loop(db_path: str, projects_dir: Optional[str] = None, interval: float = 60.0):
    # Sleep first: avoid contending with the synchronous boot warm + the user's
    # first request. The boot warm covers initial cache population.
    time.sleep(interval)
    while True:
        try:
            if SCAN_LOCK.acquire(blocking=False):
                try:
                    n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
                    if n["messages"] > 0:
                        _cache_clear()
                    # Emit the event even when messages == 0 so the frontend's
                    # "Getting latest data…" banner clears once the scan finishes.
                    # The frontend uses n.messages to decide whether to flag new data.
                    EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
                finally:
                    SCAN_LOCK.release()
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: Optional[str] = None):
    pricing = load_pricing(PRICING_JSON)
    # Warm the default range (30d) synchronously before opening the port so
    # the user's first paint is a cache hit. Then warm 7d/90d/all in the
    # background while the server is already serving.
    _warm_one(db_path, pricing, _WARM_DEFAULT_DAYS)
    def _warm_rest():
        for days in _WARM_DAYS:
            if days != _WARM_DEFAULT_DAYS:
                _warm_one(db_path, pricing, days)
    threading.Thread(target=_warm_rest, daemon=True).start()
    threading.Thread(target=_scan_loop, args=(db_path, projects_dir), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.serve_forever()
