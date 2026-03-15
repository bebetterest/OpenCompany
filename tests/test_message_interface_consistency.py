from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import opencompany.cli as cli
from opencompany.orchestrator import Orchestrator
from opencompany.webui import create_webui_app
from starlette.testclient import TestClient


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    path.write_text(text, encoding="utf-8")


class MessageInterfaceConsistencyTests(unittest.TestCase):
    def test_orchestrator_cli_webui_tui_read_same_message_pages(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-consistency"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            _write_jsonl(
                session_dir / "agent-root_messages.jsonl",
                [
                    {
                        "timestamp": "2026-03-11T12:00:00Z",
                        "session_id": session_id,
                        "agent_id": "agent-root",
                        "agent_name": "Root",
                        "agent_role": "root",
                        "message_index": 0,
                        "role": "user",
                        "message": {"role": "user", "content": "task"},
                    },
                    {
                        "timestamp": "2026-03-11T12:00:02Z",
                        "session_id": session_id,
                        "agent_id": "agent-root",
                        "agent_name": "Root",
                        "agent_role": "root",
                        "message_index": 1,
                        "role": "assistant",
                        "message": {"role": "assistant", "content": '{"actions":[{"type":"finish"}]}'},
                    },
                ],
            )
            _write_jsonl(
                session_dir / "agent-worker_messages.jsonl",
                [
                    {
                        "timestamp": "2026-03-11T12:00:01Z",
                        "session_id": session_id,
                        "agent_id": "agent-worker",
                        "agent_name": "Worker",
                        "agent_role": "worker",
                        "message_index": 0,
                        "role": "user",
                        "message": {"role": "user", "content": "inspect"},
                    }
                ],
            )

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            expected_page = orchestrator.list_session_messages(session_id, limit=500)
            expected_messages = expected_page["messages"]
            self.assertEqual(
                [record["agent_id"] for record in expected_messages],
                ["agent-root", "agent-worker", "agent-root"],
            )

            first_page = orchestrator.list_session_messages(session_id, limit=1)
            cursor = first_page["next_cursor"]
            self.assertTrue(cursor)
            expected_cursor_page = orchestrator.list_session_messages(
                session_id,
                cursor=cursor,
                limit=500,
            )

            cli_output = io.StringIO()
            with redirect_stdout(cli_output):
                cli._messages(
                    app_dir=app_dir,
                    session_id=session_id,
                    agent_id=None,
                    tail=200,
                    cursor=None,
                    include_extra=False,
                    output_format="json",
                )
            cli_page = json.loads(cli_output.getvalue())
            self.assertEqual(cli_page["messages"], expected_messages)
            self.assertEqual(cli_page["next_cursor"], expected_page["next_cursor"])
            self.assertEqual(cli_page["has_more"], expected_page["has_more"])

            cli_cursor_output = io.StringIO()
            with redirect_stdout(cli_cursor_output):
                cli._messages(
                    app_dir=app_dir,
                    session_id=session_id,
                    agent_id=None,
                    tail=200,
                    cursor=cursor,
                    include_extra=False,
                    output_format="json",
                )
            cli_cursor_page = json.loads(cli_cursor_output.getvalue())
            self.assertEqual(cli_cursor_page["messages"], expected_cursor_page["messages"])
            self.assertEqual(cli_cursor_page["next_cursor"], expected_cursor_page["next_cursor"])
            self.assertEqual(cli_cursor_page["has_more"], expected_cursor_page["has_more"])

            app = create_webui_app(app_dir=app_dir)
            with TestClient(app) as client:
                response = client.get(f"/api/session/{session_id}/messages?limit=500")
                self.assertEqual(response.status_code, 200)
                web_page = response.json()
                self.assertEqual(web_page["messages"], expected_messages)
                self.assertEqual(web_page["next_cursor"], expected_page["next_cursor"])
                self.assertEqual(web_page["has_more"], expected_page["has_more"])

                response_cursor = client.get(
                    f"/api/session/{session_id}/messages?limit=500&cursor={cursor}"
                )
                self.assertEqual(response_cursor.status_code, 200)
                web_cursor_page = response_cursor.json()
                self.assertEqual(web_cursor_page["messages"], expected_cursor_page["messages"])
                self.assertEqual(web_cursor_page["next_cursor"], expected_cursor_page["next_cursor"])
                self.assertEqual(web_cursor_page["has_more"], expected_cursor_page["has_more"])

            try:
                from opencompany.tui.app import OpenCompanyApp
            except ImportError:
                self.skipTest("textual is not installed in the current environment")

            tui_app = OpenCompanyApp(project_dir=Path.cwd(), app_dir=app_dir)
            records, next_cursor = tui_app._collect_session_messages(orchestrator, session_id)
            self.assertEqual(records, expected_messages)
            self.assertEqual(next_cursor, expected_page["next_cursor"])

            records_cursor, next_cursor_cursor = tui_app._collect_session_messages(
                orchestrator,
                session_id,
                cursor=cursor,
            )
            self.assertEqual(records_cursor, expected_cursor_page["messages"])
            self.assertEqual(next_cursor_cursor, expected_cursor_page["next_cursor"])
