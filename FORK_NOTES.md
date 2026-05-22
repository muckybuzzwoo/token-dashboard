# Fork notes

> **Last updated:** 2026-05-22 — see [recent commits](https://github.com/muckybuzzwoo/token-dashboard/commits/main) for the running change log.

This fork integrates community pull requests that have not been merged into the upstream [nateherkai/token-dashboard](https://github.com/nateherkai/token-dashboard) repository, because upstream is currently inactive.

Each integration was reviewed for security concerns (OWASP-style checks for SQL injection, path traversal, command injection, network exfiltration, dependency hygiene) and a passing test suite (`python -m unittest discover tests`).

## Integrated upstream PRs

| PR | Title | Author | Highlights |
|---|---|---|---|
| [#9](https://github.com/nateherkai/token-dashboard/pull/9) | `chore(ci): run tests on push and PR` | sebastientang | GitHub Actions workflow that runs the test suite on Python 3.8 / 3.10 / 3.12. |
| [#2](https://github.com/nateherkai/token-dashboard/pull/2) | Scanner progress (FirstTimeScanner) | lucidqdreams | Live per-file progress on stderr during scans, throttled to ~5 updates/sec, tty-aware. |
| [#10](https://github.com/nateherkai/token-dashboard/pull/10) | CSV and Markdown export for the Prompts route | sebastientang | "Copy MD" copies the current sort/filter to the clipboard. "Download CSV" produces a properly escaped CSV. |
| [#15](https://github.com/nateherkai/token-dashboard/pull/15) | Pricing fix + team plan labels | hacker4ofakind | Corrects Opus 4.5 / 4.6 / 4.7 pricing to current Anthropic rates ($5 input / $25 output, with matching cache rates). Also adds `team` / `team-premium` plan labels for the Settings dropdown. **Before this fix the dashboard showed ~3× inflated Opus costs.** |
| [#12](https://github.com/nateherkai/token-dashboard/pull/12) | Skills budget attribution + slash-command scanner | sebastientang | Adds budget-vs-actual tracking (p50 / p95 output tokens), total cost, and Task/Agent-dispatched subagent attribution to the Skills route. Synthesizes a `Skill` row from user-typed `<command-name>/<slug>` messages so slash-command usage appears in the analytics. Supersedes PRs #6 and #11. |
| [#7](https://github.com/nateherkai/token-dashboard/pull/7) | Configure Claude folder settings (UI) | kiwiswift | Settings tab can now switch the `.claude` folder at runtime. Persists the selection in the SQLite DB. Includes an opt-in "clear cached transcript data" toggle for clean separation across profiles. |
| [#13](https://github.com/nateherkai/token-dashboard/pull/13) | Large-database performance + optional RTK savings view | NewAiCoder | Materialised summary tables (daily / per-project / per-model / tools / sessions), WAL journal mode, batched scanner commits, in-process response cache, synchronous warm of the default 30-day range before serving the first request, sortable tables, URL-persisted Sessions filters, optional RTK savings tab when the [rtk](https://github.com/rtk-ai/rtk) CLI is installed at `~/.local/bin/rtk` (the tab gracefully degrades on Windows or when RTK is absent). |
| [#18](https://github.com/nateherkai/token-dashboard/pull/18) | `perf(scanner): batch inserts + bulk snapshot eviction (~33x on 1 GB)` | Dig1taly | Replaces the per-message INSERT/DELETE loop in `scan_file()` with batched `executemany()` and a single temp-table-driven snapshot eviction. Composes with this fork's PR #13 file-level batched commits — they operate on different layers. The fork-specific adaptation preserves the `days` / `sessions` sets `scan_dir()` needs for incremental rebuild of the materialised summary tables. Measured locally: a full rescan of 589 files / 63k messages drops from minutes to **~7.5 seconds**. |
| [#19](https://github.com/nateherkai/token-dashboard/pull/19) | `Skills: Track 'command' usage` | wolff-singleton | Stores Claude Code's native `attributionSkill` field on every assistant message into a new `messages.attribution_skill` column, and splits the Skills route into "You ran" (distinct sessions where you typed a slash command) vs. "Claude invoked" (Skill tool calls Claude emitted itself, typically from Task/Agent subagents). The page is renamed to "Skills & Commands" and gains two KPI tiles + two table columns matching. Includes a fork-specific de-dupe filter: the merged `skill_breakdown` query's `tool_inv` CTE adds `AND EXISTS (… messages WHERE uuid=tc.message_uuid AND type='assistant')` so the synthesised slash-command rows that PR #12 writes on user-type messages don't double-count against the new `attribution_skill` totals. The fork's existing budget / p50 / p95 / total-$ / total-inc-subagents columns from PR #12 are preserved. Migration clears `messages` / `tool_calls` / `files` / `summary_meta` so the next scan replays JSONLs and rebuilds summaries from scratch. |

## Deliberately skipped upstream PRs

| PR | Reason |
|---|---|
| [#4](https://github.com/nateherkai/token-dashboard/pull/4) | Adds Claude Code remote session-init hooks and tracks `.claude/settings.json`. Useful only for Claude Code remote (web) sessions, which this fork's owner does not use; would also start tracking project-local Claude settings that should stay private. |
| [#14](https://github.com/nateherkai/token-dashboard/pull/14) | Subset of #15 (pricing fix only); #15 includes the same fix plus the team plan additions. |
| [#17](https://github.com/nateherkai/token-dashboard/pull/17) | A less polished alternative to #2 with the same purpose (scan progress callback). PR #17 was reopened on 2026-05-12 with the callback signature `(scanned, total, totals)` — strictly less information than this fork's PR #2 signature `(index, total, path, totals)`. Integrating it would degrade the CLI progress printer. |
| [#6](https://github.com/nateherkai/token-dashboard/pull/6), [#11](https://github.com/nateherkai/token-dashboard/pull/11) | Both fully contained within #12. |

## Differences from upstream

- The Skills route is renamed **Skills & Commands** and splits invocations into two distinct counts: "You ran" (distinct sessions where you typed `/slash-command`, tracked via Claude Code's native `attributionSkill` field on assistant turns) and "Claude invoked" (real `Skill` tool calls Claude emitted, typically from `Task`/`Agent`-dispatched subagents). Both sources are de-duplicated against each other so the same slash command never inflates both columns.
- Skills tab shows declared output-token budget vs measured p50 / p95, and flags skills that exceed budget by more than 20%.
- Overview-style endpoints (`/api/overview`, `/api/projects`, `/api/sessions`, etc.) read from materialised summary tables, so the dashboard stays responsive at hundreds of thousands of messages.
- Settings tab can switch the `.claude` folder at runtime.
- Prompts tab can export the current view to CSV or Markdown.
- Scanner uses batched `executemany()` inserts and a single temp-table-driven snapshot eviction pass per file (PR #18); a full rescan of ~600 files / 60k+ messages completes in well under 10 seconds on commodity hardware.
- One-time CLI helpers: `python cli.py rescan-agent-targets` (back-fill `Agent` rows that were ingested before the `Task→Agent` rename) and `python cli.py rescan-slash-commands` (synthesise `Skill` rows from historical slash-command user messages — kept as a fallback for DBs that pre-date the `attribution_skill` column).
- The optional RTK savings tab probes `~/.local/bin/rtk` at request time and shows install instructions when absent; on Windows it always shows the install state because that path does not exist.

## Local conventions for this fork's owner

- `start_dashboard.bat` (a Windows launcher that wraps `python cli.py dashboard`) is intentionally `.gitignore`d. It is per-user and should not be redistributed.
- The default port is still `8080`; override with `PORT=9000 python cli.py dashboard` if you run multiple dashboards.

## Pulling further upstream changes

If upstream resumes and you want to pull additional changes:

```bash
git fetch upstream
git merge upstream/main
# resolve conflicts; this fork has substantial changes in scanner.py / server.py / db.py
python -m unittest discover tests
```

The `upstream` remote's push URL is intentionally set to `DISABLED` to prevent accidental pushes to the upstream repository.
