from __future__ import annotations

import asyncio
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from opencompany.models import (
    RemoteSessionConfig,
    RemoteShellContext,
    RunSession,
    SessionStatus,
    ShellCommandRequest,
    ShellCommandResult,
    WorkspaceMode,
)
from opencompany.orchestrator import Orchestrator
from opencompany.remote import save_remote_session_config
from opencompany.webui import state as webui_state
from opencompany.webui.events import EventHub, collapse_stream_events
from opencompany.webui.server import create_webui_app
from opencompany.webui.state import WebUIRuntimeState


class WebUIEventsTests(unittest.TestCase):
    def test_collapse_stream_events_merges_adjacent_reasoning_chunks(self) -> None:
        records = [
            {
                "event_type": "llm_reasoning",
                "session_id": "s1",
                "agent_id": "a1",
                "phase": "llm",
                "payload": {"token": "hello "},
                "timestamp": "2026-03-10T10:00:00Z",
            },
            {
                "event_type": "llm_reasoning",
                "session_id": "s1",
                "agent_id": "a1",
                "phase": "llm",
                "payload": {"token": "world"},
                "timestamp": "2026-03-10T10:00:01Z",
            },
        ]
        collapsed = collapse_stream_events(records)
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["payload"]["token"], "hello world")
        self.assertEqual(collapsed[0]["timestamp"], "2026-03-10T10:00:01Z")

    def test_collapse_stream_events_keeps_separate_agents(self) -> None:
        records = [
            {
                "event_type": "llm_token",
                "session_id": "s1",
                "agent_id": "a1",
                "phase": "llm",
                "payload": {"token": "A"},
            },
            {
                "event_type": "llm_token",
                "session_id": "s1",
                "agent_id": "a2",
                "phase": "llm",
                "payload": {"token": "B"},
            },
        ]
        collapsed = collapse_stream_events(records)
        self.assertEqual(len(collapsed), 2)

    def test_event_hub_drops_oldest_when_subscriber_queue_is_full(self) -> None:
        hub = EventHub(queue_size=2)
        queue = hub.subscribe()
        hub.publish({"event_type": "first"})
        hub.publish({"event_type": "second"})
        hub.publish({"event_type": "third"})

        self.assertEqual(queue.qsize(), 2)
        first = queue.get_nowait()
        second = queue.get_nowait()
        self.assertEqual(first["event_type"], "second")
        self.assertEqual(second["event_type"], "third")


