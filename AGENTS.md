# Repository Guidelines

## Project Structure & Module Organization

Token Dashboard is a stdlib-only Python app with a vanilla JS frontend. `cli.py` provides the command-line entry points. Core Python modules live in `token_dashboard/`: `scanner.py` parses Claude JSONL transcripts, `db.py` owns SQLite helpers, `server.py` serves `/api/*` and SSE, and `pricing.py`, `skills.py`, and `tips.py` hold focused domain logic. Frontend assets are in `web/`, with route modules in `web/routes/` and vendored ECharts at `web/echarts.min.js`. Tests live in `tests/`, with fixtures under `tests/fixtures/`. Project notes and limitations are in `docs/`.

## Build, Test, and Development Commands

This project has no build step and no package install. In this workspace, prefix shell commands with `rtk`; contributors without `rtk` can run the command after it directly.

```bash
rtk python3 -m unittest discover tests
rtk python3 cli.py scan
rtk python3 cli.py dashboard --no-open
rtk curl http://127.0.0.1:8080/api/overview
```

The test command runs the full offline suite. `scan` refreshes the local SQLite cache. `dashboard --no-open` starts the local server without launching a browser. The `curl` command sanity-checks an API endpoint.

## Coding Style & Naming Conventions

Use Python 3.8+ and the standard library only. Keep files small and single-purpose; split modules that grow beyond roughly 400 lines or mix unrelated concerns. Use four-space indentation, `snake_case` for Python names, and clear route-oriented names for frontend modules such as `overview.js` or `sessions.js`. SQL must use parameter binding for user-reachable values; f-strings in SQL are acceptable only for internal column names or placeholder lists. Add type hints and docstrings when they clarify intent.

## Testing Guidelines

Tests use `unittest` and should be deterministic, offline, and fast. Name files `test_<area>.py` and test methods `test_<behaviour>`. Add or update tests for scanner parsing, database queries, API responses, and frontend-affecting data contracts when behaviour changes. Bug fixes should include a regression test where practical.

## Commit & Pull Request Guidelines

Recent history uses conventional commit prefixes, for example `feat:`, `fix:`, `perf:`, and `chore:`. Keep commit subjects imperative and specific, such as `fix: dedupe streaming snapshots by message id`. Pull requests should describe the user-visible change, note tests run, link related issues, and include screenshots or short recordings for UI changes.

## Security & Configuration Tips

Keep the dashboard local-only. Do not add telemetry or outbound calls for user data. Never commit generated databases or Claude transcript data. Configuration is via `PORT`, `HOST`, `CLAUDE_PROJECTS_DIR`, `TOKEN_DASHBOARD_DB`, and `pricing.json`; avoid binding `HOST=0.0.0.0` unless the network exposure is intentional.
