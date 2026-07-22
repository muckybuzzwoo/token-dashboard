import os
import tempfile
import unittest

from token_dashboard.db import (
    init_db, connect,
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    session_model_tokens,
    daily_token_breakdown, model_breakdown, project_name_for,
    skill_breakdown, rebuild_summaries,
)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "q.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text, prompt_chars)
            VALUES
              ('u1',NULL,'s1','projA','user','2026-04-10T00:00:00Z',NULL,0,0,0,0,0,'big prompt',10),
              ('a1','u1','s1','projA','assistant','2026-04-10T00:00:01Z','claude-opus-4-7',100,200,300,0,0,NULL,NULL),
              ('u2',NULL,'s2','projB','user','2026-04-11T00:00:00Z',NULL,0,0,0,0,0,'small',5),
              ('a2','u2','s2','projB','assistant','2026-04-11T00:00:01Z','claude-sonnet-4-6',5,5,0,0,0,NULL,NULL);
            INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error)
            VALUES ('a1','s1','projA','Read','foo.py','2026-04-10T00:00:01Z',0),
                   ('a1','s1','projA','Bash','npm test','2026-04-10T00:00:01Z',0);
            """)
            c.commit()

    def test_overview_totals(self):
        t = overview_totals(self.db, since=None, until=None)
        self.assertEqual(t["sessions"], 2)
        self.assertEqual(t["turns"], 2)
        self.assertEqual(t["input_tokens"], 105)
        self.assertEqual(t["output_tokens"], 205)

    def test_turns_exclude_tool_result_user_records(self):
        # Claude Code writes tool results back as type='user' records (no
        # prompt_text). They must NOT inflate the "turns" metric (issue #25 #3).
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, prompt_text, prompt_chars)
            VALUES
              ('tr1','a1','s1','projA','user','2026-04-10T00:00:02Z',NULL,0,0,NULL,NULL),
              ('tr2','a1','s1','projA','user','2026-04-10T00:00:03Z',NULL,0,0,NULL,NULL),
              ('tr3','a2','s2','projB','user','2026-04-11T00:00:02Z',NULL,0,0,NULL,NULL);
            """)
            c.commit()
        # Two real typed prompts (u1, u2); the three tool-result rows must not count.
        self.assertEqual(overview_totals(self.db)["turns"], 2)
        by_proj = {r["project_slug"]: r for r in project_summary(self.db)}
        self.assertEqual(by_proj["projA"]["turns"], 1)
        self.assertEqual(by_proj["projB"]["turns"], 1)
        by_sess = {r["session_id"]: r for r in recent_sessions(self.db)}
        self.assertEqual(by_sess["s1"]["turns"], 1)
        self.assertEqual(by_sess["s2"]["turns"], 1)

    def test_turns_exclude_tool_result_user_records_summary(self):
        # Same as above but through the materialised summary tables.
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, prompt_text, prompt_chars)
            VALUES
              ('tr1','a1','s1','projA','user','2026-04-10T00:00:02Z',NULL,0,0,NULL,NULL),
              ('tr2','a1','s1','projA','user','2026-04-10T00:00:03Z',NULL,0,0,NULL,NULL);
            """)
            c.commit()
        rebuild_summaries(self.db)
        self.assertEqual(overview_totals(self.db)["turns"], 2)
        by_sess = {r["session_id"]: r for r in recent_sessions(self.db)}
        self.assertEqual(by_sess["s1"]["turns"], 1)

    def test_expensive_prompts_orders_by_tokens(self):
        rows = expensive_prompts(self.db, limit=10)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["prompt_text"], "big prompt")

    def test_expensive_prompts_sort_recent(self):
        rows = expensive_prompts(self.db, limit=10, sort="recent")
        self.assertEqual(rows[0]["prompt_text"], "small")
        self.assertEqual(rows[1]["prompt_text"], "big prompt")

    def test_expensive_prompts_links_across_attachment(self):
        # Regression: newer Claude Code interposes an `attachment` record between
        # the typed prompt and the assistant turn, so assistant.parent_uuid no
        # longer points at the prompt. Linkage must still work via session+time.
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text, prompt_chars, prompt_id, is_sidechain)
            VALUES
              ('u3',NULL,'s3','projC','user','2026-05-01T00:00:00Z',NULL,0,0,0,0,0,'new-format prompt',17,'p3',0),
              ('att3','u3','s3','projC','attachment','2026-05-01T00:00:00.3Z',NULL,0,0,0,0,0,NULL,NULL,'p3',0),
              ('a3','att3','s3','projC','assistant','2026-05-01T00:00:01Z','claude-opus-4-8',10,20,5,0,0,NULL,NULL,NULL,0);
            """)
            c.commit()
        row = next(r for r in expensive_prompts(self.db, sort="recent")
                   if r["prompt_text"] == "new-format prompt")
        self.assertEqual(row["model"], "claude-opus-4-8")
        self.assertEqual(row["billable_tokens"], 30)   # 10 + 20, despite no parent_uuid link
        self.assertEqual(row["cache_read_tokens"], 5)

    def test_expensive_prompts_collapses_injected_same_prompt_id(self):
        # Injected list[text] user messages (skill base dirs, system reminders)
        # carry prompt_text and share the typed prompt's prompt_id. They must
        # collapse to the earliest row, not appear as separate prompts.
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text, prompt_chars, prompt_id, is_sidechain)
            VALUES
              ('u4',NULL,'s4','projD','user','2026-05-02T00:00:00Z',NULL,0,0,0,0,0,'real typed prompt',17,'p4',0),
              ('u4b','u4','s4','projD','user','2026-05-02T00:00:05Z',NULL,0,0,0,0,0,'Base directory for this skill: ...',34,'p4',0),
              ('a4','u4b','s4','projD','assistant','2026-05-02T00:00:10Z','claude-opus-4-8',1,1,0,0,0,NULL,NULL,NULL,0);
            """)
            c.commit()
        texts = [r["prompt_text"] for r in expensive_prompts(self.db, sort="recent")]
        self.assertIn("real typed prompt", texts)
        self.assertNotIn("Base directory for this skill: ...", texts)

    def test_expensive_prompts_skips_null_model_first_assistant(self):
        # Regression: an API-error / synthetic assistant row (model NULL) can be
        # the FIRST assistant in a prompt's window, followed by the real response.
        # The displayed model must come from the real row, and the prompt must
        # NOT be dropped by the outer `model IS NOT NULL` filter.
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text, prompt_chars, prompt_id, is_sidechain)
            VALUES
              ('u5',NULL,'s5','projE','user','2026-05-03T00:00:00Z',NULL,0,0,0,0,0,'retry prompt',12,'p5',0),
              ('aerr','u5','s5','projE','assistant','2026-05-03T00:00:01Z',NULL,0,0,0,0,0,NULL,NULL,NULL,0),
              ('a5','aerr','s5','projE','assistant','2026-05-03T00:00:02Z','claude-opus-4-8',10,20,5,0,0,NULL,NULL,NULL,0);
            """)
            c.commit()
        row = next((r for r in expensive_prompts(self.db, sort="recent")
                    if r["prompt_text"] == "retry prompt"), None)
        self.assertIsNotNone(row, "prompt with a NULL-model first assistant was dropped")
        self.assertEqual(row["model"], "claude-opus-4-8")   # not the leading NULL-model error row
        self.assertEqual(row["billable_tokens"], 30)        # 10 + 20 (error row contributes 0)
        self.assertEqual(row["cache_read_tokens"], 5)

    def test_expensive_prompts_sums_multiple_assistants_in_window(self):
        # The full turn's cost spans every main-thread assistant message between
        # this prompt and the next (the tool loop), not just the first response.
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text, prompt_chars, prompt_id, is_sidechain)
            VALUES
              ('u6',NULL,'s6','projF','user','2026-05-04T00:00:00Z',NULL,0,0,0,0,0,'tool loop prompt',16,'p6',0),
              ('a6a','u6','s6','projF','assistant','2026-05-04T00:00:01Z','claude-opus-4-8',10,20,1,0,0,NULL,NULL,NULL,0),
              ('a6b','a6a','s6','projF','assistant','2026-05-04T00:00:02Z','claude-opus-4-8',5,7,2,0,0,NULL,NULL,NULL,0),
              ('a6c','a6b','s6','projF','assistant','2026-05-04T00:00:03Z','claude-opus-4-8',3,4,3,0,0,NULL,NULL,NULL,0);
            """)
            c.commit()
        row = next(r for r in expensive_prompts(self.db, sort="recent")
                   if r["prompt_text"] == "tool loop prompt")
        self.assertEqual(row["model"], "claude-opus-4-8")
        self.assertEqual(row["billable_tokens"], 49)   # (10+20)+(5+7)+(3+4)
        self.assertEqual(row["cache_read_tokens"], 6)   # 1+2+3

    def test_project_summary_groups(self):
        rows = project_summary(self.db)
        slugs = {r["project_slug"]: r for r in rows}
        self.assertIn("projA", slugs)
        self.assertEqual(slugs["projA"]["turns"], 1)

    def test_tool_breakdown(self):
        rows = tool_token_breakdown(self.db)
        names = {r["tool_name"]: r for r in rows}
        self.assertIn("Read", names)
        self.assertIn("Bash", names)

    def test_recent_sessions(self):
        rows = recent_sessions(self.db, limit=5)
        self.assertEqual(rows[0]["session_id"], "s2")

    def test_session_turns(self):
        rows = session_turns(self.db, "s1")
        self.assertEqual(len(rows), 2)

    def test_session_model_tokens_groups_per_session_and_model(self):
        out = session_model_tokens(self.db, ["s1", "s2"])
        self.assertEqual(set(out), {"s1", "s2"})
        s1 = out["s1"]
        self.assertEqual(len(s1), 1)
        self.assertEqual(s1[0]["model"], "claude-opus-4-7")
        self.assertEqual(s1[0]["input_tokens"], 100)
        self.assertEqual(s1[0]["output_tokens"], 200)
        self.assertEqual(s1[0]["cache_read_tokens"], 300)

    def test_session_model_tokens_splits_mixed_model_session(self):
        # A session that used two different models yields one row per model,
        # so the caller can price each correctly before summing.
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)
            VALUES
              ('a3','u1','s1','projA','assistant','2026-04-10T00:00:02Z','claude-haiku-4-5',7,9,0,0,0);
            """)
            c.commit()
        out = session_model_tokens(self.db, ["s1"])
        by_model = {r["model"]: r for r in out["s1"]}
        self.assertEqual(set(by_model), {"claude-opus-4-7", "claude-haiku-4-5"})
        self.assertEqual(by_model["claude-haiku-4-5"]["output_tokens"], 9)

    def test_session_model_tokens_empty_input(self):
        self.assertEqual(session_model_tokens(self.db, []), {})

    def test_daily_token_breakdown_groups_by_day(self):
        rows = daily_token_breakdown(self.db)
        days = {r["day"]: r for r in rows}
        self.assertIn("2026-04-10", days)
        self.assertIn("2026-04-11", days)
        self.assertEqual(days["2026-04-10"]["input_tokens"], 100)
        self.assertEqual(days["2026-04-10"]["output_tokens"], 200)
        self.assertEqual(days["2026-04-10"]["cache_read_tokens"], 300)

    def test_daily_token_breakdown_respects_since(self):
        rows = daily_token_breakdown(self.db, since="2026-04-11T00:00:00Z")
        days = [r["day"] for r in rows]
        self.assertEqual(days, ["2026-04-11"])

    def test_model_breakdown_respects_since_and_groups(self):
        rows = model_breakdown(self.db)
        models = {r["model"]: r for r in rows}
        self.assertIn("claude-opus-4-7", models)
        self.assertIn("claude-sonnet-4-6", models)
        self.assertEqual(models["claude-opus-4-7"]["input_tokens"], 100)

        filtered = model_breakdown(self.db, since="2026-04-11T00:00:00Z")
        names = [r["model"] for r in filtered]
        self.assertEqual(names, ["claude-sonnet-4-6"])


class SummaryQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "summary.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, cwd, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text)
            VALUES
              ('u1',NULL,'s1','projA','/work/projA','user','2026-04-10T00:00:00Z',NULL,0,0,0,0,0,'hi A'),
              ('a1','u1','s1','projA','/work/projA','assistant','2026-04-10T00:00:01Z','claude-opus-4-7',100,200,300,10,20,NULL),
              ('u2',NULL,'s2','projB','/work/projB','user','2026-04-11T00:00:00Z',NULL,0,0,0,0,0,'hi B'),
              ('a2','u2','s2','projB','/work/projB','assistant','2026-04-11T00:00:01Z','claude-sonnet-4-6',5,6,7,8,9,NULL);
            INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, timestamp, is_error)
            VALUES
              ('a1','s1','projA','Read','foo.py',50,'2026-04-10T00:00:01Z',0),
              ('a1','s1','projA','Bash','npm test',100,'2026-04-10T00:00:02Z',0),
              ('a2','s2','projB','Read','bar.py',25,'2026-04-11T00:00:01Z',0);
            """)
            c.commit()

    def test_overview_queries_can_read_from_summaries_without_raw_rows(self):
        rebuild_summaries(self.db)
        with connect(self.db) as c:
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM tool_calls")
            c.commit()

        totals = overview_totals(self.db)
        self.assertEqual(totals["sessions"], 2)
        self.assertEqual(totals["turns"], 2)
        self.assertEqual(totals["input_tokens"], 105)
        self.assertEqual(totals["output_tokens"], 206)
        self.assertEqual(totals["cache_create_5m_tokens"], 18)
        self.assertEqual(totals["cache_create_1h_tokens"], 29)

        projects = {r["project_slug"]: r for r in project_summary(self.db)}
        self.assertEqual(projects["projA"]["billable_tokens"], 330)
        self.assertEqual(projects["projA"]["project_name"], "projA")

        tools = {r["tool_name"]: r for r in tool_token_breakdown(self.db)}
        self.assertEqual(tools["Read"]["calls"], 2)
        self.assertEqual(tools["Read"]["result_tokens"], 75)

        daily = {r["day"]: r for r in daily_token_breakdown(self.db)}
        self.assertEqual(daily["2026-04-10"]["cache_create_tokens"], 30)

        models = {r["model"]: r for r in model_breakdown(self.db)}
        self.assertEqual(models["claude-opus-4-7"]["cache_read_tokens"], 300)

        sessions = recent_sessions(self.db, limit=1)
        self.assertEqual(sessions[0]["session_id"], "s2")
        self.assertEqual(sessions[0]["tokens"], 11)

    def test_summary_queries_respect_date_ranges(self):
        rebuild_summaries(self.db)
        with connect(self.db) as c:
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM tool_calls")
            c.commit()

        totals = overview_totals(self.db, since="2026-04-11T00:00:00Z")
        self.assertEqual(totals["sessions"], 1)
        self.assertEqual(totals["input_tokens"], 5)

        projects = project_summary(self.db, since="2026-04-11T00:00:00Z")
        self.assertEqual([r["project_slug"] for r in projects], ["projB"])

    def test_subday_since_bypasses_day_truncated_summary(self):
        # The materialised summaries bucket by whole UTC day. A rolling since
        # with a sub-day time component (e.g. the Overview 1d/2d/3d filters send
        # `new Date(...).toISOString()`) must NOT hit the day-truncated fast
        # path, or it wrongly counts the whole calendar day. Here two assistant
        # turns sit on the same UTC day, early and late; a midday `since` must
        # keep only the late one.
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text, prompt_chars)
            VALUES
              ('e1',NULL,'s3','projC','assistant','2026-05-01T02:00:00Z','claude-opus-4-7',1000,0,0,0,0,NULL,NULL),
              ('l1',NULL,'s3','projC','assistant','2026-05-01T22:00:00Z','claude-haiku-4-5',7,0,0,0,0,NULL,NULL);
            INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error)
            VALUES ('e1','s3','projC','Grep','x','2026-05-01T02:00:00Z',0),
                   ('l1','s3','projC','Grep','y','2026-05-01T22:00:00Z',0);
            """)
            c.commit()
        rebuild_summaries(self.db)
        since = "2026-05-01T12:00:00.000Z"

        # overview_totals: only the late (7-token) turn, not the full-day 1007.
        self.assertEqual(overview_totals(self.db, since=since)["input_tokens"], 7)

        # model_breakdown: the early model must be absent, only the late one.
        models = {r["model"]: r for r in model_breakdown(self.db, since=since)}
        self.assertNotIn("claude-opus-4-7", models)
        self.assertEqual(models["claude-haiku-4-5"]["input_tokens"], 7)

        # project_summary: projC counts only the late turn's tokens.
        projects = {r["project_slug"]: r for r in project_summary(self.db, since=since)}
        self.assertEqual(projects["projC"]["input_tokens"], 7)

        # tool_token_breakdown: only the late Grep call.
        tools = {r["tool_name"]: r for r in tool_token_breakdown(self.db, since=since)}
        self.assertEqual(tools["Grep"]["calls"], 1)

    def test_partial_summary_rebuild_updates_only_requested_days_and_sessions(self):
        rebuild_summaries(self.db)
        with connect(self.db) as c:
            c.execute("""
                INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp, model,
                  input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)
                VALUES ('a3','s2','projB','/work/projB','assistant','2026-04-11T00:00:02Z',
                  'claude-sonnet-4-6',20,30,40,50,60)
            """)
            c.execute("""
                INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp, model,
                  input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)
                VALUES ('a4','s3','projC','/work/projC','assistant','2026-04-12T00:00:01Z',
                  'claude-haiku-4-5',1,2,3,4,5)
            """)
            c.commit()

        rebuild_summaries(self.db, days={"2026-04-11"}, sessions={"s2"})

        daily = {r["day"]: r for r in daily_token_breakdown(self.db)}
        self.assertEqual(daily["2026-04-10"]["input_tokens"], 100)
        self.assertEqual(daily["2026-04-11"]["input_tokens"], 25)
        self.assertNotIn("2026-04-12", daily)

        sessions = {r["session_id"]: r for r in recent_sessions(self.db, limit=10)}
        self.assertEqual(sessions["s2"]["tokens"], 61)
        self.assertNotIn("s3", sessions)


class SkillBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "s.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, attribution_skill)
            VALUES
              -- Baseline: Skill tool calls on assistant messages.
              ('u1','s1','pA','user','2026-04-10T00:00:00Z',NULL),
              ('a1','s1','pA','assistant','2026-04-10T00:00:01Z',NULL),
              ('u2','s2','pA','user','2026-04-11T00:00:00Z',NULL),
              ('a2','s2','pA','assistant','2026-04-11T00:00:01Z',NULL),

              -- /polish-email in two distinct sessions (s3, s4). Multiple rows
              -- per session must collapse to a single manual_session via
              -- COUNT(DISTINCT session_id).
              ('u3','s3','pA','user','2026-04-12T00:00:00Z','polish-email'),
              ('a3','s3','pA','assistant','2026-04-12T00:00:01Z','polish-email'),
              ('u4','s4','pA','user','2026-04-13T00:00:00Z','polish-email'),

              -- /name-conversation only as attribution — no Skill tool calls.
              -- Exercises the manual_inv LEFT JOIN arm of the UNION.
              ('u5','s5','pA','user','2026-04-14T00:00:00Z','name-conversation'),

              -- brainstorming also gets a manual session in s7, to verify the
              -- COALESCE merge of a skill that has BOTH tool invocations and
              -- manual sessions.
              ('u7','s7','pA','user','2026-04-15T00:00:00Z','brainstorming'),

              -- De-dupe guard: a user-type message in s8 holds a SYNTHESISED
              -- Skill row (from the PR #12 slash-command extractor) for
              -- 'polish-email'. attribution_skill is NULL because we want
              -- this row to test the EXISTS filter in isolation — without it,
              -- the synthesised row would bump tool_invocations even though
              -- it lives on a user-type message.
              ('u8','s8','pA','user','2026-04-16T00:00:00Z',NULL);

            INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, timestamp, is_error)
            VALUES
              -- Real Skill tool_use blocks (on assistant messages).
              ('a1','s1','pA','Skill','brainstorming',NULL,'2026-04-10T00:00:01Z',0),
              ('a1','s1','pA','Skill','brainstorming',NULL,'2026-04-10T00:00:30Z',0),
              ('a2','s2','pA','Skill','create-skill',NULL,'2026-04-11T00:00:01Z',0),

              -- Synthesised Skill row on a USER message — must be filtered out
              -- by the EXISTS clause on messages.type='assistant'.
              ('u8','s8','pA','Skill','polish-email',NULL,'2026-04-16T00:00:00Z',0);
            """)
            c.commit()

    def test_groups_by_skill(self):
        # brainstorming: 2 tool invocations + 1 manual session — exercises
        # the COALESCE merge branch of the UNION.
        rows = skill_breakdown(self.db)
        by_name = {r["skill"]: r for r in rows}
        self.assertEqual(by_name["brainstorming"]["tool_invocations"], 2)
        self.assertEqual(by_name["brainstorming"]["manual_sessions"], 1)
        self.assertEqual(by_name["create-skill"]["tool_invocations"], 1)
        self.assertEqual(by_name["create-skill"]["manual_sessions"], 0)

    def test_manual_only_skill_appears(self):
        # name-conversation has no Skill tool_use blocks — only an
        # attribution_skill entry. Exercises the manual_inv LEFT JOIN tool_inv
        # WHERE t.skill IS NULL arm of the UNION.
        rows = skill_breakdown(self.db)
        by_name = {r["skill"]: r for r in rows}
        self.assertIn("name-conversation", by_name)
        self.assertEqual(by_name["name-conversation"]["manual_sessions"], 1)
        self.assertEqual(by_name["name-conversation"]["tool_invocations"], 0)

    def test_manual_sessions_distinct(self):
        # /polish-email has three attribution rows across two distinct sessions
        # (s3 has two rows, s4 has one) — must collapse to 2 via COUNT(DISTINCT).
        # s8's synthesised tool_calls row must NOT count toward tool_invocations.
        rows = skill_breakdown(self.db)
        by_name = {r["skill"]: r for r in rows}
        self.assertEqual(by_name["polish-email"]["manual_sessions"], 2)
        self.assertEqual(by_name["polish-email"]["tool_invocations"], 0)

    def test_synthesised_rows_filtered_out(self):
        # The EXISTS filter on messages.type='assistant' must exclude the
        # synthesised Skill row on u8 (a user-type message). If it leaked in,
        # tool_invocations for polish-email would be 1 instead of 0.
        rows = skill_breakdown(self.db)
        by_name = {r["skill"]: r for r in rows}
        self.assertEqual(by_name["polish-email"]["tool_invocations"], 0,
                         "synthesised slash-command rows on user messages "
                         "must not double-count against attribution_skill")

    def test_orders_by_combined_total(self):
        # brainstorming has 2 tool + 1 manual = 3; polish-email has 0 tool
        # + 2 manual = 2 — ordering uses the sum.
        rows = skill_breakdown(self.db)
        names = [r["skill"] for r in rows]
        self.assertEqual(names[0], "brainstorming")
        self.assertLess(names.index("brainstorming"), names.index("polish-email"))

    def test_respects_since(self):
        rows = skill_breakdown(self.db, since="2026-04-12T00:00:00Z")
        names = {r["skill"] for r in rows}
        # The 2026-04-10 brainstorming tool calls and 2026-04-11 create-skill
        # both drop out of the window.
        self.assertNotIn("create-skill", names)
        self.assertIn("polish-email", names)
        self.assertIn("name-conversation", names)


class ProjectNameTests(unittest.TestCase):
    def test_basename_of_posix_cwd(self):
        self.assertEqual(project_name_for("/Users/x/foo", "slug"), "foo")

    def test_basename_of_windows_cwd(self):
        self.assertEqual(
            project_name_for(r"C:\Users\alice\projects\Token Dashboard", "anything"),
            "Token Dashboard",
        )

    def test_trailing_slash_stripped(self):
        self.assertEqual(project_name_for("/a/b/c/", "slug"), "c")

    def test_fallback_uses_last_dash_segment(self):
        self.assertEqual(
            project_name_for(None, "C--Users-x-Foo-Bar"),
            "Bar",
        )

    def test_fallback_single_segment(self):
        self.assertEqual(project_name_for(None, "projA"), "projA")

    def test_empty(self):
        self.assertEqual(project_name_for(None, ""), "")

    def test_walks_up_cwd_to_project_root(self):
        # cwd is a subfolder; slug matches the parent → return the parent's basename
        self.assertEqual(
            project_name_for(
                r"C:\Users\alice\projects\MyProject\subdir",
                "C--Users-alice-projects-MyProject",
            ),
            "MyProject",
        )

    def test_walks_up_preserves_spaces(self):
        self.assertEqual(
            project_name_for(
                r"C:\Users\alice\projects\Token Dashboard\src\subdir",
                "C--Users-alice-projects-Token-Dashboard",
            ),
            "Token Dashboard",
        )


class ProjectNameInQueriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "n.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)
            VALUES
              ('u1','s1','C--Users-x-My-Repo','/Users/x/My Repo','user','2026-04-10T00:00:00Z',0,0,0,0,0),
              ('a1','s1','C--Users-x-My-Repo','/Users/x/My Repo','assistant','2026-04-10T00:00:01Z',10,20,0,0,0),
              ('u2','s2','slugOnly',NULL,'user','2026-04-11T00:00:00Z',0,0,0,0,0),
              ('a2','s2','slugOnly',NULL,'assistant','2026-04-11T00:00:01Z',5,5,0,0,0);
            """)
            c.commit()

    def test_project_summary_uses_cwd_basename(self):
        rows = project_summary(self.db)
        names = {r["project_slug"]: r["project_name"] for r in rows}
        self.assertEqual(names["C--Users-x-My-Repo"], "My Repo")
        self.assertEqual(names["slugOnly"], "slugOnly")

    def test_recent_sessions_has_project_name(self):
        rows = recent_sessions(self.db)
        by_sid = {r["session_id"]: r for r in rows}
        self.assertEqual(by_sid["s1"]["project_name"], "My Repo")
        self.assertEqual(by_sid["s2"]["project_name"], "slugOnly")


if __name__ == "__main__":
    unittest.main()
