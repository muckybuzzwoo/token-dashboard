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
from urllib.parse import urlparse, parse_qs

from .db import (
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

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (plan, tip key)
MAX_LIMIT = 1000

# Simple in-process response cache. TTL is slightly under the scan interval so
# cached data is never more than one scan cycle stale. Cleared after each scan.
_CACHE: "dict[str, dict]" = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300.0  # 5 min safety net; scan loop clears explicitly on new data
_SCAN_LOCK = threading.Lock()


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
    if not _SCAN_LOCK.acquire(blocking=False):
        EVENTS.put({"type": "scan-skip", "reason": "already-running", "ts": time.time()})
        return
    try:
        n = scan_dir(projects_dir, db_path)
        _cache_clear()
        EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
    except Exception as e:
        EVENTS.put({"type": "error", "message": str(e)})
    finally:
        _SCAN_LOCK.release()


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


def build_handler(db_path: str, projects_dir: str):
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
                catalog = cached_catalog()
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
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
            if path == "/api/tips":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = all_tips(db_path)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/scan":
                n = scan_dir(projects_dir, db_path)
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
                    return _send_json(self, {"ok": True})
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


def _scan_loop(db_path: str, projects_dir: str, pricing: dict, interval: float = 60.0):
    # Sleep first: avoid contending with the synchronous boot warm + the user's
    # first request. The boot warm covers initial cache population.
    time.sleep(interval)
    while True:
        try:
            if _SCAN_LOCK.acquire(blocking=False):
                try:
                    n = scan_dir(projects_dir, db_path)
                    if n["messages"] > 0:
                        _cache_clear()
                        EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
                finally:
                    _SCAN_LOCK.release()
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: str):
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
    threading.Thread(target=_scan_loop, args=(db_path, projects_dir, pricing), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.serve_forever()
