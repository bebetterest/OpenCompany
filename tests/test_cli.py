from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import opencompany.cli as cli
from opencompany.cli import build_parser


class CliParserTests(unittest.TestCase):
    def test_tui_defaults_to_prompted_configuration(self) -> None:
        args = build_parser().parse_args(["tui"])
        self.assertIsNone(args.project_dir)
        self.assertIsNone(args.session_id)

    def test_tui_accepts_project_dir_and_session_id(self) -> None:
        args = build_parser().parse_args(
            ["tui", "--project-dir", "/tmp/demo", "--session-id", "session-123"]
        )
        self.assertEqual(args.project_dir, "/tmp/demo")
        self.assertEqual(args.session_id, "session-123")

    def test_ui_defaults(self) -> None:
        args = build_parser().parse_args(["ui"])
        self.assertIsNone(args.project_dir)
        self.assertIsNone(args.session_id)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)

    def test_ui_accepts_project_session_and_server_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "ui",
                "--project-dir",
                "/tmp/demo",
                "--session-id",
                "session-123",
                "--host",
                "0.0.0.0",
                "--port",
                "9090",
            ]
        )
        self.assertEqual(args.project_dir, "/tmp/demo")
        self.assertEqual(args.session_id, "session-123")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9090)

    def test_run_still_defaults_to_current_directory(self) -> None:
        args = build_parser().parse_args(["run", "inspect repo"])
        self.assertEqual(args.project_dir, ".")
        self.assertIsNone(args.workspace_mode)
        self.assertIsNone(args.sandbox_backend)
        self.assertIsNone(args.model)
        self.assertIsNone(args.root_agent_name)
        self.assertFalse(args.debug)
        self.assertEqual(args.preview_chars, 256)

    def test_run_tui_and_ui_accept_workspace_mode(self) -> None:
        run_args = build_parser().parse_args(["run", "--workspace-mode", "staged", "inspect repo"])
        tui_args = build_parser().parse_args(["tui", "--workspace-mode", "direct"])
        ui_args = build_parser().parse_args(["ui", "--workspace-mode", "staged"])

        self.assertEqual(run_args.workspace_mode, "staged")
        self.assertEqual(tui_args.workspace_mode, "direct")
        self.assertEqual(ui_args.workspace_mode, "staged")

    def test_run_accepts_sandbox_backend_model_and_root_agent_name(self) -> None:
        args = build_parser().parse_args(
            [
                "run",
                "--sandbox-backend",
                "none",
                "--model",
                "fake/model",
                "--root-agent-name",
                "Demo Root",
                "inspect repo",
            ]
        )
        self.assertEqual(args.sandbox_backend, "none")
        self.assertEqual(args.model, "fake/model")
        self.assertEqual(args.root_agent_name, "Demo Root")

    def test_run_resume_and_skills_command_accept_skill_flags(self) -> None:
        run_args = build_parser().parse_args(
            ["run", "--skill", "skill-a", "--skill", "skill-b", "inspect repo"]
        )
        resume_args = build_parser().parse_args(
            ["resume", "session-123", "--skill", "skill-c", "continue"]
        )
        skills_args = build_parser().parse_args(["skills", "--project-dir", "/tmp/demo"])

        self.assertEqual(run_args.skills, ["skill-a", "skill-b"])
        self.assertEqual(resume_args.skills, ["skill-c"])
        self.assertEqual(skills_args.project_dir, "/tmp/demo")

    def test_run_resume_and_mcp_servers_command_accept_mcp_flags(self) -> None:
        run_args = build_parser().parse_args(
            ["run", "--mcp-server", "filesystem", "--mcp-server", "docs", "inspect repo"]
        )
        resume_args = build_parser().parse_args(
            ["resume", "session-123", "--mcp-server", "docs", "continue"]
        )
        inspect_args = build_parser().parse_args(
            ["mcp-servers", "--mcp-server", "filesystem", "--project-dir", "/tmp/demo"]
        )

        self.assertEqual(run_args.mcp_servers, ["filesystem", "docs"])
        self.assertEqual(resume_args.mcp_servers, ["docs"])
        self.assertEqual(inspect_args.mcp_servers, ["filesystem"])
        self.assertEqual(inspect_args.project_dir, "/tmp/demo")

    def test_run_tui_and_ui_accept_remote_flags(self) -> None:
        run_args = build_parser().parse_args(
            [
                "run",
                "--remote-target",
                "demo@example.com:2222",
                "--remote-dir",
                "/home/demo/workspace",
                "--remote-auth",
                "key",
                "--remote-key-path",
                "~/.ssh/id_ed25519",
                "--remote-known-hosts",
                "strict",
                "inspect repo",
            ]
        )
        tui_args = build_parser().parse_args(
            [
                "tui",
                "--remote-target",
                "demo@example.com",
                "--remote-dir",
                "/home/demo/workspace",
                "--remote-auth",
                "password",
            ]
        )
        ui_args = build_parser().parse_args(
            [
                "ui",
                "--remote-target",
                "demo@example.com",
                "--remote-dir",
                "/home/demo/workspace",
            ]
        )
        self.assertEqual(run_args.remote_target, "demo@example.com:2222")
        self.assertEqual(run_args.remote_known_hosts, "strict")
        self.assertEqual(tui_args.remote_auth, "password")
        self.assertEqual(ui_args.remote_target, "demo@example.com")

    def test_remote_cli_config_from_args_parses_key_auth(self) -> None:
        args = SimpleNamespace(
            remote_target="demo@example.com:2222",
            remote_dir="/home/demo/workspace",
            remote_auth="key",
            remote_key_path="~/.ssh/id_ed25519",
            remote_known_hosts="accept_new",
        )
        remote_config, remote_password = cli._remote_cli_config_from_args(
            args,
            command_name="run",
        )
        self.assertIsNotNone(remote_config)
        assert remote_config is not None
        self.assertEqual(remote_config.ssh_target, "demo@example.com:2222")
        self.assertEqual(remote_config.auth_mode, "key")
        self.assertEqual(remote_config.identity_file, "~/.ssh/id_ed25519")
        self.assertEqual(remote_config.known_hosts_policy, "accept_new")
        self.assertIsNone(remote_password)

    def test_remote_cli_config_from_args_prompts_password_auth(self) -> None:
        args = SimpleNamespace(
            remote_target="demo@example.com",
            remote_dir="/home/demo/workspace",
            remote_auth="password",
            remote_key_path="",
            remote_known_hosts="accept_new",
        )
        with patch.object(cli.getpass, "getpass", return_value="secret-pass"):
            remote_config, remote_password = cli._remote_cli_config_from_args(
                args,
                command_name="run",
            )
        self.assertIsNotNone(remote_config)
        assert remote_config is not None
        self.assertEqual(remote_config.auth_mode, "password")
        self.assertEqual(remote_password, "secret-pass")

    def test_maybe_prompt_remote_password_uses_stored_secret_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-remote-password"
            session_dir = app_dir / ".opencompany" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "remote_session.json").write_text(
                json.dumps(
                    {
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:22",
                        "remote_dir": "/home/demo/workspace",
                        "auth_mode": "password",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                        "password_ref": "ref-session-remote",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            orchestrator = cli.Orchestrator(Path("."), app_dir=app_dir)
            with (
                patch.object(cli, "load_remote_session_password", return_value="stored-secret"),
                patch.object(cli.getpass, "getpass", side_effect=AssertionError("should not prompt")),
            ):
                result = cli._maybe_prompt_remote_password_for_session(orchestrator, session_id)
            self.assertIsNone(result)

    def test_run_resume_tui_and_ui_accept_debug_flag(self) -> None:
        run_args = build_parser().parse_args(["run", "--debug", "inspect repo"])
        resume_args = build_parser().parse_args(
            ["resume", "session-123", "continue from latest status", "--debug"]
        )
        tui_args = build_parser().parse_args(["tui", "--debug"])
        ui_args = build_parser().parse_args(["ui", "--debug"])

        self.assertTrue(run_args.debug)
        self.assertTrue(resume_args.debug)
        self.assertTrue(tui_args.debug)
        self.assertTrue(ui_args.debug)

    def test_run_and_resume_accept_preview_chars(self) -> None:
        run_args = build_parser().parse_args(["run", "--preview-chars", "512", "inspect repo"])
        resume_args = build_parser().parse_args(
            ["resume", "session-123", "continue from latest status", "--preview-chars", "512"]
        )
        self.assertEqual(run_args.preview_chars, 512)
        self.assertEqual(resume_args.preview_chars, 512)

    def test_resume_accepts_sandbox_backend_and_model(self) -> None:
        args = build_parser().parse_args(
            [
                "resume",
                "session-123",
                "continue from latest status",
                "--sandbox-backend",
                "anthropic",
                "--model",
                "fake/model",
            ]
        )
        self.assertEqual(args.sandbox_backend, "anthropic")
        self.assertEqual(args.model, "fake/model")

    def test_resume_requires_instruction(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["resume", "session-123"])

    def test_clone_accepts_session_id_and_common_flags(self) -> None:
        args = build_parser().parse_args(
            ["clone", "session-123", "--app-dir", "/tmp/app", "--locale", "zh", "--debug"]
        )
        self.assertEqual(args.session_id, "session-123")
        self.assertEqual(args.app_dir, "/tmp/app")
        self.assertEqual(args.locale, "zh")
        self.assertTrue(args.debug)

    def test_apply_and_undo_commands_are_available(self) -> None:
        apply_args = build_parser().parse_args(["apply", "session-123", "--yes"])
        undo_args = build_parser().parse_args(["undo", "session-123", "--yes"])

        self.assertEqual(apply_args.session_id, "session-123")
        self.assertTrue(apply_args.yes)
        self.assertEqual(undo_args.session_id, "session-123")
        self.assertTrue(undo_args.yes)

    def test_terminal_command_accepts_session_id(self) -> None:
        args = build_parser().parse_args(["terminal", "session-123"])
        self.assertEqual(args.session_id, "session-123")
        self.assertFalse(args.self_check)

    def test_terminal_command_accepts_self_check_flag(self) -> None:
        args = build_parser().parse_args(["terminal", "session-123", "--self-check"])
        self.assertEqual(args.session_id, "session-123")
        self.assertTrue(args.self_check)

    def test_main_terminal_dispatches_session_id(self) -> None:
        captured: dict[str, object] = {}

        def _fake_terminal(app_dir, session_id, *, self_check=False):  # type: ignore[no-untyped-def]
            captured["app_dir"] = app_dir
            captured["session_id"] = session_id
            captured["self_check"] = self_check

        with patch.object(sys, "argv", ["opencompany", "terminal", "session-123"]):
            with patch.object(cli, "_terminal", side_effect=_fake_terminal):
                cli.main()

        self.assertEqual(captured["session_id"], "session-123")
        self.assertFalse(bool(captured["self_check"]))

    def test_main_terminal_dispatches_self_check_flag(self) -> None:
        captured: dict[str, object] = {}

        def _fake_terminal(app_dir, session_id, *, self_check=False):  # type: ignore[no-untyped-def]
            captured["app_dir"] = app_dir
            captured["session_id"] = session_id
            captured["self_check"] = self_check

        with patch.object(sys, "argv", ["opencompany", "terminal", "session-123", "--self-check"]):
            with patch.object(cli, "_terminal", side_effect=_fake_terminal):
                cli.main()

        self.assertEqual(captured["session_id"], "session-123")
        self.assertTrue(bool(captured["self_check"]))

    def test_main_tui_rejects_workspace_mode_override_with_session_id(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["opencompany", "tui", "--session-id", "session-123", "--workspace-mode", "staged"],
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        self.assertIn("--workspace-mode can only be set", str(ctx.exception))

    def test_main_run_rejects_remote_with_staged_mode(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "opencompany",
                "run",
                "--workspace-mode",
                "staged",
                "--remote-target",
                "demo@example.com",
                "--remote-dir",
                "/home/demo/workspace",
                "--remote-auth",
                "key",
                "--remote-key-path",
                "~/.ssh/id_ed25519",
                "inspect repo",
            ],
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        self.assertIn("Remote mode is not supported", str(ctx.exception))

    def test_tool_runs_accepts_cursor(self) -> None:
        args = build_parser().parse_args(
            [
                "tool-runs",
                "session-123",
                "--limit",
                "20",
                "--cursor",
                "cursor-token",
            ]
        )
        self.assertEqual(args.session_id, "session-123")
        self.assertEqual(args.limit, 20)
        self.assertEqual(args.cursor, "cursor-token")

    def test_main_tool_runs_uses_none_limit_when_flag_is_omitted(self) -> None:
        captured: dict[str, object] = {}

        def _fake_tool_runs(app_dir, session_id, status, limit, cursor):  # type: ignore[no-untyped-def]
            captured["app_dir"] = app_dir
            captured["session_id"] = session_id
            captured["status"] = status
            captured["limit"] = limit
            captured["cursor"] = cursor

        with patch.object(sys, "argv", ["opencompany", "tool-runs", "session-123"]):
            with patch.object(cli, "_tool_runs", side_effect=_fake_tool_runs):
                cli.main()

        self.assertEqual(captured["session_id"], "session-123")
        self.assertIsNone(captured["status"])
        self.assertIsNone(captured["limit"])
        self.assertIsNone(captured["cursor"])

    def test_tool_run_metrics_command_accepts_export_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "tool-run-metrics",
                "session-123",
                "--export",
            ]
        )
        self.assertEqual(args.session_id, "session-123")
        self.assertTrue(args.export)

    def test_tool_run_metrics_command_accepts_export_path_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "tool-run-metrics",
                "session-123",
                "--export",
                "--export-path",
                "/tmp/tool-run-metrics.json",
            ]
        )
        self.assertEqual(args.session_id, "session-123")
        self.assertTrue(args.export)
        self.assertEqual(args.export_path, "/tmp/tool-run-metrics.json")

    def test_main_export_logs_rejects_directory_export_path(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["opencompany", "export-logs", "session-123", "--export-path", "./"],
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        self.assertIn("--export-path must be a file path", str(ctx.exception))

    def test_main_tool_run_metrics_rejects_directory_export_path(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["opencompany", "tool-run-metrics", "session-123", "--export", "--export-path", "./"],
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        self.assertIn("--export-path must be a file path", str(ctx.exception))

    def test_export_logs_command_accepts_export_path_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "export-logs",
                "session-123",
                "--export-path",
                "/tmp/custom-export.json",
            ]
        )
        self.assertEqual(args.session_id, "session-123")
        self.assertEqual(args.export_path, "/tmp/custom-export.json")

    def test_messages_command_defaults(self) -> None:
        args = build_parser().parse_args(["messages", "session-123"])
        self.assertEqual(args.session_id, "session-123")
        self.assertIsNone(args.agent_id)
        self.assertEqual(args.tail, 200)
        self.assertEqual(args.format, "json")
        self.assertFalse(args.include_extra)

    def test_messages_command_accepts_filters_and_text_output(self) -> None:
        args = build_parser().parse_args(
            [
                "messages",
                "session-123",
                "--agent-id",
                "agent-root",
                "--tail",
                "50",
                "--include-extra",
                "--format",
                "text",
                "--cursor",
                "cursor-token",
            ]
        )
        self.assertEqual(args.session_id, "session-123")
        self.assertEqual(args.agent_id, "agent-root")
        self.assertEqual(args.tail, 50)
        self.assertTrue(args.include_extra)
        self.assertEqual(args.format, "text")
        self.assertEqual(args.cursor, "cursor-token")


