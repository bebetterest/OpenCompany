from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.logging import AgentMessageLogger
from opencompany.models import AgentNode, AgentRole


def _agent(*, session_id: str, agent_id: str, name: str) -> AgentNode:
    return AgentNode(
        id=agent_id,
        session_id=session_id,
        name=name,
        role=AgentRole.WORKER,
        instruction="demo",
        workspace_id=f"workspace-{agent_id}",
    )


class AgentMessageCursorTests(unittest.TestCase):
    def test_append_persists_step_count(self) -> None:
        with TemporaryDirectory() as temp_dir:
            logger = AgentMessageLogger(Path(temp_dir))
            agent_a = _agent(session_id="session-1", agent_id="agent-a", name="A")
            agent_a.step_count = 7

            logger.append(agent_a, {"role": "user", "content": "task"})
            records = logger.read(agent_a.id)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["step_count"], 7)

    def test_list_records_supports_incremental_cursor(self) -> None:
        with TemporaryDirectory() as temp_dir:
            logger = AgentMessageLogger(Path(temp_dir))
            agent_a = _agent(session_id="session-1", agent_id="agent-a", name="A")
            agent_b = _agent(session_id="session-1", agent_id="agent-b", name="B")

            logger.append(agent_a, {"role": "user", "content": "task"})
            logger.append(agent_a, {"role": "assistant", "content": '{"actions":[{"type": "list_agent_runs"}]}'})
            logger.append(agent_b, {"role": "user", "content": "inspect"})

            first_page = logger.list_records(limit=2)
            self.assertEqual(len(first_page["messages"]), 2)
            self.assertTrue(first_page["has_more"])
            self.assertTrue(first_page["next_cursor"])

            second_page = logger.list_records(cursor=first_page["next_cursor"], limit=2)
            self.assertEqual(len(second_page["messages"]), 1)
            self.assertFalse(second_page["has_more"])
            self.assertTrue(second_page["next_cursor"])

            third_page = logger.list_records(cursor=second_page["next_cursor"], limit=2)
            self.assertEqual(third_page["messages"], [])
            self.assertFalse(third_page["has_more"])

    def test_list_records_supports_agent_filter_and_tail(self) -> None:
        with TemporaryDirectory() as temp_dir:
            logger = AgentMessageLogger(Path(temp_dir))
            agent_a = _agent(session_id="session-1", agent_id="agent-a", name="A")
            agent_b = _agent(session_id="session-1", agent_id="agent-b", name="B")

            logger.append(agent_a, {"role": "user", "content": "a-1"})
            logger.append(agent_b, {"role": "user", "content": "b-1"})
            logger.append(agent_a, {"role": "assistant", "content": "a-2"})
            logger.append(agent_b, {"role": "assistant", "content": "b-2"})

            agent_only = logger.list_records(agent_id="agent-a", limit=10)
            self.assertEqual(
                [record["agent_id"] for record in agent_only["messages"]],
                ["agent-a", "agent-a"],
            )

            tail_page = logger.list_records(tail=2, limit=10)
            self.assertEqual(len(tail_page["messages"]), 2)
            self.assertFalse(tail_page["has_more"])

    def test_invalid_cursor_falls_back_to_full_list(self) -> None:
        with TemporaryDirectory() as temp_dir:
            logger = AgentMessageLogger(Path(temp_dir))
            agent_a = _agent(session_id="session-1", agent_id="agent-a", name="A")
            logger.append(agent_a, {"role": "user", "content": "a-1"})

            page = logger.list_records(cursor="not-a-cursor", limit=10)
            self.assertEqual(len(page["messages"]), 1)
            self.assertFalse(page["has_more"])

    def test_tail_and_before_merge_globally_without_per_agent_window_reads(self) -> None:
        with TemporaryDirectory() as temp_dir:
            logger = AgentMessageLogger(Path(temp_dir))
            agent_a = _agent(session_id="session-1", agent_id="agent-a", name="A")
            agent_b = _agent(session_id="session-1", agent_id="agent-b", name="B")
            agent_c = _agent(session_id="session-1", agent_id="agent-c", name="C")

            logger.append(
                agent_a,
                {"role": "user", "content": "a-1"},
                {"timestamp": "2026-03-18T10:00:00Z"},
            )
            logger.append(
                agent_b,
                {"role": "user", "content": "b-1"},
                {"timestamp": "2026-03-18T10:00:01Z"},
            )
            logger.append(
                agent_c,
                {"role": "user", "content": "c-1"},
                {"timestamp": "2026-03-18T10:00:02Z"},
            )
            logger.append(
                agent_a,
                {"role": "assistant", "content": "a-2"},
                {"timestamp": "2026-03-18T10:00:03Z"},
            )

            original_read_range = logger._read_range
            spans: list[int] = []

            def _recording_read_range(agent_id: str, start_index: int, end_index: int | None):  # type: ignore[no-untyped-def]
                upper_bound = start_index if end_index is None else end_index
                spans.append(max(0, int(upper_bound) - int(start_index)))
                return original_read_range(agent_id, start_index, end_index)

            logger._read_range = _recording_read_range  # type: ignore[method-assign]

            tail_page = logger.list_records(tail=2, limit=10)
            self.assertEqual(
                [record["message"]["content"] for record in tail_page["messages"]],
                ["c-1", "a-2"],
            )
            self.assertTrue(tail_page["before_cursor"])
            before_page = logger.list_records(before=tail_page["before_cursor"], limit=2)
            self.assertEqual(
                [record["message"]["content"] for record in before_page["messages"]],
                ["a-1", "b-1"],
            )
            self.assertTrue(not spans or all(span <= 1 for span in spans))
