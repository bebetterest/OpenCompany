from __future__ import annotations
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.models import EventRecord
from opencompany.storage import Storage


class StorageTests(unittest.TestCase):
    def test_migration_backfills_missing_skill_columns_with_defaults(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "opencompany.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        project_dir TEXT NOT NULL,
                        task TEXT NOT NULL,
                        locale TEXT NOT NULL,
                        root_agent_id TEXT NOT NULL,
                        workspace_mode TEXT NOT NULL DEFAULT 'staged',
                        status TEXT NOT NULL,
                        status_reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        loop_index INTEGER NOT NULL DEFAULT 0,
                        final_summary TEXT,
                        completion_state TEXT,
                        follow_up_needed INTEGER NOT NULL DEFAULT 0,
                        config_snapshot_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        id, project_dir, task, locale, root_agent_id, workspace_mode, status,
                        status_reason, created_at, updated_at, loop_index, final_summary,
                        completion_state, follow_up_needed, config_snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "session-skills-legacy",
                        "/tmp/project",
                        "legacy task",
                        "en",
                        "agent-root",
                        "direct",
                        "completed",
                        None,
                        "2026-03-14T00:00:00+00:00",
                        "2026-03-14T00:00:00+00:00",
                        0,
                        None,
                        "completed",
                        0,
                        "{}",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            storage = Storage(db_path)
            row = storage.load_session("session-skills-legacy")

            assert row is not None
            self.assertEqual(row["enabled_skill_ids_json"], "[]")
            self.assertEqual(row["skill_bundle_root"], "")
            self.assertEqual(row["skills_state_json"], "{}")

    def test_migration_backfills_missing_workspace_mode_to_staged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "opencompany.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        project_dir TEXT NOT NULL,
                        task TEXT NOT NULL,
                        locale TEXT NOT NULL,
                        root_agent_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        final_summary TEXT,
                        loop_index INTEGER NOT NULL DEFAULT 0,
                        completion_state TEXT,
                        root_loop_hard_cap_hit INTEGER NOT NULL DEFAULT 0,
                        config_snapshot_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        id, project_dir, task, locale, root_agent_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "session-legacy",
                        "/tmp/project",
                        "legacy task",
                        "en",
                        "agent-root",
                        "completed",
                        "2026-03-14T00:00:00+00:00",
                        "2026-03-14T00:00:00+00:00",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            storage = Storage(db_path)
            row = storage.load_session("session-legacy")

            assert row is not None
            self.assertEqual(row["workspace_mode"], "staged")

    def test_tool_run_timeline_projection_is_idempotent_and_returns_tail_in_order(self) -> None:
        with TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "opencompany.db")

            storage.append_tool_run_timeline_event(
                source_event_id=2,
                session_id="session-1",
                tool_run_id="toolrun-1",
                timestamp="2026-03-18T10:00:02Z",
                event_type="tool_run_submitted",
                phase="tool",
                agent_id="agent-root",
                payload={"tool_run_id": "toolrun-1", "status": "running"},
            )
            storage.append_tool_run_timeline_event(
                source_event_id=1,
                session_id="session-1",
                tool_run_id="toolrun-1",
                timestamp="2026-03-18T10:00:01Z",
                event_type="tool_call_started",
                phase="tool",
                agent_id="agent-root",
                payload={"tool_run_id": "toolrun-1"},
            )
            storage.append_tool_run_timeline_event(
                source_event_id=2,
                session_id="session-1",
                tool_run_id="toolrun-1",
                timestamp="2026-03-18T10:00:02Z",
                event_type="tool_run_submitted",
                phase="tool",
                agent_id="agent-root",
                payload={"tool_run_id": "toolrun-1", "status": "running"},
            )
            storage.append_tool_run_timeline_event(
                source_event_id=3,
                session_id="session-1",
                tool_run_id="toolrun-1",
                timestamp="2026-03-18T10:00:03Z",
                event_type="tool_run_updated",
                phase="tool",
                agent_id="agent-root",
                payload={"tool_run_id": "toolrun-1", "status": "completed"},
            )

            timeline = storage.load_tool_run_timeline(
                session_id="session-1",
                tool_run_id="toolrun-1",
                limit=2,
            )

            self.assertEqual(
                [entry["event_type"] for entry in timeline],
                ["tool_run_submitted", "tool_run_updated"],
            )
            self.assertEqual(
                [entry["source_event_id"] for entry in timeline],
                [2, 3],
            )

    def test_append_event_projects_tool_run_timeline_from_top_level_tool_run_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "opencompany.db")

            storage.append_event(
                EventRecord(
                    timestamp="2026-03-18T10:00:00Z",
                    session_id="session-1",
                    agent_id="agent-root",
                    parent_agent_id=None,
                    event_type="tool_call_started",
                    phase="tool",
                    payload={
                        "tool_run_id": "toolrun-1",
                        "action": {"type": "shell", "_tool_call_id": "call-1"},
                    },
                    workspace_id="root",
                    checkpoint_seq=0,
                )
            )

            timeline = storage.load_tool_run_timeline(
                session_id="session-1",
                tool_run_id="toolrun-1",
                limit=10,
            )

            self.assertEqual(len(timeline), 1)
            self.assertEqual(timeline[0]["event_type"], "tool_call_started")
            self.assertEqual(timeline[0]["payload"]["tool_run_id"], "toolrun-1")

    def test_tool_run_timeline_backfill_marker_round_trip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "opencompany.db")

            self.assertFalse(storage.has_tool_run_timeline_backfill("session-1"))
            storage.mark_tool_run_timeline_backfilled(
                "session-1",
                "2026-03-18T10:00:00Z",
            )
            self.assertTrue(storage.has_tool_run_timeline_backfill("session-1"))