class CliOutputTests(unittest.TestCase):
    def test_export_logs_passes_optional_export_path(self) -> None:
        captured: dict[str, object] = {}

        class _FakeDiagnostics:
            def log(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                self.diagnostics = _FakeDiagnostics()

            def export_logs(self, session_id, export_path=None):  # type: ignore[no-untyped-def]
                captured["session_id"] = session_id
                captured["export_path"] = export_path
                return export_path or Path("/tmp/default-export.json")

        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            cli._export(
                app_dir=None,
                session_id="session-123",
                export_path=Path("/tmp/custom-export.json"),
            )

        self.assertEqual(captured["session_id"], "session-123")
        self.assertEqual(captured["export_path"], Path("/tmp/custom-export.json"))

    def test_terminal_self_check_prints_report(self) -> None:
        output = io.StringIO()

        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def terminal_self_check(self, session_id):  # type: ignore[no-untyped-def]
                return {
                    "session_id": session_id,
                    "workspace_root": "/tmp/workspace",
                    "passed": True,
                    "runtime_error": None,
                    "checks": {
                        "policy_match_agent_shell": {"ok": True},
                        "settings_match_agent_shell": {"ok": True},
                        "workspace_write": {"ok": True},
                        "outside_write_blocked": {"ok": True},
                    },
                }

        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._terminal(app_dir=None, session_id="session-123", self_check=True)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["session_id"], "session-123")
        self.assertTrue(payload["passed"])

    def test_terminal_rejects_invalid_session_id_before_orchestrator_init(self) -> None:
        with patch.object(cli, "Orchestrator") as orchestrator_cls:
            with self.assertRaises(SystemExit):
                cli._terminal(app_dir=None, session_id="../escape", self_check=False)
        orchestrator_cls.assert_not_called()

    def test_terminal_self_check_failure_raises_system_exit(self) -> None:
        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def terminal_self_check(self, session_id):  # type: ignore[no-untyped-def]
                return {
                    "session_id": session_id,
                    "workspace_root": "/tmp/workspace",
                    "passed": False,
                    "runtime_error": "simulated failure",
                    "checks": {},
                }

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as ctx:
                    cli._terminal(app_dir=None, session_id="session-123", self_check=True)

        self.assertIn("self-check failed", str(ctx.exception))

    def test_tool_run_metrics_passes_optional_export_path(self) -> None:
        captured: dict[str, object] = {}

        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def export_tool_run_metrics(self, session_id, export_path=None):  # type: ignore[no-untyped-def]
                captured["session_id"] = session_id
                captured["export_path"] = export_path
                return export_path or Path("/tmp/tool_run_metrics.json")

            def tool_run_metrics(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {"session_id": "session-123", "total_runs": 1}

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._tool_run_metrics(
                    app_dir=None,
                    session_id="session-123",
                    export=True,
                    export_path=Path("/tmp/custom-tool-run-metrics.json"),
                )

        self.assertEqual(captured["session_id"], "session-123")
        self.assertEqual(captured["export_path"], Path("/tmp/custom-tool-run-metrics.json"))

    def test_messages_json_output_includes_cursor_and_flags(self) -> None:
        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def list_session_messages(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "messages": [
                        {
                            "timestamp": "2026-03-10T10:00:00Z",
                            "session_id": "session-123",
                            "agent_id": "agent-root",
                            "agent_name": "Root",
                            "message_index": 0,
                            "role": "assistant",
                            "message": {"content": "hello"},
                        }
                    ],
                    "next_cursor": "cursor-next",
                    "has_more": True,
                }

            def load_session_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return []

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._messages(
                    app_dir=None,
                    session_id="session-123",
                    agent_id=None,
                    tail=200,
                    cursor=None,
                    include_extra=False,
                    output_format="json",
                )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["session_id"], "session-123")
        self.assertEqual(payload["next_cursor"], "cursor-next")
        self.assertTrue(payload["has_more"])
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["agent_id"], "agent-root")

    def test_messages_text_output_can_include_extra_events(self) -> None:
        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def list_session_messages(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "messages": [
                        {
                            "timestamp": "2026-03-10T10:00:00Z",
                            "session_id": "session-123",
                            "agent_id": "agent-root",
                            "agent_name": "Root",
                            "message_index": 0,
                            "role": "assistant",
                            "message": {"content": "hello from assistant"},
                        }
                    ],
                    "next_cursor": None,
                    "has_more": False,
                }

            def load_session_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return [
                    {
                        "timestamp": "2026-03-10T10:00:01Z",
                        "session_id": "session-123",
                        "agent_id": "agent-root",
                        "event_type": "shell_stream",
                        "payload": {"text": "line-1\n"},
                    }
                ]

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._messages(
                    app_dir=None,
                    session_id="session-123",
                    agent_id=None,
                    tail=200,
                    cursor=None,
                    include_extra=True,
                    output_format="text",
                )
        rendered = output.getvalue()
        self.assertIn("message Root", rendered)
        self.assertIn("extra shell_stream", rendered)

    def test_messages_text_output_pretty_prints_tool_message_without_truncation(self) -> None:
        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def list_session_messages(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                long_value = "z" * 256
                return {
                    "messages": [
                        {
                            "timestamp": "2026-03-10T10:00:00Z",
                            "session_id": "session-123",
                            "agent_id": "agent-root",
                            "agent_name": "Root",
                            "message_index": 1,
                            "role": "tool",
                            "message": {
                                "tool_call_id": "call-1",
                                "content": json.dumps(
                                    {"result": {"nested": {"value": long_value}}},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                    "next_cursor": None,
                    "has_more": False,
                }

            def load_session_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return []

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._messages(
                    app_dir=None,
                    session_id="session-123",
                    agent_id=None,
                    tail=200,
                    cursor=None,
                    include_extra=False,
                    output_format="text",
                )
        rendered = output.getvalue()
        self.assertIn("tool_call_id=call-1", rendered)
        self.assertIn('"nested": {', rendered)
        self.assertIn("z" * 256, rendered)

    def test_messages_text_output_keeps_non_assistant_in_previous_step(self) -> None:
        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def list_session_messages(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "messages": [
                        {
                            "timestamp": "2026-03-10T10:00:00Z",
                            "session_id": "session-123",
                            "agent_id": "agent-root",
                            "agent_name": "Root",
                            "message_index": 0,
                            "role": "assistant",
                            "message": {"content": '{"actions":[{"type":"list_agent_runs","path":"."}]}'},
                        },
                        {
                            "timestamp": "2026-03-10T10:00:01Z",
                            "session_id": "session-123",
                            "agent_id": "agent-root",
                            "agent_name": "Root",
                            "message_index": 1,
                            "role": "user",
                            "message": {"content": "fallback control"},
                        },
                    ],
                    "next_cursor": None,
                    "has_more": False,
                }

            def load_session_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return []

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._messages(
                    app_dir=None,
                    session_id="session-123",
                    agent_id=None,
                    tail=200,
                    cursor=None,
                    include_extra=False,
                    output_format="text",
                )
        rendered = output.getvalue()
        self.assertIn("idx=0 step=1 role=assistant", rendered)
        self.assertIn("idx=1 step=1 role=user", rendered)

    def test_messages_text_output_filters_preview_and_tool_run_extra_events(self) -> None:
        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def list_session_messages(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "messages": [],
                    "next_cursor": None,
                    "has_more": False,
                }

            def load_session_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return [
                    {
                        "timestamp": "2026-03-10T10:00:01Z",
                        "session_id": "session-123",
                        "agent_id": "agent-root",
                        "event_type": "llm_token",
                        "payload": {"token": "stream"},
                    },
                    {
                        "timestamp": "2026-03-10T10:00:02Z",
                        "session_id": "session-123",
                        "agent_id": "agent-root",
                        "event_type": "tool_call_started",
                        "payload": {"action": {"type": "shell"}},
                    },
                    {
                        "timestamp": "2026-03-10T10:00:03Z",
                        "session_id": "session-123",
                        "agent_id": "agent-root",
                        "event_type": "tool_run_updated",
                        "payload": {"tool_run_id": "toolrun-1", "status": "completed"},
                    },
                    {
                        "timestamp": "2026-03-10T10:00:04Z",
                        "session_id": "session-123",
                        "agent_id": "agent-root",
                        "event_type": "control_message",
                        "payload": {"content": "checkpoint"},
                    },
                ]

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._messages(
                    app_dir=None,
                    session_id="session-123",
                    agent_id=None,
                    tail=200,
                    cursor=None,
                    include_extra=True,
                    output_format="text",
                )
        rendered = output.getvalue()
        self.assertNotIn("extra llm_token", rendered)
        self.assertNotIn("extra tool_call_started", rendered)
        self.assertNotIn("extra tool_run_updated", rendered)
        self.assertIn("extra control_message", rendered)

    def test_tool_runs_output_reports_has_more_and_filters(self) -> None:
        class _FakeTools:
            @staticmethod
            def normalize_list_limit(value):  # type: ignore[no-untyped-def]
                return 20 if value is None else int(value)

        class _FakeRuntime:
            tools = _FakeTools()

        class _FakeConfig:
            runtime = _FakeRuntime()

        class _FakeOrchestrator:
            def __init__(self, *_args, **_kwargs) -> None:
                self.config = _FakeConfig()

            def list_tool_runs_page(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "tool_runs": [
                        {
                            "id": "toolrun-1",
                            "session_id": "session-123",
                            "agent_id": "agent-root",
                            "tool_name": "shell",
                            "status": "running",
                        }
                    ],
                    "next_cursor": "cursor-next",
                    "has_more": True,
                }

        output = io.StringIO()
        with patch.object(cli, "Orchestrator", _FakeOrchestrator):
            with redirect_stdout(output):
                cli._tool_runs(
                    app_dir=None,
                    session_id="session-123",
                    status="running",
                    limit=20,
                    cursor=None,
                )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["session_id"], "session-123")
        self.assertEqual(payload["status_filter"], "running")
        self.assertEqual(payload["limit"], 20)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_cursor"], "cursor-next")


class CliRunResumePanelTests(unittest.TestCase):
    class _TTYBuffer(io.StringIO):
        def isatty(self) -> bool:  # type: ignore[override]
            return True

    @staticmethod
    def _fake_agent_row(
        *,
        agent_id: str,
        role: str,
        status: str,
        instruction: str,
        summary: str,
        parent_agent_id: str | None,
        children: list[str],
        model: str | None = None,
        completion_status: str | None = None,
        step_count: int = 0,
    ) -> dict[str, object]:
        return {
            "id": agent_id,
            "name": "Root Coordinator" if role == "root" else "Worker Agent",
            "role": role,
            "status": status,
            "instruction": instruction,
            "summary": summary,
            "parent_agent_id": parent_agent_id,
            "children_json": json.dumps(children),
            "metadata_json": json.dumps({"model": model} if model else {}),
            "completion_status": completion_status,
            "step_count": step_count,
        }

    def _fake_orchestrator_class(self):  # type: ignore[no-untyped-def]
        class _FakeDiagnostics:
            def log(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

        class _FakeStorage:
            def __init__(self) -> None:
                self.session: dict[str, object] | None = None
                self.agents: list[dict[str, object]] = []

            def load_session(self, _session_id: str):  # type: ignore[no-untyped-def]
                return dict(self.session) if self.session else None

            def load_agents(self, _session_id: str):  # type: ignore[no-untyped-def]
                return [dict(item) for item in self.agents]

        class _FakeOrchestrator:
            def __init__(self, *_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                self.locale = kwargs.get("locale") or "en"
                self.diagnostics = _FakeDiagnostics()
                self.storage = _FakeStorage()
                self.latest_session_id: str | None = None
                self._subscriber = None

            def subscribe(self, callback):  # type: ignore[no-untyped-def]
                self._subscriber = callback

            def load_session_context(self, session_id: str):  # type: ignore[no-untyped-def]
                return SimpleNamespace(id=str(session_id))

            async def run_task(self, _task: str):  # type: ignore[no-untyped-def]
                self.latest_session_id = "session-123"
                self.storage.session = {"id": "session-123", "status": "running"}
                self.storage.agents = [
                    CliRunResumePanelTests._fake_agent_row(
                        agent_id="agent-root-abcdef",
                        role="root",
                        status="running",
                        instruction="Inspect repository and provide next engineering steps.",
                        summary="",
                        parent_agent_id=None,
                        children=["agent-child-1234"],
                        model="openai/gpt-4.1",
                        step_count=2,
                    ),
                    CliRunResumePanelTests._fake_agent_row(
                        agent_id="agent-child-1234",
                        role="worker",
                        status="running",
                        instruction="Search recent failures and summarize root cause.",
                        summary="",
                        parent_agent_id="agent-root-abcdef",
                        children=[],
                        model="openai/gpt-4.1",
                        step_count=5,
                    ),
                ]
                if self._subscriber:
                    self._subscriber(
                        {
                            "session_id": "session-123",
                            "event_type": "session_started",
                            "payload": {"session_status": "running"},
                        }
                    )
                await asyncio.sleep(0.03)
                self.storage.agents[0]["summary"] = "Root summary complete."
                self.storage.agents[0]["status"] = "completed"
                self.storage.agents[1]["summary"] = "Cancelled after parent finish."
                self.storage.agents[1]["status"] = "cancelled"
                self.storage.agents[1]["completion_status"] = None
                self.storage.session["status"] = "completed"
                if self._subscriber:
                    self._subscriber(
                        {
                            "session_id": "session-123",
                            "event_type": "session_finalized",
                            "payload": {"session_status": "completed"},
                        }
                    )
                return SimpleNamespace(
                    id="session-123",
                    root_agent_id="agent-root-abcdef",
                    status=SimpleNamespace(value="completed"),
                    completion_state="completed",
                    final_summary="All done.",
                )

            async def resume(self, session_id: str, instruction: str):  # type: ignore[no-untyped-def]
                self.latest_session_id = session_id
                self.storage.session = {"id": session_id, "status": "running"}
                self.storage.agents = [
                    CliRunResumePanelTests._fake_agent_row(
                        agent_id="agent-root-abcdef",
                        role="root",
                        status="running",
                        instruction="Resume and finalize the task.",
                        summary="",
                        parent_agent_id=None,
                        children=[],
                        model="openai/gpt-4.1",
                        step_count=4,
                    )
                ]
                if self._subscriber:
                    self._subscriber(
                        {
                            "session_id": session_id,
                            "event_type": "session_resumed",
                            "payload": {
                                "session_status": "running",
                                "instruction": instruction,
                            },
                        }
                    )
                await asyncio.sleep(0.03)
                self.storage.agents[0]["summary"] = "Resume done."
                self.storage.agents[0]["status"] = "completed"
                self.storage.session["status"] = "completed"
                if self._subscriber:
                    self._subscriber(
                        {
                            "session_id": session_id,
                            "event_type": "session_finalized",
                            "payload": {"session_status": "completed"},
                        }
                    )
                return SimpleNamespace(
                    id=session_id,
                    root_agent_id="agent-root-abcdef",
                    status=SimpleNamespace(value="completed"),
                    completion_state="completed",
                    final_summary="Session resumed.",
                )

            def project_sync_status(self, _session_id: str):  # type: ignore[no-untyped-def]
                return None

        return _FakeOrchestrator

    def test_run_non_tty_keeps_static_output(self) -> None:
        output = io.StringIO()
        with patch.object(cli, "Orchestrator", self._fake_orchestrator_class()):
            with patch.object(cli, "_RUN_STATUS_REFRESH_SECONDS", 0.01):
                with redirect_stdout(output):
                    asyncio.run(
                        cli._run_task(
                            project_dir=Path("."),
                            app_dir=None,
                            locale="en",
                            task="inspect repo",
                            debug=False,
                        )
                    )
        rendered = output.getvalue()
        self.assertIn("All done.", rendered)
        self.assertIn("session_id=session-123", rendered)
        self.assertIn("session_status=completed", rendered)
        self.assertIn("completion_state=completed", rendered)
        self.assertNotIn("goal=", rendered)
        self.assertNotIn("\x1b[?25l", rendered)

    def test_run_passes_workspace_mode_model_root_agent_name_and_backend(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")

            class _FakeOrchestrator:
                def __init__(self, project_dir, locale=None, app_dir=None, debug=False):  # type: ignore[no-untyped-def]
                    del project_dir, locale, debug
                    self.locale = "en"
                    self.app_dir = (
                        Path(app_dir).resolve()
                        if app_dir is not None
                        else Path(temp_dir).resolve()
                    )
                    self.config = SimpleNamespace(sandbox=SimpleNamespace(backend="anthropic"))
                    self.tool_executor = SimpleNamespace(
                        sandbox_backend_cls="backend::anthropic",
                        _shell_backend_instance="cached",
                    )
                    self.diagnostics = SimpleNamespace(log=lambda **kwargs: None)
                    captured["orchestrator"] = self

                async def run_task(self, task: str, **kwargs):  # type: ignore[no-untyped-def]
                    captured["task"] = task
                    captured["run_kwargs"] = dict(kwargs)
                    return SimpleNamespace(
                        id="session-123",
                        root_agent_id="agent-root",
                        status=SimpleNamespace(value="completed"),
                        completion_state="completed",
                        final_summary="done",
                    )

                def project_sync_status(self, _session_id: str):  # type: ignore[no-untyped-def]
                    return {"status": "disabled"}

            with (
                patch.object(cli, "Orchestrator", _FakeOrchestrator),
                patch.object(
                    cli,
                    "resolve_sandbox_backend_cls",
                    side_effect=lambda sandbox: f"backend::{sandbox.backend}",
                ),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    asyncio.run(
                        cli._run_task(
                            project_dir=Path("."),
                            app_dir=app_dir,
                            locale="en",
                            task="inspect repo",
                            debug=False,
                            workspace_mode="staged",
                            model="fake/model",
                            root_agent_name="Demo Root",
                            enabled_mcp_server_ids=["filesystem", "docs"],
                            sandbox_backend="none",
                        )
                    )

        self.assertEqual(captured["task"], "inspect repo")
        self.assertEqual(
            captured["run_kwargs"],
            {
                "model": "fake/model",
                "root_agent_name": "Demo Root",
                "workspace_mode": "staged",
                "enabled_mcp_server_ids": ["filesystem", "docs"],
            },
        )
        orchestrator = captured["orchestrator"]
        self.assertEqual(orchestrator.config.sandbox.backend, "none")
        self.assertEqual(orchestrator.tool_executor.sandbox_backend_cls, "backend::none")
        self.assertIsNone(orchestrator.tool_executor._shell_backend_instance)

    def test_run_defaults_sandbox_backend_from_config_when_omitted(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                '[sandbox]\nbackend = "none"\n',
                encoding="utf-8",
            )

            class _FakeOrchestrator:
                def __init__(self, project_dir, locale=None, app_dir=None, debug=False):  # type: ignore[no-untyped-def]
                    del project_dir, locale, debug
                    self.locale = "en"
                    self.app_dir = (
                        Path(app_dir).resolve()
                        if app_dir is not None
                        else Path(temp_dir).resolve()
                    )
                    self.config = SimpleNamespace(sandbox=SimpleNamespace(backend="anthropic"))
                    self.tool_executor = SimpleNamespace(
                        sandbox_backend_cls="backend::anthropic",
                        _shell_backend_instance="cached",
                    )
                    self.diagnostics = SimpleNamespace(log=lambda **kwargs: None)
                    captured["orchestrator"] = self

                async def run_task(self, task: str, **kwargs):  # type: ignore[no-untyped-def]
                    del task, kwargs
                    return SimpleNamespace(
                        id="session-123",
                        root_agent_id="agent-root",
                        status=SimpleNamespace(value="completed"),
                        completion_state="completed",
                        final_summary="done",
                    )

                def project_sync_status(self, _session_id: str):  # type: ignore[no-untyped-def]
                    return {"status": "disabled"}

            with (
                patch.object(cli, "Orchestrator", _FakeOrchestrator),
                patch.object(
                    cli,
                    "resolve_sandbox_backend_cls",
                    side_effect=lambda sandbox: f"backend::{sandbox.backend}",
                ),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    asyncio.run(
                        cli._run_task(
                            project_dir=Path("."),
                            app_dir=app_dir,
                            locale="en",
                            task="inspect repo",
                            debug=False,
                        )
                    )

        orchestrator = captured["orchestrator"]
        self.assertEqual(orchestrator.config.sandbox.backend, "none")
        self.assertEqual(orchestrator.tool_executor.sandbox_backend_cls, "backend::none")
        self.assertIsNone(orchestrator.tool_executor._shell_backend_instance)

    def test_resume_passes_model_and_applies_sandbox_backend(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")

            class _FakeOrchestrator:
                def __init__(self, project_dir, locale=None, app_dir=None, debug=False):  # type: ignore[no-untyped-def]
                    del project_dir, locale, debug
                    self.locale = "en"
                    self.app_dir = (
                        Path(app_dir).resolve()
                        if app_dir is not None
                        else Path(temp_dir).resolve()
                    )
                    self.config = SimpleNamespace(sandbox=SimpleNamespace(backend="anthropic"))
                    self.tool_executor = SimpleNamespace(
                        sandbox_backend_cls="backend::anthropic",
                        _shell_backend_instance="cached",
                    )
                    self.diagnostics = SimpleNamespace(log=lambda **kwargs: None)
                    captured["orchestrator"] = self

                async def resume(self, session_id: str, instruction: str, **kwargs):  # type: ignore[no-untyped-def]
                    captured["resumed_session_id"] = session_id
                    captured["instruction"] = instruction
                    captured["resume_kwargs"] = dict(kwargs)
                    captured["resume_backend"] = self.config.sandbox.backend
                    captured["resume_backend_cls"] = self.tool_executor.sandbox_backend_cls
                    return SimpleNamespace(
                        id=session_id,
                        root_agent_id="agent-root",
                        status=SimpleNamespace(value="completed"),
                        completion_state="completed",
                        final_summary="Session resumed.",
                    )

                def project_sync_status(self, _session_id: str):  # type: ignore[no-untyped-def]
                    return {"status": "disabled"}

            with (
                patch.object(cli, "Orchestrator", _FakeOrchestrator),
                patch.object(
                    cli,
                    "resolve_sandbox_backend_cls",
                    side_effect=lambda sandbox: f"backend::{sandbox.backend}",
                ),
                patch.object(cli, "_maybe_prompt_remote_password_for_session", return_value=None),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    asyncio.run(
                        cli._resume(
                            app_dir=app_dir,
                            locale="en",
                            session_id="session-123",
                            instruction="continue from latest status",
                            debug=False,
                            model="fake/model",
                            enabled_mcp_server_ids=["filesystem"],
                            sandbox_backend="none",
                        )
                    )

        self.assertEqual(captured["resumed_session_id"], "session-123")
        self.assertEqual(captured["instruction"], "continue from latest status")
        self.assertEqual(
            captured["resume_kwargs"],
            {"model": "fake/model", "enabled_mcp_server_ids": ["filesystem"]},
        )
        self.assertEqual(captured["resume_backend"], "none")
        self.assertEqual(captured["resume_backend_cls"], "backend::none")
        orchestrator = captured["orchestrator"]
        self.assertIsNone(orchestrator.tool_executor._shell_backend_instance)

    def test_skills_command_prints_json_and_forwards_remote_args(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()

            class _FakeOrchestrator:
                def __init__(self, project_dir, locale=None, app_dir=None, debug=False):  # type: ignore[no-untyped-def]
                    captured["init"] = {
                        "project_dir": project_dir,
                        "locale": locale,
                        "app_dir": app_dir,
                        "debug": debug,
                    }

                async def discover_skills(self, **kwargs):  # type: ignore[no-untyped-def]
                    captured["discover_kwargs"] = kwargs
                    return [{"id": "skill-a"}]

            remote_config = SimpleNamespace(remote_dir="/home/demo/workspace")

            with patch.object(cli, "Orchestrator", _FakeOrchestrator):
                output = io.StringIO()
                with redirect_stdout(output):
                    asyncio.run(
                        cli._skills(
                            project_dir=project_dir,
                            app_dir=app_dir,
                            locale="en",
                            debug=False,
                            remote_config=remote_config,  # type: ignore[arg-type]
                            remote_password="secret",
                        )
                    )

        self.assertEqual(json.loads(output.getvalue()), [{"id": "skill-a"}])
        self.assertEqual(
            captured["discover_kwargs"],
            {
                "project_dir": None,
                "remote_config": remote_config,
                "remote_password": "secret",
            },
        )

    def test_mcp_servers_command_prints_json_and_cleans_up_remote_runtime(self) -> None:
        captured: dict[str, object] = {"events": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()

            class _FakeMcpManager:
                async def inspect_servers(self, **kwargs):  # type: ignore[no-untyped-def]
                    captured["inspect_kwargs"] = kwargs
                    return [{"id": "filesystem"}]

                async def close_session(self, session_id: str) -> None:
                    captured["closed_session_id"] = session_id

            class _FakeOrchestrator:
                def __init__(self, project_dir, locale=None, app_dir=None, debug=False):  # type: ignore[no-untyped-def]
                    del project_dir, locale, debug
                    self.app_dir = app_dir
                    self.config = SimpleNamespace(sandbox=SimpleNamespace(backend="anthropic"))
                    self.tool_executor = SimpleNamespace(
                        cleanup_session_remote_runtime=lambda session_id: captured.setdefault(
                            "cleaned_session_ids", []
                        ).append(session_id)
                    )
                    self.mcp_manager = _FakeMcpManager()
                    self.diagnostics = SimpleNamespace(
                        log=lambda **kwargs: captured["events"].append(kwargs)
                    )

                def _apply_session_remote_runtime(self, **kwargs):  # type: ignore[no-untyped-def]
                    captured["remote_runtime_kwargs"] = kwargs

            remote_config = SimpleNamespace(remote_dir="/home/demo/workspace")

            with patch.object(cli, "Orchestrator", _FakeOrchestrator):
                output = io.StringIO()
                with redirect_stdout(output):
                    asyncio.run(
                        cli._mcp_servers(
                            project_dir=project_dir,
                            app_dir=app_dir,
                            locale="en",
                            debug=False,
                            enabled_mcp_server_ids=["filesystem"],
                            remote_config=remote_config,  # type: ignore[arg-type]
                            remote_password="secret",
                        )
                    )

        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered, [{"id": "filesystem"}])
        inspect_kwargs = captured["inspect_kwargs"]
        assert isinstance(inspect_kwargs, dict)
        self.assertEqual(inspect_kwargs["enabled_server_ids"], ["filesystem"])
        self.assertEqual(inspect_kwargs["workspace_path"], Path("/home/demo/workspace"))
        self.assertTrue(inspect_kwargs["workspace_is_remote"])
        self.assertTrue(str(captured["closed_session_id"]).startswith("mcp-inspect-"))
        self.assertEqual(
            captured["cleaned_session_ids"],
            [captured["closed_session_id"]],
        )

    def test_clone_command_calls_explicit_clone_and_prints_ids(self) -> None:
        captured: dict[str, object] = {"events": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")

            class _FakeOrchestrator:
                def __init__(self, project_dir, locale=None, app_dir=None, debug=False):  # type: ignore[no-untyped-def]
                    del project_dir, locale, debug
                    self.app_dir = Path(app_dir).resolve() if app_dir is not None else Path(temp_dir).resolve()
                    self.diagnostics = SimpleNamespace(log=lambda **kwargs: captured["events"].append(kwargs))

                def clone_session(self, session_id: str):  # type: ignore[no-untyped-def]
                    captured["cloned_from"] = session_id
                    return SimpleNamespace(
                        id="session-clone-456",
                        status=SimpleNamespace(value="interrupted"),
                    )

            with patch.object(cli, "Orchestrator", _FakeOrchestrator):
                output = io.StringIO()
                with redirect_stdout(output):
                    cli._clone_session(
                        app_dir=app_dir,
                        locale="en",
                        session_id="session-123",
                        debug=False,
                    )

        rendered = output.getvalue()
        self.assertEqual(captured["cloned_from"], "session-123")
        self.assertIn("source_session_id=session-123", rendered)
        self.assertIn("session_id=session-clone-456", rendered)
        self.assertIn("session_status=interrupted", rendered)
        event_types = [str(item.get("event_type", "")) for item in captured["events"]]
        self.assertEqual(
            event_types,
            ["clone_command_started", "clone_command_finished"],
        )

    def test_run_tty_panel_includes_agent_fields(self) -> None:
        output = self._TTYBuffer()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            with patch.object(cli, "Orchestrator", self._fake_orchestrator_class()):
                with patch.object(cli, "_RUN_STATUS_REFRESH_SECONDS", 0.01):
                    with patch.object(sys, "stdout", output):
                        asyncio.run(
                            cli._run_task(
                                project_dir=Path("."),
                                app_dir=None,
                                locale="en",
                                task="inspect repo",
                                debug=False,
                            )
                        )
        rendered = output.getvalue()
        self.assertIn("mode=run", rendered)
        self.assertIn("session=session-123", rendered)
        self.assertIn("[Root Coordinator(agent-root-abcdef), Worker Agent(agent-child-1234)]", rendered)
        self.assertIn("goal=", rendered)
        self.assertIn("summary=", rendered)
        self.assertIn("step=", rendered)
        self.assertIn("active=", rendered)
        self.assertIn("latest=", rendered)
        self.assertIn("tools=", rendered)
        self.assertIn("out_tok=", rendered)
        self.assertIn("model=", rendered)
        self.assertIn("parent=", rendered)
        self.assertIn("children=", rendered)
        self.assertRegex(rendered, r"\n(?:\x1b\[[0-9;]*m)*  stats=")
        self.assertRegex(rendered, r"\n(?:\x1b\[[0-9;]*m)*  lineage=")
        self.assertRegex(rendered, r"\n(?:\x1b\[[0-9;]*m)*  latest=")
        self.assertRegex(rendered, r"\n(?:\x1b\[[0-9;]*m)*  goal=")
        self.assertRegex(rendered, r"\n(?:\x1b\[[0-9;]*m)*  summary=")
        self.assertIn("cancelled", rendered)
        self.assertIn("All done.", rendered)
        self.assertRegex(rendered, r"\x1b\[[0-9;]*m")

    def test_agent_view_keeps_terminated_status_without_completion_override(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        row = self._fake_agent_row(
            agent_id="agent-child-1234",
            role="worker",
            status="terminated",
            instruction="Cancelled after parent finish.",
            summary="Cancelled after parent finish.",
            parent_agent_id="agent-root-abcdef",
            children=[],
            completion_status="cancelled",
            step_count=5,
        )
        agent = panel._agent_view_from_row(
            row,
            last_event_at_by_agent={},
            tool_stats_by_agent={},
            message_stats_by_agent={},
        )
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertEqual(agent.status, "terminated")

    def test_agent_view_reads_model_from_metadata(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        row = self._fake_agent_row(
            agent_id="agent-child-1234",
            role="worker",
            status="running",
            instruction="Inspect repository",
            summary="",
            parent_agent_id="agent-root-abcdef",
            children=[],
            model="openai/gpt-4.1-mini",
        )
        agent = panel._agent_view_from_row(
            row,
            last_event_at_by_agent={},
            tool_stats_by_agent={},
            message_stats_by_agent={},
        )
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertEqual(agent.model, "openai/gpt-4.1-mini")

    def test_run_tty_zh_locale_and_no_color(self) -> None:
        output = self._TTYBuffer()
        with patch.object(cli, "Orchestrator", self._fake_orchestrator_class()):
            with patch.object(cli, "_RUN_STATUS_REFRESH_SECONDS", 0.01):
                with patch.dict(os.environ, {"NO_COLOR": "1"}):
                    with patch.object(sys, "stdout", output):
                        asyncio.run(
                            cli._run_task(
                                project_dir=Path("."),
                                app_dir=None,
                                locale="zh",
                                task="检查仓库",
                                debug=False,
                            )
                        )
        rendered = output.getvalue()
        self.assertIn("模式=运行", rendered)
        self.assertIn("目标=", rendered)
        self.assertIn("总结=", rendered)
        self.assertIn("步骤=", rendered)
        self.assertIn("活动=", rendered)
        self.assertIn("最新活动=", rendered)
        self.assertIn("工具=", rendered)
        self.assertIn("输出Token=", rendered)
        self.assertIn("模型=", rendered)
        self.assertIn("父=", rendered)
        self.assertIn("子=", rendered)
        self.assertIn("  概览=", rendered)
        self.assertIn("  关系=", rendered)
        self.assertIn("  最新活动=", rendered)
        self.assertIn("  工具=", rendered)
        self.assertIn("  输出Token=", rendered)
        self.assertIn("  模型=", rendered)
        self.assertIn("  目标=", rendered)
        self.assertIn("  总结=", rendered)
        self.assertIsNone(re.search(r"\x1b\[[0-9;]*m", rendered))

    def test_resume_tty_panel_uses_resume_mode(self) -> None:
        output = self._TTYBuffer()
        with patch.object(cli, "Orchestrator", self._fake_orchestrator_class()):
            with patch.object(cli, "_RUN_STATUS_REFRESH_SECONDS", 0.01):
                with patch.object(sys, "stdout", output):
                    asyncio.run(
                        cli._resume(
                            app_dir=None,
                            locale="en",
                            session_id="session-123",
                            instruction="continue from latest status",
                            debug=False,
                        )
                    )
        rendered = output.getvalue()
        self.assertIn("mode=resume", rendered)
        self.assertIn("session=session-123", rendered)
        self.assertIn("Session resumed.", rendered)
        self.assertIn("session_id=session-123", rendered)

    def test_detail_lines_wrap_long_goal_and_summary(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        goal_lines = panel._detail_lines(
            label="goal",
            value=(
                "Investigate root cause across coordinator and workers, "
                "then propose a minimal rollback-safe remediation plan."
            ),
            max_total_width=36,
            max_lines=3,
        )
        summary_lines = panel._detail_lines(
            label="summary",
            value=(
                "Draft summary is intentionally long so we can verify continuation lines "
                "are rendered and prefixed correctly."
            ),
            max_total_width=36,
            max_lines=3,
        )
        self.assertGreater(len(goal_lines), 1)
        self.assertGreater(len(summary_lines), 1)
        self.assertTrue(goal_lines[0].startswith("  goal="))
        self.assertTrue(summary_lines[0].startswith("  summary="))
        self.assertTrue(goal_lines[1].startswith("       "))
        self.assertTrue(summary_lines[1].startswith("          "))

    def test_detail_lines_adapt_to_narrow_width(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        lines = panel._detail_lines(
            label="summary",
            value=(
                "Narrow terminal compatibility validation for summary content "
                "that should wrap without visual overflow."
            ),
            max_total_width=10,
            max_lines=4,
        )
        for line in lines:
            self.assertLessEqual(cli._display_width(line), 10)

    def test_detail_lines_adapt_to_narrow_width_with_cjk_labels(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="zh",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        lines = panel._detail_lines(
            label="总结",
            value="这是一个用于验证窄窗口下换行和前缀宽度计算的长文本内容",
            max_total_width=12,
            max_lines=4,
        )
        for line in lines:
            self.assertLessEqual(cli._display_width(line), 12)

    def test_preview_chars_limits_each_content(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
            preview_chars=32,
        )
        lines = panel._detail_lines(
            label="goal",
            value=(
                "This goal contains far more than thirty-two characters "
                "and should be truncated with ellipsis."
            ),
            max_total_width=80,
            max_lines=6,
        )
        rendered = "\n".join(lines)
        self.assertIn("...", rendered)

    def test_record_output_tokens_supports_multiple_usage_shapes(self) -> None:
        self.assertEqual(
            cli._RunStatusPanel._record_output_tokens(
                {"response": {"usage": {"output_tokens": 12, "input_tokens": 8}}}
            ),
            12,
        )
        self.assertEqual(
            cli._RunStatusPanel._record_output_tokens(
                {"response": {"usage": {"completion_tokens": 7, "prompt_tokens": 6}}}
            ),
            7,
        )
        self.assertEqual(
            cli._RunStatusPanel._record_output_tokens(
                {"response": {"usage": {"total_tokens": 30, "prompt_tokens": 11}}}
            ),
            19,
        )

    def test_message_stats_extract_latest_message_preview(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "agent-1_messages.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-03-12T10:00:00+00:00",
                                "role": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": "first response",
                                },
                                "response": {"usage": {"output_tokens": 12}},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-03-12T10:00:05+00:00",
                                "role": "tool",
                                "message": {
                                    "role": "tool",
                                    "content": "{\"status\": \"ok\"}",
                                },
                                "response": {"usage": {"completion_tokens": 3}},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            metrics = panel._message_stats_for_path(path)
        self.assertEqual(int(metrics["output_tokens"]), 15)
        self.assertEqual(str(metrics["last_activity_at"]), "2026-03-12T10:00:05+00:00")
        self.assertIn("tool:", str(metrics["latest_message"]))

    def test_build_lines_applies_distinct_content_type_colors(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        agent = cli._AgentView(
            id="agent-root-abcdef",
            name="Root Coordinator",
            role="root",
            status="running",
            step_count=3,
            last_activity_at="2026-01-01T00:00:00+00:00",
            latest_message="assistant: completed initial analysis",
            running_tool_runs=1,
            queued_tool_runs=2,
            failed_tool_runs=0,
            output_tokens=321,
            parent_agent_id=None,
            children=["agent-child-1234"],
            goal="Investigate failures and produce a patch plan.",
            summary="Working on analysis.",
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            panel.use_color = True
            lines = panel._build_lines(
                session_id="session-123",
                session_status="running",
                agents=[agent],
            )
        joined = "\n".join(lines)
        self.assertRegex(joined, r"\x1b\[[0-9;]*m")
        goal_line = next(line for line in lines if "goal=" in line)
        summary_line = next(line for line in lines if "summary=" in line)
        parent_line = next(line for line in lines if "parent=" in line)
        stats_line = next(line for line in lines if "stats=" in line)
        self.assertNotEqual(goal_line, summary_line)
        self.assertIn("\x1b[33m", goal_line)
        self.assertIn("\x1b[32m", summary_line)
        self.assertIn("\x1b[34m", parent_line)
        self.assertIn("\x1b[94m", stats_line)
        latest_line = next(line for line in lines if "latest=" in line)
        tools_line = next(line for line in lines if "tools=" in line)
        tokens_line = next(line for line in lines if "out_tok=" in line)
        self.assertIn("\x1b[35m", latest_line)
        self.assertIn("\x1b[96m", tools_line)
        self.assertIn("\x1b[92m", tokens_line)

    def test_agent_catalog_wraps_without_preview_truncation(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
            preview_chars=16,
        )
        panel.use_color = False
        agents = [
            cli._AgentView(
                id="agent-root-abcdef1234567890",
                name="Root Coordinator Alpha",
                role="root",
                status="running",
                step_count=1,
                last_activity_at="2026-01-01T00:00:00+00:00",
                latest_message="-",
                running_tool_runs=0,
                queued_tool_runs=0,
                failed_tool_runs=0,
                output_tokens=0,
                parent_agent_id=None,
                children=["agent-worker-1234567890abcdef"],
                goal="-",
                summary="-",
            ),
            cli._AgentView(
                id="agent-worker-1234567890abcdef",
                name="Worker Agent Beta",
                role="worker",
                status="pending",
                step_count=0,
                last_activity_at=None,
                latest_message="-",
                running_tool_runs=0,
                queued_tool_runs=0,
                failed_tool_runs=0,
                output_tokens=0,
                parent_agent_id="agent-root-abcdef1234567890",
                children=[],
                goal="-",
                summary="-",
            ),
        ]
        lines = panel._build_lines(
            session_id="session-123",
            session_status="running",
            agents=agents,
            max_total_width=38,
        )
        self.assertEqual(panel._panel_header_line_count, 2)
        first_agent_index = next(idx for idx, line in enumerate(lines) if " [root]" in line)
        catalog_lines = lines[2:first_agent_index]
        self.assertGreater(len(catalog_lines), 1)
        catalog_text = "".join(catalog_lines)
        self.assertIn("Root Coordinator Alpha(agent-root-abcdef1234567890)", catalog_text)
        self.assertIn("Worker Agent Beta(agent-worker-1234567890abcdef)", catalog_text)
        self.assertNotIn("...", catalog_text)

    def test_paginate_for_terminal_keeps_header_and_limits_rows(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        lines = ["header-1", "header-2"] + [f"body-{i}" for i in range(8)]
        page0, index0, pages = panel._paginate_for_terminal(
            lines,
            terminal_columns=80,
            terminal_rows=6,
            force_page_index=0,
        )
        page1, index1, _ = panel._paginate_for_terminal(
            lines,
            terminal_columns=80,
            terminal_rows=6,
            force_page_index=1,
        )
        self.assertEqual(pages, 3)
        self.assertEqual(index0, 0)
        self.assertEqual(index1, 1)
        self.assertEqual(page0[:2], ["header-1", "header-2"])
        self.assertEqual(page1[:2], ["header-1", "header-2"])
        self.assertLessEqual(len(page0), 5)
        self.assertLessEqual(len(page1), 5)
        self.assertIn("body-0", page0)
        self.assertIn("body-3", page1)

    def test_paginate_for_terminal_supports_keyboard_page_shift(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        lines = ["header-1", "header-2"] + [f"body-{i}" for i in range(8)]
        panel._apply_page_key_chunk(b"=")
        _, index_next, pages = panel._paginate_for_terminal(
            lines,
            terminal_columns=80,
            terminal_rows=6,
        )
        panel._apply_page_key_chunk(b"-")
        _, index_prev, _ = panel._paginate_for_terminal(
            lines,
            terminal_columns=80,
            terminal_rows=6,
        )
        self.assertEqual(pages, 3)
        self.assertEqual(index_next, 1)
        self.assertEqual(index_prev, 0)

    def test_paginate_keyboard_shift_uses_current_auto_page_as_base(self) -> None:
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=self._TTYBuffer(),
            refresh_seconds=0.01,
        )
        lines = ["header-1", "header-2"] + [f"body-{i}" for i in range(8)]
        with patch.object(panel, "_current_page_index", return_value=2):
            panel._apply_page_key_chunk(b"=")
            _, index_next, pages = panel._paginate_for_terminal(
                lines,
                terminal_columns=80,
                terminal_rows=6,
            )
            _, index_sticky, _ = panel._paginate_for_terminal(
                lines,
                terminal_columns=80,
                terminal_rows=6,
            )
        self.assertEqual(pages, 3)
        self.assertEqual(index_next, 0)
        self.assertEqual(index_sticky, 0)

    def test_render_once_includes_page_hint_when_paginated(self) -> None:
        output = self._TTYBuffer()
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=output,
            refresh_seconds=0.01,
        )
        agents = [
            cli._AgentView(
                id=f"agent-{idx:02d}-abcdef",
                name=f"Worker {idx}",
                role="worker",
                status="running",
                step_count=idx,
                last_activity_at="2026-01-01T00:00:00+00:00",
                latest_message="assistant: working",
                running_tool_runs=1,
                queued_tool_runs=0,
                failed_tool_runs=0,
                output_tokens=10 + idx,
                parent_agent_id="agent-root-abcdef",
                children=[],
                goal="Do a long-running task for pagination checks.",
                summary="-",
            )
            for idx in range(4)
        ]
        with patch.object(panel, "_snapshot", return_value=("session-123", "running", agents)):
            with patch.object(panel, "_terminal_columns", return_value=80):
                with patch.object(panel, "_terminal_rows", return_value=8):
                    panel._render_once()
        rendered = output.getvalue()
        self.assertIn("page=", rendered)
        self.assertIn("(-/=)", rendered)

    def test_render_once_rewinds_by_visual_rows(self) -> None:
        output = self._TTYBuffer()
        panel = cli._RunStatusPanel(
            mode="run",
            locale="en",
            stream=output,
            refresh_seconds=0.01,
        )
        with patch.object(panel, "_snapshot", return_value=("session-123", "running", [])):
            with patch.object(panel, "_terminal_columns", return_value=12):
                with patch.object(panel, "_build_lines", return_value=["x" * 25, "y"]):
                    panel._render_once()
                    first_rows = panel._printed_rows
                    panel._render_once()
        rendered = output.getvalue()
        self.assertGreater(first_rows, 2)
        self.assertIn(f"\x1b[{first_rows}F", rendered)
