from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.storage import Storage


class StorageTests(unittest.TestCase):
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
