from __future__ import annotations

import unittest

from opencompany.models import AgentRole
from opencompany.tools.runtime import (
    decode_offset_cursor,
    decode_steer_run_cursor,
    decode_tool_run_cursor,
    encode_offset_cursor,
    encode_steer_run_cursor,
    encode_tool_run_cursor,
    parse_steer_run_status_filters,
    parse_tool_run_status_filters,
    steer_run_metrics,
    next_steer_run_cursor,
    tool_run_duration_ms,
    tool_run_metrics,
    validate_compress_context_action,
    validate_finish_action,
    validate_wait_run_action,
    validate_wait_time_action,
)


class ToolRuntimeHelpersTests(unittest.TestCase):
    def test_tool_run_cursor_round_trip(self) -> None:
        encoded = encode_tool_run_cursor("2026-03-11T10:00:00Z", "toolrun-abc123")
        self.assertEqual(
            decode_tool_run_cursor(encoded),
            ("2026-03-11T10:00:00Z", "toolrun-abc123"),
        )

    def test_invalid_cursor_returns_none(self) -> None:
        self.assertIsNone(decode_tool_run_cursor("not-a-valid-cursor"))
        self.assertIsNone(decode_tool_run_cursor(""))
        self.assertIsNone(decode_tool_run_cursor(None))

    def test_offset_cursor_round_trip(self) -> None:
        encoded = encode_offset_cursor(25)
        self.assertEqual(decode_offset_cursor(encoded), 25)

    def test_offset_cursor_defaults_and_invalid_values(self) -> None:
        self.assertEqual(decode_offset_cursor(None), 0)
        self.assertEqual(decode_offset_cursor("", default=7), 7)
        self.assertIsNone(decode_offset_cursor("not-a-valid-cursor"))

    def test_root_finish_rejects_next_recommendation(self) -> None:
        error = validate_finish_action(
            AgentRole.ROOT,
            {
                "type": "finish",
                "status": "completed",
                "summary": "done",
                "next_recommendation": "continue",
            },
        )
        self.assertIn("'next_recommendation'", str(error))

    def test_finish_rejects_unsupported_field(self) -> None:
        error = validate_finish_action(
            AgentRole.WORKER,
            {
                "type": "finish",
                "status": "completed",
                "summary": "done",
                "unexpected": True,
            },
        )
        self.assertIn("'unexpected'", str(error))

    def test_wait_time_validation_rejects_unsupported_fields_and_accepts_seconds(self) -> None:
        error = validate_wait_time_action({"type": "wait_time", "seconds": 10, "unexpected": False})
        self.assertIn("unsupported field", str(error))
        self.assertIsNone(validate_wait_time_action({"type": "wait_time", "seconds": 10}))
        self.assertIn(">= 10", str(validate_wait_time_action({"type": "wait_time", "seconds": 9.99})))
        self.assertIn("<= 60", str(validate_wait_time_action({"type": "wait_time", "seconds": 60.01})))
        self.assertIsNone(
            validate_wait_time_action(
                {"type": "wait_time", "seconds": 7},
                minimum_seconds=3,
                maximum_seconds=7,
            )
        )
        self.assertIn(
            ">= 3",
            str(
                validate_wait_time_action(
                    {"type": "wait_time", "seconds": 2.9},
                    minimum_seconds=3,
                    maximum_seconds=7,
                )
            ),
        )
        self.assertIn(
            "<= 7",
            str(
                validate_wait_time_action(
                    {"type": "wait_time", "seconds": 7.1},
                    minimum_seconds=3,
                    maximum_seconds=7,
                )
            ),
        )

    def test_wait_run_validation_requires_xor(self) -> None:
        self.assertIn(
            "exactly one",
            str(validate_wait_run_action({"type": "wait_run", "tool_run_id": "toolrun-1", "agent_id": "agent-1"})),
        )
        self.assertIsNone(validate_wait_run_action({"type": "wait_run", "tool_run_id": "toolrun-1"}))
        self.assertIsNone(validate_wait_run_action({"type": "wait_run", "agent_id": "agent-1"}))

    def test_compress_context_validation_rejects_extra_fields(self) -> None:
        self.assertIsNone(validate_compress_context_action({"type": "compress_context"}))
        error = validate_compress_context_action(
            {"type": "compress_context", "unexpected": True}
        )
        self.assertIn("unsupported field", str(error))

    def test_parse_tool_run_status_filters_validates_unknown_values(self) -> None:
        statuses, invalid = parse_tool_run_status_filters(["running", "bad", "COMPLETED", "ABANDONED"])
        self.assertEqual(statuses, ["running", "completed", "abandoned"])
        self.assertEqual(invalid, ["bad"])

    def test_steer_run_cursor_round_trip(self) -> None:
        encoded = encode_steer_run_cursor("2026-03-11T10:00:00Z", "steerrun-abc123")
        self.assertEqual(
            decode_steer_run_cursor(encoded),
            ("2026-03-11T10:00:00Z", "steerrun-abc123"),
        )
        cursor = next_steer_run_cursor(
            [
                {
                    "id": "steerrun-abc123",
                    "created_at": "2026-03-11T10:00:00Z",
                }
            ],
            limit=1,
        )
        self.assertEqual(cursor, encoded)

    def test_parse_steer_run_status_filters_validates_unknown_values(self) -> None:
        statuses, invalid = parse_steer_run_status_filters(["waiting", "bad", "COMPLETED"])
        self.assertEqual(statuses, ["waiting", "completed"])
        self.assertEqual(invalid, ["bad"])

    def test_worker_partial_finish_requires_next_recommendation(self) -> None:
        error = validate_finish_action(
            AgentRole.WORKER,
            {
                "type": "finish",
                "status": "partial",
                "summary": "partial result",
            },
        )
        self.assertIn("requires a non-empty 'next_recommendation'", str(error))

    def test_tool_run_duration_prefers_started_and_completed_timestamps(self) -> None:
        duration = tool_run_duration_ms(
            {
                "status": "completed",
                "created_at": "2026-03-11T10:00:00Z",
                "started_at": "2026-03-11T10:00:01Z",
                "completed_at": "2026-03-11T10:00:03.500000Z",
            }
        )
        self.assertEqual(duration, 2500)

    def test_tool_run_metrics_includes_distribution_and_failure_rate(self) -> None:
        metrics = tool_run_metrics(
            [
                {
                    "id": "toolrun-1",
                    "session_id": "session-1",
                    "agent_id": "agent-a",
                    "tool_name": "list_agent_runs",
                    "status": "completed",
                    "created_at": "2026-03-11T10:00:00Z",
                    "started_at": "2026-03-11T10:00:00Z",
                    "completed_at": "2026-03-11T10:00:01Z",
                },
                {
                    "id": "toolrun-2",
                    "session_id": "session-1",
                    "agent_id": "agent-a",
                    "tool_name": "shell",
                    "status": "failed",
                    "created_at": "2026-03-11T10:00:02Z",
                    "started_at": "2026-03-11T10:00:02Z",
                    "completed_at": "2026-03-11T10:00:04Z",
                },
                {
                    "id": "toolrun-3",
                    "session_id": "session-1",
                    "agent_id": "agent-b",
                    "tool_name": "spawn_agent",
                    "status": "cancelled",
                    "created_at": "2026-03-11T10:00:05Z",
                    "started_at": "2026-03-11T10:00:05Z",
                    "completed_at": "2026-03-11T10:00:06Z",
                },
            ],
            session_id="session-1",
            generated_at="2026-03-11T10:00:10Z",
        )
        self.assertEqual(metrics["total_runs"], 3)
        self.assertEqual(metrics["terminal_runs"], 3)
        self.assertEqual(metrics["failed_runs"], 1)
        self.assertEqual(metrics["cancelled_runs"], 1)
        self.assertAlmostEqual(metrics["failure_rate"], 1 / 3, places=6)
        self.assertAlmostEqual(metrics["failure_or_cancel_rate"], 2 / 3, places=6)
        self.assertEqual(metrics["duration_ms"]["count"], 3)
        self.assertEqual(metrics["duration_ms"]["p50"], 1000)
        self.assertTrue(metrics["by_tool"])
        self.assertTrue(metrics["by_agent"])

    def test_steer_run_metrics_include_status_counts_and_agent_breakdown(self) -> None:
        metrics = steer_run_metrics(
            [
                {
                    "id": "steerrun-1",
                    "session_id": "session-1",
                    "agent_id": "agent-a",
                    "status": "waiting",
                },
                {
                    "id": "steerrun-2",
                    "session_id": "session-1",
                    "agent_id": "agent-a",
                    "status": "completed",
                },
                {
                    "id": "steerrun-3",
                    "session_id": "session-1",
                    "agent_id": "agent-b",
                    "status": "cancelled",
                },
            ],
            session_id="session-1",
            generated_at="2026-03-11T10:00:10Z",
        )
        self.assertEqual(metrics["total_runs"], 3)
        self.assertEqual(metrics["waiting_runs"], 1)
        self.assertEqual(metrics["completed_runs"], 1)
        self.assertEqual(metrics["cancelled_runs"], 1)
        self.assertEqual(metrics["status_counts"]["waiting"], 1)
        self.assertEqual(metrics["status_counts"]["completed"], 1)
        self.assertEqual(metrics["status_counts"]["cancelled"], 1)
        by_agent = metrics["by_agent"]
        self.assertIsInstance(by_agent, list)
        self.assertEqual(len(by_agent), 2)
