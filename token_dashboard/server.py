"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import http.server
import json
import mimetypes
import queue
import threading
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

from .db import (
    clear_scan_data, default_claude_dir, get_setting, set_setting,
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    daily_token_breakdown, model_breakdown, skill_breakdown,
)
from .pricing import load_pricing, cost_for, get_plan, set_plan
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .skills import cached_catalog


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"

EVENTS: "queue.Queue[dict]" = queue.Queue()
# Keep cache resets from interleaving with background or manual scans.
SCAN_LOCK = threading.Lock()

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (settings, plan, tip key)
MAX_LIMIT = 1000


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


def _serve_static(handler, rel: str) -> None:
    rel = rel.lstrip("/")
    p = (WEB_ROOT / rel).resolve()
    if not str(p).startswith(str(WEB_ROOT.resolve())) or not p.is_file():
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
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview":
                totals = overview_totals(db_path, since, until)
                cost_usd = 0.0
                for m in model_breakdown(db_path, since, until):
                    c = cost_for(m["model"], m, pricing)
                    if c["usd"] is not None:
                        cost_usd += c["usd"]
                totals["cost_usd"] = round(cost_usd, 4)
                return _send_json(self, totals)
            if path == "/api/prompts":
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
                return _send_json(self, rows)
            if path == "/api/projects":
                return _send_json(self, project_summary(db_path, since, until))
            if path == "/api/tools":
                return _send_json(self, tool_token_breakdown(db_path, since, until))
            if path == "/api/sessions":
                return _send_json(self, recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                ))
            if path == "/api/daily":
                return _send_json(self, daily_token_breakdown(db_path, since, until))
            if path == "/api/skills":
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
                return _send_json(self, rows)
            if path == "/api/by-model":
                rows = model_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                sid = path.rsplit("/", 1)[1]
                return _send_json(self, session_turns(db_path, sid))
            if path == "/api/tips":
                return _send_json(self, all_tips(db_path))
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/settings":
                return _send_json(self, _settings_payload(db_path, projects_dir))
            if path == "/api/scan":
                with SCAN_LOCK:
                    n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
                return _send_json(self, n)
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
            if url.path == "/api/plan":
                set_plan(db_path, body.get("plan", "api"))
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
                return _send_json(self, {"ok": True, **_settings_payload(db_path, projects_dir)})
            if url.path == "/api/tips/dismiss":
                dismiss_tip(db_path, body.get("key", ""))
                return _send_json(self, {"ok": True})
            self.send_response(404)
            self.end_headers()

    return H


def _scan_loop(db_path: str, projects_dir: Optional[str] = None, interval: float = 30.0):
    while True:
        try:
            with SCAN_LOCK:
                n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
            if n["messages"] > 0:
                EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: Optional[str] = None):
    threading.Thread(target=_scan_loop, args=(db_path, projects_dir), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.serve_forever()
