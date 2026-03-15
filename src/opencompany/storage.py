from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opencompany.models import (
    AgentNode,
    CheckpointState,
    EventRecord,
    RunSession,
    SteerRun,
    ToolRun,
)
from opencompany.status_machine import normalize_session_completion_state
from opencompany.utils import json_ready


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.connection = sqlite3.connect(str(db_path))
        self.connection.row_factory = sqlite3.Row
        self._batched_write_depth = 0
        self._initialize()

    def _initialize(self) -> None:
        cursor = self.connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
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
                loop_index INTEGER NOT NULL,
                final_summary TEXT,
                completion_state TEXT,
                follow_up_needed INTEGER NOT NULL,
                config_snapshot_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                instruction TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                parent_agent_id TEXT,
                status TEXT NOT NULL,
                status_reason TEXT,
                children_json TEXT NOT NULL,
                summary TEXT,
                next_recommendation TEXT,
                diff_artifact TEXT,
                completion_status TEXT,
                step_count INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT,
                parent_agent_id TEXT,
                event_type TEXT NOT NULL,
                phase TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                workspace_id TEXT,
                checkpoint_seq INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                state_json TEXT NOT NULL,
                PRIMARY KEY (session_id, seq)
            );

            CREATE TABLE IF NOT EXISTS pending_actions (
                session_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                PRIMARY KEY (session_id, position)
            );

            CREATE TABLE IF NOT EXISTS tool_runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                status TEXT NOT NULL,
                status_reason TEXT,
                blocking INTEGER NOT NULL,
                parent_run_id TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tool_runs_session_created_id
              ON tool_runs(session_id, created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_tool_runs_session_status_created_id
              ON tool_runs(session_id, status, created_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS steer_runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                source_agent_id TEXT,
                source_agent_name TEXT,
                status TEXT NOT NULL,
                status_reason TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                cancelled_at TEXT,
                delivered_step INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_steer_runs_session_created_id
              ON steer_runs(session_id, created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_steer_runs_session_status_created_id
              ON steer_runs(session_id, status, created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_steer_runs_session_agent_status_created_id
              ON steer_runs(session_id, agent_id, status, created_at DESC, id DESC);
            """
        )
        self._migrate_schema()
        self._migrate_status_semantics()
        self._commit()

    def _migrate_schema(self) -> None:
        self._ensure_column("sessions", "status_reason", "TEXT")
        self._ensure_column("sessions", "workspace_mode", "TEXT NOT NULL DEFAULT 'staged'")
        self._ensure_column("agents", "status_reason", "TEXT")
        self._ensure_column("tool_runs", "status_reason", "TEXT")
        self._ensure_column("steer_runs", "status_reason", "TEXT")
        self.connection.execute(
            """
            UPDATE sessions
            SET workspace_mode = 'staged'
            WHERE TRIM(COALESCE(workspace_mode, '')) = ''
            """
        )
        self._ensure_column("steer_runs", "source_agent_id", "TEXT")
        self._ensure_column("steer_runs", "source_agent_name", "TEXT")
        with self.batched_writes():
            self.connection.execute(
                """
                UPDATE steer_runs
                SET source_agent_id = COALESCE(NULLIF(TRIM(source_agent_id), ''), 'user'),
                    source_agent_name = COALESCE(
                        NULLIF(TRIM(source_agent_name), ''),
                        'user'
                    )
                WHERE NULLIF(TRIM(COALESCE(source_agent_id, '')), '') IS NULL
                   OR NULLIF(TRIM(COALESCE(source_agent_name, '')), '') IS NULL
                """
            )

    def _ensure_column(self, table: str, column: str, sql_type: str) -> None:
        normalized_table = str(table).strip()
        normalized_column = str(column).strip()
        if not normalized_table or not normalized_column:
            return
        rows = self.connection.execute(f"PRAGMA table_info({normalized_table})").fetchall()
        known_columns = {str(row["name"]).strip() for row in rows if row is not None}
        if normalized_column in known_columns:
            return
        self.connection.execute(
            f"ALTER TABLE {normalized_table} ADD COLUMN {normalized_column} {sql_type}"
        )

    def _migrate_status_semantics(self) -> None:
        with self.batched_writes():
            self.connection.execute(
                """
                UPDATE agents
                SET status = 'cancelled',
                    status_reason = COALESCE(
                        NULLIF(TRIM(status_reason), ''),
                        'migrated:terminated_cancelled_to_cancelled'
                    ),
                    completion_status = NULL
                WHERE LOWER(COALESCE(status, '')) = 'terminated'
                  AND LOWER(COALESCE(completion_status, '')) = 'cancelled'
                """
            )
            self.connection.execute(
                """
                UPDATE agents
                SET status = 'running',
                    status_reason = COALESCE(
                        NULLIF(TRIM(status_reason), ''),
                        'migrated:waiting_to_running'
                    )
                WHERE LOWER(COALESCE(status, '')) = 'waiting'
                """
            )
            self.connection.execute(
                """
                UPDATE sessions
                SET completion_state = NULL
                WHERE LOWER(COALESCE(status, '')) <> 'completed'
                """
            )
            self.connection.execute(
                """
                UPDATE sessions
                SET completion_state = 'partial',
                    status_reason = COALESCE(
                        NULLIF(TRIM(status_reason), ''),
                        'migrated:completion_state_interrupted_to_partial'
                    )
                WHERE LOWER(COALESCE(status, '')) = 'completed'
                  AND LOWER(COALESCE(completion_state, '')) = 'interrupted'
                """
            )

    @contextmanager
    def batched_writes(self):
        self._batched_write_depth += 1
        succeeded = False
        try:
            yield
            succeeded = True
        except Exception:
            if self._batched_write_depth == 1:
                self.connection.rollback()
            raise
        finally:
            self._batched_write_depth = max(0, self._batched_write_depth - 1)
            if succeeded and self._batched_write_depth == 0:
                self.connection.commit()

    def _commit(self) -> None:
        if self._batched_write_depth == 0:
            self.connection.commit()

    def upsert_session(self, session: RunSession) -> None:
        completion_state = normalize_session_completion_state(
            session_status=session.status,
            completion_state=session.completion_state,
        )
        self.connection.execute(
            """
            INSERT INTO sessions (
                id, project_dir, task, locale, root_agent_id, workspace_mode, status, created_at,
                status_reason, updated_at, loop_index, final_summary, completion_state,
                follow_up_needed, config_snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project_dir=excluded.project_dir,
                task=excluded.task,
                locale=excluded.locale,
                root_agent_id=excluded.root_agent_id,
                workspace_mode=excluded.workspace_mode,
                status=excluded.status,
                status_reason=excluded.status_reason,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                loop_index=excluded.loop_index,
                final_summary=excluded.final_summary,
                completion_state=excluded.completion_state,
                follow_up_needed=excluded.follow_up_needed,
                config_snapshot_json=excluded.config_snapshot_json
            """,
            (
                session.id,
                str(session.project_dir),
                session.task,
                session.locale,
                session.root_agent_id,
                session.workspace_mode.value,
                session.status.value,
                session.created_at,
                session.status_reason,
                session.updated_at,
                session.loop_index,
                session.final_summary,
                completion_state,
                int(session.follow_up_needed),
                json.dumps(json_ready(session.config_snapshot), ensure_ascii=False),
            ),
        )
        self._commit()

    def upsert_agent(self, agent: AgentNode) -> None:
        self.connection.execute(
            """
            INSERT INTO agents (
                id, session_id, name, role, instruction, workspace_id, parent_agent_id,
                status, status_reason, children_json, summary, next_recommendation, diff_artifact,
                completion_status, step_count, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id=excluded.session_id,
                name=excluded.name,
                role=excluded.role,
                instruction=excluded.instruction,
                workspace_id=excluded.workspace_id,
                parent_agent_id=excluded.parent_agent_id,
                status=excluded.status,
                status_reason=excluded.status_reason,
                children_json=excluded.children_json,
                summary=excluded.summary,
                next_recommendation=excluded.next_recommendation,
                diff_artifact=excluded.diff_artifact,
                completion_status=excluded.completion_status,
                step_count=excluded.step_count,
                metadata_json=excluded.metadata_json
            """,
            (
                agent.id,
                agent.session_id,
                agent.name,
                agent.role.value,
                agent.instruction,
                agent.workspace_id,
                agent.parent_agent_id,
                agent.status.value,
                agent.status_reason,
                json.dumps(agent.children),
                agent.summary,
                agent.next_recommendation,
                agent.diff_artifact,
                agent.completion_status,
                agent.step_count,
                json.dumps(json_ready(agent.metadata), ensure_ascii=False),
            ),
        )
        self._commit()

    def append_event(self, event: EventRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO events (
                timestamp, session_id, agent_id, parent_agent_id, event_type,
                phase, payload_json, workspace_id, checkpoint_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.timestamp,
                event.session_id,
                event.agent_id,
                event.parent_agent_id,
                event.event_type,
                event.phase,
                json.dumps(json_ready(event.payload), ensure_ascii=False),
                event.workspace_id,
                event.checkpoint_seq,
            ),
        )
        self._commit()

    def replace_pending_agents(self, session_id: str, agent_ids: list[str]) -> None:
        self.connection.execute("DELETE FROM pending_actions WHERE session_id = ?", (session_id,))
        for position, agent_id in enumerate(agent_ids):
            self.connection.execute(
                """
                INSERT INTO pending_actions (session_id, position, agent_id)
                VALUES (?, ?, ?)
                """,
                (session_id, position, agent_id),
            )
        self._commit()

    def save_checkpoint(self, session_id: str, created_at: str, state: CheckpointState) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = int(row[0])
        self.connection.execute(
            """
            INSERT INTO checkpoints (session_id, seq, created_at, state_json)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, seq, created_at, json.dumps(json_ready(state), ensure_ascii=False)),
        )
        self._commit()
        return seq

    def upsert_tool_run(self, tool_run: ToolRun) -> None:
        self.connection.execute(
            """
            INSERT INTO tool_runs (
                id, session_id, agent_id, tool_name, arguments_json, status,
                status_reason, blocking, parent_run_id, result_json, error, created_at, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id=excluded.session_id,
                agent_id=excluded.agent_id,
                tool_name=excluded.tool_name,
                arguments_json=excluded.arguments_json,
                status=excluded.status,
                status_reason=excluded.status_reason,
                blocking=excluded.blocking,
                parent_run_id=excluded.parent_run_id,
                result_json=excluded.result_json,
                error=excluded.error,
                created_at=excluded.created_at,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at
            """,
            (
                tool_run.id,
                tool_run.session_id,
                tool_run.agent_id,
                tool_run.tool_name,
                json.dumps(json_ready(tool_run.arguments), ensure_ascii=False),
                tool_run.status.value,
                tool_run.status_reason,
                int(tool_run.blocking),
                tool_run.parent_run_id,
                (
                    None
                    if tool_run.result is None
                    else json.dumps(json_ready(tool_run.result), ensure_ascii=False)
                ),
                tool_run.error,
                tool_run.created_at,
                tool_run.started_at,
                tool_run.completed_at,
            ),
        )
        self._commit()

    def load_tool_run(self, tool_run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tool_runs WHERE id = ?",
            (tool_run_id,),
        ).fetchone()
        if not row:
            return None
        return self._tool_run_row_to_dict(dict(row))

    def upsert_steer_run(self, steer_run: SteerRun) -> None:
        self.connection.execute(
            """
            INSERT INTO steer_runs (
                id, session_id, agent_id, content, source, source_agent_id, source_agent_name,
                status, status_reason,
                created_at, completed_at, cancelled_at, delivered_step
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id=excluded.session_id,
                agent_id=excluded.agent_id,
                content=excluded.content,
                source=excluded.source,
                source_agent_id=excluded.source_agent_id,
                source_agent_name=excluded.source_agent_name,
                status=excluded.status,
                status_reason=excluded.status_reason,
                created_at=excluded.created_at,
                completed_at=excluded.completed_at,
                cancelled_at=excluded.cancelled_at,
                delivered_step=excluded.delivered_step
            """,
            (
                steer_run.id,
                steer_run.session_id,
                steer_run.agent_id,
                steer_run.content,
                steer_run.source,
                steer_run.source_agent_id,
                steer_run.source_agent_name,
                steer_run.status.value,
                steer_run.status_reason,
                steer_run.created_at,
                steer_run.completed_at,
                steer_run.cancelled_at,
                (
                    int(steer_run.delivered_step)
                    if steer_run.delivered_step is not None
                    else None
                ),
            ),
        )
        self._commit()

    def load_steer_run(self, steer_run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM steer_runs WHERE id = ?",
            (steer_run_id,),
        ).fetchone()
        if not row:
            return None
        return self._steer_run_row_to_dict(dict(row))

    def list_tool_runs(
        self,
        *,
        session_id: str,
        agent_id: str | None = None,
        statuses: list[str] | None = None,
        cursor: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if statuses:
            normalized = [str(status) for status in statuses if str(status)]
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                clauses.append(f"status IN ({placeholders})")
                params.extend(normalized)
        if cursor is not None:
            cursor_created_at, cursor_id = cursor
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor_created_at, cursor_created_at, cursor_id])
        params.append(max(1, int(limit)))
        rows = self.connection.execute(
            (
                "SELECT * FROM tool_runs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, id DESC LIMIT ?"
            ),
            tuple(params),
        ).fetchall()
        return [self._tool_run_row_to_dict(dict(row)) for row in rows]

    def list_steer_runs(
        self,
        *,
        session_id: str,
        agent_id: str | None = None,
        statuses: list[str] | None = None,
        cursor: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if statuses:
            normalized = [str(status) for status in statuses if str(status)]
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                clauses.append(f"status IN ({placeholders})")
                params.extend(normalized)
        if cursor is not None:
            cursor_created_at, cursor_id = cursor
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor_created_at, cursor_created_at, cursor_id])
        params.append(max(1, int(limit)))
        rows = self.connection.execute(
            (
                "SELECT * FROM steer_runs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, id DESC LIMIT ?"
            ),
            tuple(params),
        ).fetchall()
        return [self._steer_run_row_to_dict(dict(row)) for row in rows]

    def load_tool_runs_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM tool_runs
            WHERE session_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
        return [self._tool_run_row_to_dict(dict(row)) for row in rows]

    def load_steer_runs_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM steer_runs
            WHERE session_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
        return [self._steer_run_row_to_dict(dict(row)) for row in rows]

    def pending_tool_run_ids(self, session_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT id FROM tool_runs
            WHERE session_id = ?
              AND status IN ('queued', 'running')
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def complete_waiting_steer_run(
        self,
        *,
        session_id: str,
        steer_run_id: str,
        completed_at: str,
        delivered_step: int | None,
    ) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """
            UPDATE steer_runs
            SET status = 'completed',
                completed_at = ?,
                cancelled_at = NULL,
                delivered_step = ?,
                status_reason = COALESCE(NULLIF(TRIM(status_reason), ''), 'completed_by_delivery')
            WHERE id = ?
              AND session_id = ?
              AND status = 'waiting'
            """,
            (
                completed_at,
                int(delivered_step) if delivered_step is not None else None,
                steer_run_id,
                session_id,
            ),
        )
        if int(cursor.rowcount or 0) <= 0:
            return None
        self._commit()
        return self.load_steer_run(steer_run_id)

    def cancel_waiting_steer_run(
        self,
        *,
        session_id: str,
        steer_run_id: str,
        cancelled_at: str,
    ) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """
            UPDATE steer_runs
            SET status = 'cancelled',
                cancelled_at = ?,
                completed_at = NULL,
                status_reason = COALESCE(NULLIF(TRIM(status_reason), ''), 'cancelled_by_request')
            WHERE id = ?
              AND session_id = ?
              AND status = 'waiting'
            """,
            (
                cancelled_at,
                steer_run_id,
                session_id,
            ),
        )
        if int(cursor.rowcount or 0) <= 0:
            return None
        self._commit()
        return self.load_steer_run(steer_run_id)

    def steer_run_metrics(self, session_id: str) -> dict[str, Any]:
        status_rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM steer_runs
            WHERE session_id = ?
            GROUP BY status
            """,
            (session_id,),
        ).fetchall()
        by_agent_rows = self.connection.execute(
            """
            SELECT agent_id, status, COUNT(*) AS count
            FROM steer_runs
            WHERE session_id = ?
            GROUP BY agent_id, status
            """,
            (session_id,),
        ).fetchall()
        status_counts: dict[str, int] = {
            "waiting": 0,
            "completed": 0,
            "cancelled": 0,
        }
        for row in status_rows:
            status = str(row["status"] or "").strip().lower()
            if status in status_counts:
                status_counts[status] = int(row["count"] or 0)
        by_agent: dict[str, dict[str, Any]] = {}
        for row in by_agent_rows:
            agent_id = str(row["agent_id"] or "").strip() or "-"
            status = str(row["status"] or "").strip().lower()
            count = int(row["count"] or 0)
            entry = by_agent.setdefault(
                agent_id,
                {
                    "agent_id": agent_id,
                    "total_runs": 0,
                    "status_counts": {
                        "waiting": 0,
                        "completed": 0,
                        "cancelled": 0,
                    },
                },
            )
            entry["total_runs"] = int(entry.get("total_runs", 0)) + count
            status_map = entry.get("status_counts")
            if isinstance(status_map, dict) and status in status_map:
                status_map[status] = int(status_map.get(status, 0)) + count
        return {
            "session_id": session_id,
            "total_runs": sum(status_counts.values()),
            "waiting_runs": int(status_counts["waiting"]),
            "completed_runs": int(status_counts["completed"]),
            "cancelled_runs": int(status_counts["cancelled"]),
            "status_counts": status_counts,
            "by_agent": sorted(by_agent.values(), key=lambda item: str(item.get("agent_id", ""))),
        }

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def load_agents(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM agents WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT seq, created_at, state_json
            FROM checkpoints
            WHERE session_id = ?
            ORDER BY seq DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "seq": row["seq"],
            "created_at": row["created_at"],
            "state": json.loads(row["state_json"]),
        }

    def load_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT seq, created_at, state_json
            FROM checkpoints
            WHERE session_id = ?
            ORDER BY seq ASC
            """,
            (session_id,),
        ).fetchall()
        checkpoints: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except json.JSONDecodeError:
                state = {}
            checkpoints.append(
                {
                    "seq": int(row["seq"]),
                    "created_at": str(row["created_at"]),
                    "state": state,
                }
            )
        return checkpoints

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def load_events(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def export_session(self, session_id: str) -> dict[str, Any]:
        checkpoint = self.latest_checkpoint(session_id)
        return {
            "session": self.load_session(session_id),
            "agents": self.load_agents(session_id),
            "events": self.load_events(session_id),
            "tool_runs": self.list_tool_runs(session_id=session_id, limit=500),
            "steer_runs": self.list_steer_runs(session_id=session_id, limit=500),
            "checkpoint": checkpoint,
        }

    @staticmethod
    def _tool_run_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        arguments_json = row.get("arguments_json")
        result_json = row.get("result_json")
        try:
            arguments = json.loads(arguments_json) if isinstance(arguments_json, str) else {}
        except json.JSONDecodeError:
            arguments = {}
        try:
            result = json.loads(result_json) if isinstance(result_json, str) else None
        except json.JSONDecodeError:
            result = None
        return {
            "id": row.get("id"),
            "session_id": row.get("session_id"),
            "agent_id": row.get("agent_id"),
            "tool_name": row.get("tool_name"),
            "arguments": arguments,
            "status": row.get("status"),
            "status_reason": row.get("status_reason"),
            "blocking": bool(int(row.get("blocking", 0) or 0)),
            "parent_run_id": row.get("parent_run_id"),
            "result": result,
            "error": row.get("error"),
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
        }

    @staticmethod
    def _steer_run_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        delivered_step_value = row.get("delivered_step")
        try:
            delivered_step = (
                int(delivered_step_value)
                if delivered_step_value is not None
                else None
            )
        except (TypeError, ValueError):
            delivered_step = None
        return {
            "id": row.get("id"),
            "session_id": row.get("session_id"),
            "agent_id": row.get("agent_id"),
            "content": str(row.get("content", "") or ""),
            "source": str(row.get("source", "") or ""),
            "source_agent_id": str(row.get("source_agent_id", "") or "user"),
            "source_agent_name": str(row.get("source_agent_name", "") or "user"),
            "status": row.get("status"),
            "status_reason": row.get("status_reason"),
            "created_at": row.get("created_at"),
            "completed_at": row.get("completed_at"),
            "cancelled_at": row.get("cancelled_at"),
            "delivered_step": delivered_step,
        }
