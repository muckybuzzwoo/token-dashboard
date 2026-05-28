import json
import os
import unittest
from token_dashboard.scanner import parse_record

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class ParseRecordTests(unittest.TestCase):
    def test_parses_assistant_usage(self):
        msg, tools = parse_record(_load("simple_assistant.json"), project_slug="proj-x")
        self.assertEqual(msg["uuid"], "msg-1")
        self.assertEqual(msg["session_id"], "sess-1")
        self.assertEqual(msg["project_slug"], "proj-x")
        self.assertEqual(msg["model"], "claude-opus-4-7")
        self.assertEqual(msg["input_tokens"], 10)
        self.assertEqual(msg["output_tokens"], 5)
        self.assertEqual(msg["cache_read_tokens"], 100)
        self.assertEqual(msg["cache_create_5m_tokens"], 30)
        self.assertEqual(msg["cache_create_1h_tokens"], 20)
        self.assertEqual(msg["is_sidechain"], 0)
        self.assertIsNone(msg["agent_id"])
        self.assertEqual(tools, [])


class ToolExtractionTests(unittest.TestCase):
    def test_extracts_tool_uses(self):
        rec = _load("tool_use_assistant.json")
        msg, tools = parse_record(rec, project_slug="p")
        self.assertEqual(len(tools), 2)
        names = [t["tool_name"] for t in tools]
        self.assertEqual(names, ["Read", "Bash"])
        self.assertEqual(tools[0]["target"], "C:/proj/foo.py")
        self.assertEqual(tools[1]["target"], "npm run lint")
        self.assertIsNotNone(msg["tool_calls_json"])
        parsed = json.loads(msg["tool_calls_json"])
        self.assertEqual(parsed[0]["name"], "Read")
        self.assertEqual(parsed[1]["target"], "npm run lint")

    def test_tool_use_id_populated_on_tool_use(self):
        """The `id` from each tool_use block must be stored on the tool_call so
        result_tokens can be joined back to the originating command."""
        rec = {
            "type": "assistant", "uuid": "u", "sessionId": "s", "timestamp": "t",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {"type": "tool_use", "id": "toolu_abc", "name": "Bash",
                     "input": {"command": "find / -name foo"}},
                ],
            },
        }
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(tools[0]["tool_use_id"], "toolu_abc")

    def test_tool_result_carries_tool_use_id(self):
        """The matching _tool_result row must also expose the tool_use_id so a
        join with the original tool_use row is possible."""
        rec = {
            "type": "user", "uuid": "u2", "sessionId": "s",
            "timestamp": "t", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_xyz",
                 "content": "x" * 4000, "is_error": False}
            ]},
        }
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(tools[0]["tool_name"], "_tool_result")
        self.assertEqual(tools[0]["tool_use_id"], "toolu_xyz")
        self.assertEqual(tools[0]["target"], "toolu_xyz")

    def test_agent_and_task_both_populate_target(self):
        """Claude Code renamed Task → Agent; both must resolve subagent_type as target."""
        rec = {
            "type": "assistant", "uuid": "u", "sessionId": "s", "timestamp": "t",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Agent",
                     "input": {"subagent_type": "software-architect", "description": "x"}},
                    {"type": "tool_use", "id": "t2", "name": "Task",
                     "input": {"subagent_type": "researcher", "description": "y"}},
                ],
            },
        }
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(len(tools), 2)
        by_name = {t["tool_name"]: t for t in tools}
        self.assertEqual(by_name["Agent"]["target"], "software-architect")
        self.assertEqual(by_name["Task"]["target"], "researcher")


class SidechainTests(unittest.TestCase):
    def test_is_sidechain_flag_propagates(self):
        rec = {
            "type": "assistant", "uuid": "u", "sessionId": "s",
            "timestamp": "t", "isSidechain": True, "agentId": "agent-explore-1",
            "message": {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 1, "output_tokens": 1}},
        }
        msg, _ = parse_record(rec, project_slug="p")
        self.assertEqual(msg["is_sidechain"], 1)
        self.assertEqual(msg["agent_id"], "agent-explore-1")

    def test_tool_result_estimates_tokens(self):
        rec = {
            "type": "user", "uuid": "u2", "sessionId": "s",
            "timestamp": "t", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "x" * 4000, "is_error": False}
            ]},
        }
        msg, tools = parse_record(rec, project_slug="p")
        self.assertEqual(msg["type"], "user")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_name"], "_tool_result")
        self.assertAlmostEqual(tools[0]["result_tokens"], 1000, delta=10)


class SlashCommandExtractionTests(unittest.TestCase):
    """User-typed slash commands (`/foo`) must synthesize a Skill tool_call.

    Claude Code logs them as a user-role record whose content is a string
    containing `<command-name>/<slug></command-name>`. Two observed orderings
    — `<command-name>` first or `<command-message>` first — must both match.
    """

    def _user_record(self, content):
        return {
            "type":        "user",
            "uuid":        "u-cmd",
            "sessionId":   "s1",
            "timestamp":   "2026-04-24T07:12:56Z",
            "isSidechain": False,
            "message":     {"role": "user", "content": content},
        }

    def test_slash_command_name_first(self):
        rec = self._user_record(
            "<command-name>/demo-cmd</command-name>\n"
            "<command-message>demo-cmd</command-message>\n"
            "<command-args></command-args>"
        )
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_name"], "Skill")
        self.assertEqual(tools[0]["target"], "demo-cmd")
        self.assertEqual(tools[0]["timestamp"], "2026-04-24T07:12:56Z")

    def test_slash_command_message_first(self):
        rec = self._user_record(
            "<command-message>demo-cmd</command-message>\n"
            "<command-name>/demo-cmd</command-name>"
        )
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["target"], "demo-cmd")

    def test_plugin_namespaced_slug_preserves_colon(self):
        rec = self._user_record("<command-name>/codex:review</command-name>")
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(tools[0]["target"], "codex:review")

    def test_list_content_with_text_blocks(self):
        rec = self._user_record([
            {"type": "text", "text": "<command-name>/demo-skill</command-name>"},
        ])
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(tools[0]["target"], "demo-skill")

    def test_non_user_record_ignored(self):
        rec = {
            "type": "assistant", "uuid": "a1", "sessionId": "s1",
            "timestamp": "t", "isSidechain": False,
            "message": {"content": [{"type": "text",
                                     "text": "<command-name>/foo</command-name>"}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}},
        }
        _, tools = parse_record(rec, project_slug="p")
        # Assistant text doesn't count as a slash invocation.
        self.assertEqual([t["tool_name"] for t in tools], [])

    def test_ordinary_user_message_yields_no_skill_row(self):
        rec = self._user_record("just a normal question about the code")
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(tools, [])

    def test_malformed_slug_rejected(self):
        rec = self._user_record("<command-name>/not a slug</command-name>")
        _, tools = parse_record(rec, project_slug="p")
        self.assertEqual(tools, [])


if __name__ == "__main__":
    unittest.main()