class WebUIStateTests(unittest.TestCase):
    def test_set_launch_config_accepts_project_or_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_dir = app_dir / ".opencompany" / "sessions" / "session-1"
            session_dir.mkdir(parents=True, exist_ok=True)
            project_dir = app_dir / "project"
            project_dir.mkdir()

            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )
            state.current_session_id = "old-session"
            state.current_task = "old task"
            state.current_session_status = "completed"
            state.current_summary = "old summary"
            state.set_launch_config(
                project_dir=str(project_dir),
                session_id=None,
                session_mode="staged",
            )
            self.assertEqual(state.project_dir, project_dir.resolve())
            self.assertTrue(state.launch_config().can_run())
            self.assertEqual(state.launch_config().session_mode, WorkspaceMode.STAGED)
            self.assertFalse(state.launch_config().session_mode_locked)
            self.assertIsNone(state.current_session_id)
            self.assertEqual(state.current_task, "")
            self.assertEqual(state.current_session_status, "idle")
            self.assertEqual(state.current_summary, "")

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.app_dir = app_dir

                def load_session_context(self, session_id: str) -> RunSession:
                    del session_id
                    return RunSession(
                        id="session-1",
                        project_dir=project_dir,
                        task="loaded task",
                        locale="en",
                        root_agent_id="agent-root",
                        workspace_mode=WorkspaceMode.DIRECT,
                        status=SessionStatus.INTERRUPTED,
                    )

            state._read_orchestrator = lambda _project_dir: _FakeOrchestrator()  # type: ignore[method-assign]
            state.set_launch_config(project_dir=None, session_id="session-1")
            self.assertEqual(state.configured_resume_session_id, "session-1")
            self.assertEqual(state.current_session_id, "session-1")
            self.assertEqual(state.project_dir, project_dir.resolve())
            self.assertTrue(state.launch_config().can_resume())
            self.assertEqual(state.launch_config().session_mode, WorkspaceMode.DIRECT)
            self.assertTrue(state.launch_config().session_mode_locked)

    def test_sandbox_backend_defaults_to_config_and_can_be_overridden_per_launch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            project_dir = app_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)
            (app_dir / "opencompany.toml").write_text(
                """
[sandbox]
backend = "none"
""".strip(),
                encoding="utf-8",
            )
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )
            self.assertEqual(state.launch_config().sandbox_backend_default, "none")
            self.assertEqual(state.launch_config().sandbox_backend, "none")
            state.set_launch_config(
                project_dir=str(project_dir),
                session_id=None,
                sandbox_backend="anthropic",
            )
            self.assertEqual(state.launch_config().sandbox_backend, "anthropic")

    def test_set_launch_config_reuses_in_memory_remote_password(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )

            remote_payload = {
                "kind": "remote_ssh",
                "ssh_target": "demo@example.com:22",
                "remote_dir": "/home/demo/workspace",
                "auth_mode": "password",
                "known_hosts_policy": "accept_new",
                "remote_os": "linux",
            }
            state.set_launch_config(
                project_dir=None,
                session_id=None,
                session_mode="direct",
                remote=remote_payload,
                remote_password="secret-pass",
            )
            self.assertEqual(state.remote_password, "secret-pass")
            snapshot = state.snapshot()
            self.assertIsNone(snapshot["launch_config"]["project_dir"])
            self.assertEqual(
                snapshot["launch_config"]["project_dir_display"],
                "/home/demo/workspace",
            )
            self.assertTrue(snapshot["launch_config"]["project_dir_is_remote"])

            state.set_launch_config(
                project_dir=None,
                session_id=None,
                session_mode="direct",
                remote=remote_payload,
                remote_password=None,
            )
            self.assertEqual(state.remote_password, "secret-pass")

            state.set_launch_config(
                project_dir=None,
                session_id=None,
                session_mode="direct",
                remote=None,
                remote_password=None,
            )
            self.assertEqual(state.remote_password, "")

    def test_validate_remote_session_load_skips_check_for_none_backend(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_dir = app_dir / ".opencompany" / "sessions" / "session-1"
                session_dir.mkdir(parents=True, exist_ok=True)
                save_remote_session_config(
                    session_dir,
                    RemoteSessionConfig(
                        kind="remote_ssh",
                        ssh_target="demo@example.com:22",
                        remote_dir="/home/demo/workspace",
                        auth_mode="key",
                        identity_file="~/.ssh/id_ed25519",
                        known_hosts_policy="accept_new",
                        remote_os="linux",
                    ),
                )
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                captured: dict[str, object] = {"called": False}

                async def _fake_validate_remote_workspace(**kwargs):  # type: ignore[no-untyped-def]
                    captured["called"] = True
                    captured["kwargs"] = kwargs
                    return {"ok": True}

                state.validate_remote_workspace = _fake_validate_remote_workspace  # type: ignore[method-assign]
                result = await state.validate_remote_session_load(
                    session_id="session-1",
                    sandbox_backend="none",
                )
                self.assertIsNone(result)
                self.assertFalse(bool(captured["called"]))

        asyncio.run(run())

    def test_validate_remote_session_load_runs_check_for_anthropic_backend(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_dir = app_dir / ".opencompany" / "sessions" / "session-1"
                session_dir.mkdir(parents=True, exist_ok=True)
                save_remote_session_config(
                    session_dir,
                    RemoteSessionConfig(
                        kind="remote_ssh",
                        ssh_target="demo@example.com:22",
                        remote_dir="/home/demo/workspace",
                        auth_mode="key",
                        identity_file="~/.ssh/id_ed25519",
                        known_hosts_policy="accept_new",
                        remote_os="linux",
                    ),
                )
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                captured: dict[str, object] = {}

                async def _fake_validate_remote_workspace(**kwargs):  # type: ignore[no-untyped-def]
                    captured.update(kwargs)
                    return {"ok": True}

                state.validate_remote_workspace = _fake_validate_remote_workspace  # type: ignore[method-assign]
                result = await state.validate_remote_session_load(
                    session_id="session-1",
                    sandbox_backend="anthropic",
                )
                self.assertEqual(result, {"ok": True})
                self.assertEqual(str(captured.get("sandbox_backend")), "anthropic")
                self.assertEqual(str((captured.get("remote") or {}).get("ssh_target", "")), "demo@example.com:22")

        asyncio.run(run())

    def test_validate_remote_session_load_wraps_unexpected_exception(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_dir = app_dir / ".opencompany" / "sessions" / "session-1"
                session_dir.mkdir(parents=True, exist_ok=True)
                save_remote_session_config(
                    session_dir,
                    RemoteSessionConfig(
                        kind="remote_ssh",
                        ssh_target="demo@example.com:22",
                        remote_dir="/home/demo/workspace",
                        auth_mode="key",
                        identity_file="~/.ssh/id_ed25519",
                        known_hosts_policy="accept_new",
                        remote_os="linux",
                    ),
                )
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                async def _fake_validate_remote_workspace(**kwargs):  # type: ignore[no-untyped-def]
                    del kwargs
                    raise RuntimeError("Remote sandbox dependency check crashed unexpectedly")

                state.validate_remote_workspace = _fake_validate_remote_workspace  # type: ignore[method-assign]
                with self.assertRaises(ValueError) as exc_info:
                    await state.validate_remote_session_load(
                        session_id="session-1",
                        sandbox_backend="anthropic",
                    )
                message = str(exc_info.exception)
                self.assertIn("Remote validation failed", message)
                self.assertIn("dependency check crashed unexpectedly", message)

        asyncio.run(run())

    def test_pick_session_directory_validates_remote_session_before_applying(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                sessions_root = app_dir / ".opencompany" / "sessions"
                sessions_root.mkdir(parents=True, exist_ok=True)
                picked = sessions_root / "session-1"
                picked.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                captured: dict[str, object] = {}

                state.prompt_for_directory = lambda _title, _initial: picked  # type: ignore[method-assign]

                async def _fake_validate_remote_session_load(**kwargs):  # type: ignore[no-untyped-def]
                    captured["validate_kwargs"] = kwargs
                    return None

                def _fake_set_launch_config(**kwargs):  # type: ignore[no-untyped-def]
                    captured["set_kwargs"] = kwargs
                    return {"launch_config": {"session_id": kwargs.get("session_id")}}

                state.validate_remote_session_load = _fake_validate_remote_session_load  # type: ignore[method-assign]
                state.set_launch_config = _fake_set_launch_config  # type: ignore[method-assign]
                result = await state.pick_session_directory(sandbox_backend="anthropic")
                self.assertEqual(str((result.get("launch_config") or {}).get("session_id", "")), "session-1")
                self.assertEqual(
                    str((captured.get("validate_kwargs") or {}).get("session_id", "")),
                    "session-1",
                )
                self.assertEqual(
                    str((captured.get("validate_kwargs") or {}).get("sandbox_backend", "")),
                    "anthropic",
                )
                self.assertEqual(str((captured.get("set_kwargs") or {}).get("session_id", "")), "session-1")

        asyncio.run(run())

    def test_start_run_passes_staged_mode_to_orchestrator(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                state.set_launch_config(
                    project_dir=str(project_dir),
                    session_id=None,
                    session_mode="staged",
                )

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.workspace_mode: object | None = None

                    def subscribe(self, callback) -> None:  # type: ignore[no-untyped-def]
                        self.callback = callback

                    async def run_task(
                        self,
                        task: str,
                        model: str | None = None,
                        root_agent_name: str | None = None,
                        workspace_mode: str | None = None,
                    ) -> RunSession:
                        del task, model, root_agent_name
                        self.workspace_mode = workspace_mode
                        return RunSession(
                            id="session-run-staged",
                            project_dir=project_dir,
                            task="demo task",
                            locale="en",
                            root_agent_id="agent-root",
                            workspace_mode=WorkspaceMode.STAGED,
                            status=SessionStatus.COMPLETED,
                        )

                fake = _FakeOrchestrator()
                state._create_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]
                await state.start_run("demo task")
                assert state.session_task is not None
                await state.session_task
                self.assertEqual(fake.workspace_mode, WorkspaceMode.STAGED)
                self.assertEqual(state.launch_config().session_mode, WorkspaceMode.STAGED)
                self.assertTrue(state.launch_config().session_mode_locked)

        asyncio.run(run())

    def test_set_launch_config_rejects_invalid_session_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )
            with self.assertRaises(ValueError):
                state.set_launch_config(project_dir=None, session_id="../escape")

    def test_open_terminal_delegates_to_orchestrator(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )

            workspace_path = (app_dir / "workspace").resolve()
            workspace_path.mkdir(parents=True, exist_ok=True)
            state._resolve_session_id = lambda _session_id=None: "session-demo"  # type: ignore[method-assign]

            class _FakeOrchestrator:
                def open_session_terminal(self, session_id: str) -> dict[str, str]:
                    self.session_id = session_id
                    return {
                        "session_id": session_id,
                        "workspace_root": str(workspace_path),
                    }

            fake = _FakeOrchestrator()
            state._terminal_orchestrator = lambda: fake  # type: ignore[method-assign]
            context = state.open_terminal()
            self.assertEqual(context["session_id"], "session-demo")
            self.assertEqual(context["workspace_root"], str(workspace_path))
            self.assertEqual(fake.session_id, "session-demo")

    def test_open_terminal_reapplies_selected_sandbox_backend_on_existing_orchestrator(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )
            state.sandbox_backend = "anthropic"
            state.sandbox_backend_default = "anthropic"
            state._resolve_session_id = lambda _session_id=None: "session-demo"  # type: ignore[method-assign]

            class _FakeSandboxConfig:
                def __init__(self) -> None:
                    self.backend = "none"

            class _FakeConfig:
                def __init__(self) -> None:
                    self.sandbox = _FakeSandboxConfig()

            class _FakeToolExecutor:
                def __init__(self) -> None:
                    self.sandbox_backend_cls = object
                    self._shell_backend_instance = object()

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.config = _FakeConfig()
                    self.tool_executor = _FakeToolExecutor()

                def open_session_terminal(self, session_id: str) -> dict[str, str]:
                    return {
                        "session_id": session_id,
                        "backend": str(self.config.sandbox.backend),
                    }

            fake = _FakeOrchestrator()
            state.orchestrator = fake  # type: ignore[assignment]
            context = state.open_terminal()
            self.assertEqual(context["session_id"], "session-demo")
            self.assertEqual(context["backend"], "anthropic")
            self.assertEqual(fake.config.sandbox.backend, "anthropic")
            self.assertIsNone(fake.tool_executor._shell_backend_instance)

    def test_validate_remote_workspace_includes_setup_status_lines(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeBackend:
                    async def run_command(self, request, on_event=None):  # type: ignore[no-untyped-def]
                        del request
                        if on_event is not None:
                            maybe = on_event(
                                "stderr",
                                "[opencompany][remote-setup] Installing ripgrep via apt-get\n",
                            )
                            if asyncio.iscoroutine(maybe):
                                await maybe
                        return ShellCommandResult(
                            exit_code=0,
                            stdout="Linux\n",
                            stderr="",
                            command="uname -s",
                        )

                class _FakeToolExecutor:
                    def __init__(self) -> None:
                        self._remote_context: RemoteShellContext | None = None

                    def set_session_remote_config(  # type: ignore[no-untyped-def]
                        self,
                        session_id,
                        remote_config,
                        *,
                        password="",
                    ) -> None:
                        self._remote_context = RemoteShellContext(
                            session_id=str(session_id),
                            config=remote_config,
                            password=password,
                        )

                    def session_remote_context(self, session_id: str) -> RemoteShellContext | None:
                        del session_id
                        return self._remote_context

                    def build_shell_request(  # type: ignore[no-untyped-def]
                        self,
                        *,
                        workspace_root,
                        command,
                        cwd,
                        writable_paths,
                        session_id,
                        remote,
                    ) -> ShellCommandRequest:
                        del cwd
                        return ShellCommandRequest(
                            command=command,
                            cwd=workspace_root,
                            workspace_root=workspace_root,
                            writable_paths=writable_paths,
                            timeout_seconds=30,
                            session_id=session_id,
                            remote=remote,
                        )

                    def shell_backend(self) -> _FakeBackend:
                        return _FakeBackend()

                    def cleanup_session_remote_runtime(self, session_id: str) -> None:
                        del session_id
                        self._remote_context = None

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.tool_executor = _FakeToolExecutor()

                state._create_orchestrator = lambda _project_dir: _FakeOrchestrator()  # type: ignore[method-assign]
                result = await state.validate_remote_workspace(
                    remote={
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:22",
                        "remote_dir": "/home/demo/workspace",
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    },
                    session_mode="direct",
                )

                self.assertTrue(result["ok"])
                self.assertIn("[opencompany][remote-setup]", str(result.get("stderr", "")))

        asyncio.run(run())

    def test_list_session_directories_includes_continued_from_session_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_dir = app_dir / ".opencompany" / "sessions" / "session-copy-1"
            session_dir.mkdir(parents=True, exist_ok=True)
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )

            class _FakeStorage:
                def list_sessions(self) -> list[dict[str, object]]:
                    return [
                        {
                            "id": "session-copy-1",
                            "status": "interrupted",
                            "task": "loaded task",
                            "updated_at": "2026-03-12T12:00:00Z",
                            "project_dir": str(app_dir / "project"),
                            "config_snapshot_json": json.dumps(
                                {"continued_from_session_id": "session-origin-1"}
                            ),
                        }
                    ]

            class _FakeOrchestrator:
                def __init__(self) -> None:
                    self.storage = _FakeStorage()

            state._read_orchestrator = lambda _project_dir: _FakeOrchestrator()  # type: ignore[method-assign]
            listed = state.list_session_directories()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["session_id"], "session-copy-1")
            self.assertEqual(
                listed[0]["continued_from_session_id"],
                "session-origin-1",
            )

    def test_initial_locale_uses_config_default_locale(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[project]
default_locale = "zh"
""".strip(),
                encoding="utf-8",
            )
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale=None,
                debug=False,
            )
            self.assertEqual(state.locale, "zh")
            self.assertEqual(state.snapshot()["locale"], "zh")

    def test_initial_model_uses_config_default_model(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[llm.openrouter]
model = "openai/gpt-4.1-mini"
""".strip(),
                encoding="utf-8",
            )
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale=None,
                debug=False,
            )
            self.assertEqual(state.snapshot()["runtime"]["model"], "openai/gpt-4.1-mini")

    def test_start_run_passes_selected_model_to_orchestrator(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text(
                    """
[llm.openrouter]
model = "openai/gpt-4o-mini"
""".strip(),
                    encoding="utf-8",
                )
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=None,
                    app_dir=app_dir,
                    locale=None,
                    debug=False,
                )

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.task = ""
                        self.model = ""
                        self.root_agent_name: str | None = None

                    def subscribe(self, callback) -> None:  # type: ignore[no-untyped-def]
                        self.callback = callback

                    async def run_task(
                        self,
                        task: str,
                        model: str | None = None,
                        root_agent_name: str | None = None,
                    ) -> RunSession:
                        self.task = task
                        self.model = str(model or "")
                        self.root_agent_name = root_agent_name
                        return RunSession(
                            id="session-run-1",
                            project_dir=project_dir,
                            task=task,
                            locale="en",
                            root_agent_id="agent-root",
                            status=SessionStatus.COMPLETED,
                        )

                fake = _FakeOrchestrator()
                state._create_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]
                await state.start_run(
                    "demo task",
                    model="openai/gpt-4.1",
                    root_agent_name="Root Alpha",
                )
                assert state.session_task is not None
                await state.session_task
                self.assertEqual(fake.task, "demo task")
                self.assertEqual(fake.model, "openai/gpt-4.1")
                self.assertEqual(fake.root_agent_name, "Root Alpha")
                self.assertEqual(state.current_session_status, "completed")

        asyncio.run(run())

    def test_start_run_remote_setup_failure_keeps_session_id(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                remote_workspace = project_dir / "remote-workspace"
                remote_workspace.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                state.set_launch_config(
                    project_dir=None,
                    session_id=None,
                    session_mode="direct",
                    remote={
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:2222",
                        "remote_dir": str(remote_workspace),
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    },
                )

                orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)

                def _raise_remote_setup(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                    del args, kwargs
                    raise RuntimeError("remote setup failed")

                orchestrator._apply_session_remote_runtime = _raise_remote_setup  # type: ignore[method-assign]
                state._create_orchestrator = lambda _project_dir: orchestrator  # type: ignore[method-assign]

                await state.start_run(
                    "demo task",
                    model="openai/gpt-4.1",
                    root_agent_name="Root Alpha",
                )
                assert state.session_task is not None
                await state.session_task
                self.assertIsNotNone(state.current_session_id)
                self.assertEqual(state.current_session_id, orchestrator.latest_session_id)
                self.assertEqual(state.current_session_status, "failed")
                self.assertIn("remote setup failed", state.current_summary)

        asyncio.run(run())

    def test_start_run_remote_keeps_project_dir_none_after_completion(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                state.set_launch_config(
                    project_dir=None,
                    session_id=None,
                    session_mode="direct",
                    remote={
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:2222",
                        "remote_dir": "/home/demo/workspace",
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    },
                )

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir

                    def subscribe(self, callback) -> None:  # type: ignore[no-untyped-def]
                        self.callback = callback

                    async def run_task(
                        self,
                        task: str,
                        model: str | None = None,
                        root_agent_name: str | None = None,
                        remote_config: RemoteSessionConfig | None = None,
                        remote_password: str | None = None,
                    ) -> RunSession:
                        del task, model, root_agent_name, remote_config, remote_password
                        return RunSession(
                            id="session-remote-1",
                            project_dir=Path("/home/demo/workspace"),
                            task="demo task",
                            locale="en",
                            root_agent_id="agent-root",
                            status=SessionStatus.COMPLETED,
                        )

                fake = _FakeOrchestrator()
                state._create_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]
                await state.start_run("demo task")
                assert state.session_task is not None
                await state.session_task
                self.assertIsNone(state.project_dir)
                self.assertIsNotNone(state.remote_config)
                assert state.remote_config is not None
                self.assertEqual(state.remote_config.remote_dir, "/home/demo/workspace")

        asyncio.run(run())

    def test_start_run_with_configured_session_uses_run_task_in_session(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_id = "session-start-run-existing"
                (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=session_id,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.session_id = ""
                        self.task = ""
                        self.model = ""
                        self.root_agent_name: str | None = None
                        self.run_task_called = False

                    def subscribe(self, callback) -> None:  # type: ignore[no-untyped-def]
                        self.callback = callback

                    async def run_task_in_session(
                        self,
                        session_id: str,
                        task: str,
                        model: str | None = None,
                        root_agent_name: str | None = None,
                    ) -> RunSession:
                        self.session_id = session_id
                        self.task = task
                        self.model = str(model or "")
                        self.root_agent_name = root_agent_name
                        return RunSession(
                            id=session_id,
                            project_dir=project_dir,
                            task=task,
                            locale="en",
                            root_agent_id="agent-root-new",
                            status=SessionStatus.COMPLETED,
                        )

                    async def run_task(
                        self,
                        task: str,
                        model: str | None = None,
                        root_agent_name: str | None = None,
                    ) -> RunSession:
                        del task, model, root_agent_name
                        self.run_task_called = True
                        return RunSession(
                            id="unexpected",
                            project_dir=project_dir,
                            task="unexpected",
                            locale="en",
                            root_agent_id="agent-root",
                            status=SessionStatus.COMPLETED,
                        )

                fake = _FakeOrchestrator()
                state._create_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]
                await state.start_run(
                    "new root task",
                    model="openai/gpt-4.1",
                    root_agent_name="Root Beta",
                )
                assert state.session_task is not None
                await state.session_task
                self.assertEqual(fake.session_id, session_id)
                self.assertEqual(fake.task, "new root task")
                self.assertEqual(fake.model, "openai/gpt-4.1")
                self.assertEqual(fake.root_agent_name, "Root Beta")
                self.assertFalse(fake.run_task_called)
                self.assertEqual(state.current_session_status, "completed")

        asyncio.run(run())

    def test_continue_task_remote_keeps_project_dir_none_after_resume(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                state.set_launch_config(
                    project_dir=None,
                    session_id=None,
                    session_mode="direct",
                    remote={
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:2222",
                        "remote_dir": "/home/demo/workspace",
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    },
                )

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir

                    async def resume(
                        self,
                        session_id: str,
                        instruction: str,
                        model: str | None = None,
                        reactivate_agent_id: str | None = None,
                        run_root_agent: bool = True,
                        remote_password: str | None = None,
                        enabled_skill_ids: list[str] | None = None,
                    ) -> RunSession:
                        del (
                            session_id,
                            instruction,
                            model,
                            reactivate_agent_id,
                            run_root_agent,
                            remote_password,
                            enabled_skill_ids,
                        )
                        return RunSession(
                            id="session-remote-2",
                            project_dir=Path("/home/demo/workspace"),
                            task="resume task",
                            locale="en",
                            root_agent_id="agent-root",
                            status=SessionStatus.COMPLETED,
                        )

                state.orchestrator = _FakeOrchestrator()  # type: ignore[assignment]
                await state._continue_task(
                    "session-remote-2",
                    "resume task",
                    "openai/gpt-4.1",
                )
                self.assertIsNone(state.project_dir)
                self.assertEqual(state.current_session_id, "session-remote-2")
                self.assertEqual(state.current_session_status, "completed")

        asyncio.run(run())

    def test_start_run_while_running_submits_live_root_run(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_id = "session-run-queue"
                (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=session_id,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.calls: list[tuple[str, str, str, str]] = []
                        self.root_agent_names: list[str | None] = []

                    def submit_run_in_active_session(
                        self,
                        session_id: str,
                        task: str,
                        *,
                        model: str | None = None,
                        root_agent_name: str | None = None,
                        source: str = "webui",
                    ) -> dict[str, str]:
                        self.calls.append((session_id, task, str(model or ""), source))
                        self.root_agent_names.append(root_agent_name)
                        return {
                            "session_id": session_id,
                            "root_agent_id": "agent-root-live",
                            "task": task,
                            "model": str(model or ""),
                            "source": source,
                        }

                fake = _FakeOrchestrator()
                state.orchestrator = fake  # type: ignore[assignment]
                state.current_session_id = session_id
                state.session_task = asyncio.create_task(asyncio.sleep(30))
                snapshot = await state.start_run(
                    "root task live",
                    model="openai/gpt-4.1-mini",
                    root_agent_name="Root Live",
                )
                self.assertEqual(
                    fake.calls,
                    [
                        (
                            session_id,
                            "root task live",
                            "openai/gpt-4.1-mini",
                            "webui",
                        )
                    ],
                )
                self.assertEqual(fake.root_agent_names, ["Root Live"])
                self.assertEqual(snapshot["runtime"]["session_status"], "running")
                self.assertEqual(state.current_task, "root task live")
                self.assertIsNotNone(state.session_task)
                assert state.session_task is not None
                state.session_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await state.session_task

        asyncio.run(run())

    def test_save_config_updates_runtime_locale_when_no_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[project]
default_locale = "en"
""".strip(),
                encoding="utf-8",
            )
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale=None,
                debug=False,
            )
            self.assertEqual(state.locale, "en")
            saved = state.save_config('[project]\ndefault_locale = "zh"\n')
            self.assertEqual(state.locale, "zh")
            self.assertEqual(saved["snapshot"]["locale"], "zh")

    def test_save_config_keeps_explicit_locale_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[project]
default_locale = "zh"
""".strip(),
                encoding="utf-8",
            )
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )
            saved = state.save_config('[project]\ndefault_locale = "zh"\n')
            self.assertEqual(state.locale, "en")
            self.assertEqual(saved["snapshot"]["locale"], "en")

    def test_save_config_updates_sessions_dir_in_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[project]
data_dir = ".opencompany"
""".strip(),
                encoding="utf-8",
            )
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale=None,
                debug=False,
            )
            saved = state.save_config('[project]\ndata_dir = ".opencompany-next"\n')
            self.assertEqual(
                saved["snapshot"]["sessions_dir"],
                str((app_dir / ".opencompany-next" / "sessions").resolve()),
            )

    def test_save_config_rejects_invalid_toml(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            state = WebUIRuntimeState(
                project_dir=None,
                session_id=None,
                app_dir=app_dir,
                locale="en",
                debug=False,
            )
            with self.assertRaises(ValueError):
                state.save_config("not = valid = toml")

    def test_shutdown_cancels_running_session_task(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                state = WebUIRuntimeState(
                    project_dir=None,
                    session_id=None,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )
                state.session_task = asyncio.create_task(asyncio.sleep(30))
                await state.shutdown()
                self.assertIsNone(state.session_task)

        asyncio.run(run())

    def test_submit_steer_run_with_activation_resumes_inactive_session(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_id = "session-steer-auto-activate"
                (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=session_id,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeStorage:
                    @staticmethod
                    def load_session(_session_id: str) -> dict[str, str]:
                        return {"id": session_id, "status": "interrupted"}

                    @staticmethod
                    def load_agents(_session_id: str) -> list[dict[str, str]]:
                        return [{"id": "agent-root", "role": "root"}]

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.storage = _FakeStorage()
                        self.resume_args: dict[str, object] | None = None

                    def submit_steer_run(
                        self,
                        *,
                        session_id: str,
                        agent_id: str,
                        content: str,
                        source: str = "webui",
                        source_agent_id: str = "user",
                        source_agent_name: str | None = None,
                    ) -> dict[str, str]:
                        return {
                            "id": "steerrun-auto-activate",
                            "session_id": session_id,
                            "agent_id": agent_id,
                            "content": content,
                            "source": source,
                            "source_agent_id": source_agent_id,
                            "source_agent_name": source_agent_name or "user",
                            "status": "waiting",
                        }

                    async def resume(
                        self,
                        session_id: str,
                        instruction: str,
                        model: str | None = None,
                        reactivate_agent_id: str | None = None,
                        run_root_agent: bool = True,
                    ) -> RunSession:
                        self.resume_args = {
                            "session_id": session_id,
                            "instruction": instruction,
                            "model": model,
                            "reactivate_agent_id": reactivate_agent_id,
                            "run_root_agent": run_root_agent,
                        }
                        return RunSession(
                            id=session_id,
                            project_dir=project_dir,
                            task=instruction,
                            locale="en",
                            root_agent_id="agent-root",
                            status=SessionStatus.COMPLETED,
                        )

                fake = _FakeOrchestrator()
                state._write_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]

                run_payload = await state.submit_steer_run_with_activation(
                    session_id,
                    agent_id="agent-root",
                    content="resume this from steer",
                    source="webui",
                )
                self.assertEqual(run_payload["status"], "waiting")
                self.assertIsNotNone(state.session_task)
                assert state.session_task is not None
                await state.session_task
                self.assertIsNotNone(fake.resume_args)
                assert fake.resume_args is not None
                self.assertEqual(fake.resume_args["session_id"], session_id)
                self.assertEqual(fake.resume_args["reactivate_agent_id"], "agent-root")
                self.assertEqual(fake.resume_args["run_root_agent"], True)
                self.assertEqual(state.current_session_status, "completed")

        asyncio.run(run())

    def test_submit_steer_run_with_activation_skips_resume_when_session_running(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_id = "session-steer-running"
                (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=session_id,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeStorage:
                    @staticmethod
                    def load_session(_session_id: str) -> dict[str, str]:
                        return {"id": session_id, "status": "running"}

                    @staticmethod
                    def load_agents(_session_id: str) -> list[dict[str, str]]:
                        return [{"id": "agent-root", "role": "root"}]

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.storage = _FakeStorage()
                        self.resume_called = False

                    def submit_steer_run(
                        self,
                        *,
                        session_id: str,
                        agent_id: str,
                        content: str,
                        source: str = "webui",
                        source_agent_id: str = "user",
                        source_agent_name: str | None = None,
                    ) -> dict[str, str]:
                        return {
                            "id": "steerrun-running",
                            "session_id": session_id,
                            "agent_id": agent_id,
                            "content": content,
                            "source": source,
                            "source_agent_id": source_agent_id,
                            "source_agent_name": source_agent_name or "user",
                            "status": "waiting",
                        }

                    async def resume(
                        self,
                        session_id: str,
                        instruction: str,
                        model: str | None = None,
                        reactivate_agent_id: str | None = None,
                        run_root_agent: bool = True,
                    ) -> RunSession:
                        del session_id, instruction, model, reactivate_agent_id, run_root_agent
                        self.resume_called = True
                        return RunSession(
                            id="unused",
                            project_dir=project_dir,
                            task="unused",
                            locale="en",
                            root_agent_id="agent-root",
                            status=SessionStatus.COMPLETED,
                        )

                fake = _FakeOrchestrator()
                state._write_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]

                run_payload = await state.submit_steer_run_with_activation(
                    session_id,
                    agent_id="agent-root",
                    content="normal steer",
                    source="webui",
                )
                self.assertEqual(run_payload["status"], "waiting")
                self.assertIsNone(state.session_task)
                self.assertFalse(fake.resume_called)

        asyncio.run(run())

    def test_terminate_agent_with_subtree_forwards_to_orchestrator(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_id = "session-terminate-worker"
                (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=session_id,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.calls: list[tuple[str, str, str]] = []

                    async def terminate_agent_subtree(
                        self,
                        *,
                        session_id: str,
                        agent_id: str,
                        source: str = "webui",
                    ) -> dict[str, object]:
                        self.calls.append((session_id, agent_id, source))
                        return {
                            "session_id": session_id,
                            "agent_id": agent_id,
                            "source": source,
                            "target_agent_ids": [agent_id],
                            "terminated_agent_ids": [agent_id],
                            "cancelled_tool_run_ids": ["toolrun-1"],
                        }

                fake = _FakeOrchestrator()
                state._write_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]

                result = await state.terminate_agent_with_subtree(
                    session_id,
                    agent_id="agent-worker",
                    source="webui",
                )
                self.assertEqual(
                    fake.calls,
                    [(session_id, "agent-worker", "webui")],
                )
                self.assertEqual(result["agent_id"], "agent-worker")
                self.assertEqual(
                    state.status_message,
                    state.translator.text("agent_terminate_requested"),
                )

        asyncio.run(run())

    def test_submit_steer_run_with_activation_non_root_resumes_without_root(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_id = "session-steer-non-root"
                (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=session_id,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeStorage:
                    @staticmethod
                    def load_session(_session_id: str) -> dict[str, str]:
                        return {"id": session_id, "status": "interrupted"}

                    @staticmethod
                    def load_agents(_session_id: str) -> list[dict[str, str]]:
                        return [{"id": "agent-worker", "role": "worker"}]

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.storage = _FakeStorage()
                        self.resume_args: dict[str, object] | None = None

                    def submit_steer_run(
                        self,
                        *,
                        session_id: str,
                        agent_id: str,
                        content: str,
                        source: str = "webui",
                        source_agent_id: str = "user",
                        source_agent_name: str | None = None,
                    ) -> dict[str, str]:
                        return {
                            "id": "steerrun-worker",
                            "session_id": session_id,
                            "agent_id": agent_id,
                            "content": content,
                            "source": source,
                            "source_agent_id": source_agent_id,
                            "source_agent_name": source_agent_name or "user",
                            "status": "waiting",
                        }

                    async def resume(
                        self,
                        session_id: str,
                        instruction: str,
                        model: str | None = None,
                        reactivate_agent_id: str | None = None,
                        run_root_agent: bool = True,
                    ) -> RunSession:
                        self.resume_args = {
                            "session_id": session_id,
                            "instruction": instruction,
                            "model": model,
                            "reactivate_agent_id": reactivate_agent_id,
                            "run_root_agent": run_root_agent,
                        }
                        return RunSession(
                            id=session_id,
                            project_dir=project_dir,
                            task=instruction,
                            locale="en",
                            root_agent_id="agent-root",
                            status=SessionStatus.INTERRUPTED,
                        )

                fake = _FakeOrchestrator()
                state._write_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]

                await state.submit_steer_run_with_activation(
                    session_id,
                    agent_id="agent-worker",
                    content="run worker only",
                    source="webui",
                )
                self.assertIsNotNone(state.session_task)
                assert state.session_task is not None
                await state.session_task
                self.assertIsNotNone(fake.resume_args)
                assert fake.resume_args is not None
                self.assertEqual(fake.resume_args["reactivate_agent_id"], "agent-worker")
                self.assertEqual(fake.resume_args["run_root_agent"], False)

        asyncio.run(run())

    def test_submit_steer_run_with_activation_inactive_noncurrent_root_runs_target_root(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                app_dir = Path(temp_dir)
                project_dir = app_dir / "project"
                project_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
                session_id = "session-steer-inactive-old-root"
                (app_dir / ".opencompany" / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
                state = WebUIRuntimeState(
                    project_dir=project_dir,
                    session_id=session_id,
                    app_dir=app_dir,
                    locale="en",
                    debug=False,
                )

                class _FakeStorage:
                    @staticmethod
                    def load_session(_session_id: str) -> dict[str, str]:
                        return {
                            "id": session_id,
                            "status": "interrupted",
                            "root_agent_id": "agent-root-current",
                        }

                    @staticmethod
                    def load_agents(_session_id: str) -> list[dict[str, str]]:
                        return [{"id": "agent-root-old", "role": "root"}]

                class _FakeOrchestrator:
                    def __init__(self) -> None:
                        self.app_dir = app_dir
                        self.storage = _FakeStorage()
                        self.resume_args: dict[str, object] | None = None

                    def submit_steer_run(
                        self,
                        *,
                        session_id: str,
                        agent_id: str,
                        content: str,
                        source: str = "webui",
                        source_agent_id: str = "user",
                        source_agent_name: str | None = None,
                    ) -> dict[str, str]:
                        return {
                            "id": "steerrun-old-root",
                            "session_id": session_id,
                            "agent_id": agent_id,
                            "content": content,
                            "source": source,
                            "source_agent_id": source_agent_id,
                            "source_agent_name": source_agent_name or "user",
                            "status": "waiting",
                        }

                    async def resume(
                        self,
                        session_id: str,
                        instruction: str,
                        model: str | None = None,
                        reactivate_agent_id: str | None = None,
                        run_root_agent: bool = True,
                    ) -> RunSession:
                        self.resume_args = {
                            "session_id": session_id,
                            "instruction": instruction,
                            "model": model,
                            "reactivate_agent_id": reactivate_agent_id,
                            "run_root_agent": run_root_agent,
                        }
                        return RunSession(
                            id=session_id,
                            project_dir=project_dir,
                            task=instruction,
                            locale="en",
                            root_agent_id="agent-root-old",
                            status=SessionStatus.INTERRUPTED,
                        )

                fake = _FakeOrchestrator()
                state._write_orchestrator = lambda _project_dir: fake  # type: ignore[method-assign]

                await state.submit_steer_run_with_activation(
                    session_id,
                    agent_id="agent-root-old",
                    content="run this old root only",
                    source="webui",
                )
                self.assertIsNotNone(state.session_task)
                assert state.session_task is not None
                await state.session_task
                self.assertIsNotNone(fake.resume_args)
                assert fake.resume_args is not None
                self.assertEqual(fake.resume_args["reactivate_agent_id"], "agent-root-old")
                self.assertEqual(fake.resume_args["run_root_agent"], True)

        asyncio.run(run())


class NativeDirectoryPickerTests(unittest.TestCase):
    def test_open_macos_directory_picker_activates_system_events(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["osascript"],
            returncode=0,
            stdout="/tmp/example\n",
            stderr="",
        )
        with patch.object(webui_state.subprocess, "run", return_value=completed) as run_mock:
            selected = webui_state._open_macos_directory_picker(
                title="Select a Folder",
                initial_dir=Path("/tmp"),
            )

        self.assertEqual(selected, "/tmp/example")
        run_mock.assert_called_once()
        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], "osascript")
        script_lines = [command[index + 1] for index, token in enumerate(command[:-1]) if token == "-e"]
        self.assertIn('tell application "System Events" to activate', script_lines)
        choose_line_index = next(
            (
                index
                for index, line in enumerate(script_lines)
                if line.startswith("set chosenFolder to choose folder with prompt ")
            ),
            -1,
        )
        self.assertGreaterEqual(choose_line_index, 0)
        self.assertLess(
            script_lines.index('tell application "System Events" to activate'),
            choose_line_index,
        )


class WebUIServerTests(unittest.TestCase):
    def test_create_webui_app_import_guard(self) -> None:
        try:
            import fastapi  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError:
            with self.assertRaises(RuntimeError):
                create_webui_app()
            return

        app = create_webui_app()
        self.assertIsNotNone(app)
