from __future__ import annotations

import json
import unittest

from opencompany.orchestration.messages import tool_result_message


class ToolResultMessageTests(unittest.TestCase):
    def test_shell_tool_result_omits_command_field_from_tool_message(self) -> None:
        message = tool_result_message(
            {"type": "shell", "command": "pwd", "_tool_call_id": "call-shell"},
            {
                "exit_code": 0,
                "stdout": "/tmp/workspace\n",
                "stderr": "",
                "command": "pwd",
            },
        )

        self.assertEqual(message["role"], "tool")
        payload = json.loads(message["content"])
        self.assertEqual(payload["exit_code"], 0)
        self.assertNotIn("command", payload)

    def test_non_shell_tool_result_is_unchanged(self) -> None:
        message = tool_result_message(
            {"type": "list_agent_runs", "_tool_call_id": "call-list"},
            {"agent_runs": [{"id": "agent-root"}]},
        )

        self.assertEqual(message["role"], "tool")
        payload = json.loads(message["content"])
        self.assertEqual(payload, {"agent_runs": [{"id": "agent-root"}]})
