# Contributing

Thanks for considering a contribution! This is a small, stdlib-only Python project — easy to run, easy to change.

## Running the tests

```bash
python3 -m unittest discover tests
```

That's it. No `pip install`, no fixtures to download. All tests run in under 5 seconds.

If you're fixing a bug, add a failing test first. If you're adding a feature, add a test that exercises the happy path.

## Running the dashboard locally

```bash
python3 cli.py dashboard --no-open
```

Open http://127.0.0.1:8080 in your browser. The server re-scans every 30 seconds and pushes updates over Server-Sent Events, so you'll see changes without a hard refresh.

## Code style

- **Stdlib only.** No `pip install`. If you think a feature genuinely needs a third-party dependency, open an issue first to discuss — we weigh "is this worth the install friction" heavily.
- **SQL: parameter binding always.** Any f-string in a SQL statement interpolates only internal values (hardcoded column names, placeholder lists built from internal UUIDs). User-reachable values go through `?`.
- **Small focused files.** If a file is creeping past ~400 lines and accreting distinct concerns, split it.
- **Type hints where they aid readability.** Not a hard requirement, but helpful on function signatures.
- **Docstrings explain *why*, not *what*.** The code already shows what.

Component layout: `cli.py` (entry points) → `token_dashboard/scanner.py` (JSONL → SQLite) → `token_dashboard/db.py` (query helpers) → `token_dashboard/server.py` (HTTP + SSE + `/api/*` routes) → `web/` (vanilla JS UI). See [`CLAUDE.md`](CLAUDE.md) for the short architecture overview. To add a new API route: add a handler branch in `token_dashboard/server.py`, put the SQL in a helper in `token_dashboard/db.py`, and add a test under `tests/`.

## Opening a pull request

1. Fork the repo.
2. Create a branch: `git checkout -b feat/<short-description>` or `fix/<short-description>`.
3. Make the change. Add or update tests.
4. Run `python3 -m unittest discover tests` — must be green.
5. Commit with a conventional-commit-style message: `feat: add X`, `fix: handle Y`, `docs: update Z`.
6. Push and open a PR against `main`. Describe the user-visible change and link to any relevant issue.

## Ideas that would genuinely help

- A CSV or JSON export of any route.
- A session-filter UI (currently everything is all-time or implicit-"recent").
- A GitHub Actions workflow that runs the tests on push.

## What we're not looking for

- Adding a frontend framework. Vanilla JS is a feature.
- Adding telemetry, analytics, or any outbound HTTP for user data. This dashboard is local-only and will stay that way.

## License

By contributing, you agree your contribution is licensed under the [MIT License](LICENSE).
