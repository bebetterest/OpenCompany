from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    RunSession,
    SessionStatus,
    ToolRun,
    ToolRunStatus,
)
from opencompany.orchestrator import Orchestrator, default_app_dir
from opencompany.tools.definitions import DEFAULT_TOOL_NAMES
from opencompany.utils import utc_now
from opencompany.workspace import WorkspaceManager


RECORDS_PATH = Path(__file__).with_name("tool_io_contract_records.json")


def build_test_project(project_dir: Path, *, list_default_limit: int = 20, list_max_limit: int = 200) -> None:
    (project_dir / "README.md").write_text("demo\n", encoding="utf-8")
    (project_dir / "prompts").mkdir(parents=True, exist_ok=True)
    source_prompts = default_app_dir() / "prompts"
    for source in source_prompts.iterdir():
        target = project_dir / "prompts" / source.name
        if source.is_file():
            target.write_bytes(source.read_bytes())
    (project_dir / "opencompany.toml").write_text(
        f"""
[project]
name = "OpenCompany"
default_locale = "en"
data_dir = ".opencompany"

[llm.openrouter]
model = "fake/model"
max_tokens = 1000

[runtime.tools]
list_default_limit = {int(list_default_limit)}
list_max_limit = {int(list_max_limit)}
""".strip(),
        encoding="utf-8",
    )


class ToolIOContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _records() -> dict[str, dict[str, object]]:
        return json.loads(RECORDS_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _build_runtime(
        project_dir: Path,
        *,
        session_id: str,
    ) -> tuple[Orchestrator, RunSession, WorkspaceManager, AgentNode, dict[str, AgentNode]]:
        orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
        workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
        root_workspace = workspace_manager.create_root_workspace(project_dir)
        root = AgentNode(
            id="agent-root",
            session_id=session_id,
            name="Root",
            role=AgentRole.ROOT,
            instruction="inspect",
            workspace_id=root_workspace.id,
            status=AgentStatus.RUNNING,
            metadata={"created_at": "2026-03-11T10:00:00Z"},
            conversation=[{"role": "user", "content": "start"}],
        )
        session = RunSession(
            id=session_id,
            project_dir=project_dir,
            task="tool contract",
            locale="en",
            root_agent_id=root.id,
            status=SessionStatus.RUNNING,
            created_at=utc_now(),
            updated_at=utc_now(),
            config_snapshot={},
        )
        orchestrator.storage.upsert_session(session)
        orchestrator.storage.upsert_agent(root)
        return orchestrator, session, workspace_manager, root, {root.id: root}

    def _assert_contract(self, tool_name: str, output: dict[str, object]) -> None:
        record = self._records()[tool_name]
        required = record.get("required_output_keys", [])
        forbidden = record.get("forbidden_output_keys", [])
        allowed = record.get("allowed_output_keys", [])
        for field in required:
            self.assertIn(field, output, msg=f"{tool_name}: missing required output field '{field}'")
        for field in forbidden:
            self.assertNotIn(field, output, msg=f"{tool_name}: should not echo input field '{field}'")
        if isinstance(allowed, list) and allowed:
            allowed_set = {str(field) for field in allowed}
            unexpected = sorted(str(field) for field in output.keys() if str(field) not in allowed_set)
            self.assertFalse(
                unexpected,
                msg=(
                    f"{tool_name}: contains unexpected output field(s): "
                    + ", ".join(unexpected)
                ),
            )

    def test_record_file_covers_all_tools(self) -> None:
        records = self._records()
        self.assertEqual(set(records.keys()), set(DEFAULT_TOOL_NAMES))
        for tool_name in DEFAULT_TOOL_NAMES:
            entry = records.get(tool_name, {})
            assert isinstance(entry, dict)
            self.assertIn("output_example", entry)

    async def test_shell_wait_time_and_finish_io_contracts(self) -> None:
        records = self._records()
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            shell_output = orchestrator._project_tool_result(
                action=dict(records["shell"]["input"]),
                raw_result={"exit_code": 0, "stdout": "hello\n", "stderr": "", "duration_ms": 12},
                run_id="toolrun-shell-case",
            )
            self._assert_contract("shell", shell_output)

            wait_output = await orchestrator._wait_time_result(action=dict(records["wait_time"]["input"]))
            self._assert_contract("wait_time", wait_output)

            compress_output = orchestrator._project_tool_result(
                action=dict(records["compress_context"]["input"]),
                raw_result={
                    "compressed": True,
                    "reason": "manual",
                    "summary_version": 1,
                    "message_range": {"start": 0, "end": 1},
                    "step_range": {"start": 1, "end": 1},
                    "context_tokens_before": 5120,
                    "context_tokens_after": 1024,
                    "context_limit_tokens": 128000,
                },
                run_id="toolrun-compress-case",
            )
            self._assert_contract("compress_context", compress_output)

            finish_output = orchestrator._project_tool_result(
                action=dict(records["finish"]["input"]),
                raw_result={"status": "accepted"},
                run_id="toolrun-finish-case",
            )
            self._assert_contract("finish", finish_output)

    async def test_get_agent_run_projection_keeps_truncation_hints(self) -> None:
        records = self._records()
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            projected = orchestrator._project_tool_result(
                action=dict(records["get_agent_run"]["input"]),
                raw_result={
                    "agent_run": {
                        "id": "agent-root",
                        "name": "Root",
                        "role": "root",
                        "status": "running",
                        "created_at": "2026-03-11T10:00:00Z",
                        "parent_agent_id": None,
                        "children_count": 0,
                        "step_count": 0,
                    },
                    "messages": [{"role": "assistant", "content": "m-0"}],
                    "warning": "get_agent_run returned only the first 5 messages for the requested slice.",
                    "next_messages_start": 6,
                },
                run_id="toolrun-get-agent-run-projected",
            )
            self._assert_contract("get_agent_run", projected)
            self.assertEqual(projected.get("next_messages_start"), 6)
            self.assertIn("first 5 messages", str(projected.get("warning", "")))

    async def test_list_agent_runs_get_agent_run_and_cancel_agent_contracts(self) -> None:
        records = self._records()
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = self._build_runtime(
                project_dir,
                session_id="session-agent-tools",
            )
            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=root.session_id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="work",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
                summary="Child summary",
                conversation=[{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}],
            )
            root.children.append(child.id)
            agents[child.id] = child
            orchestrator.storage.upsert_agent(root)
            orchestrator.storage.upsert_agent(child)

            listed = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action=dict(records["list_agent_runs"]["input"]),
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self._assert_contract("list_agent_runs", listed)
            rows = listed.get("agent_runs")
            assert isinstance(rows, list)
            self.assertTrue(rows)

            detail = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action=dict(records["get_agent_run"]["input"]),
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self._assert_contract("get_agent_run", detail)
            self.assertIsInstance(detail.get("messages"), list)

            cancelled = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action=dict(records["cancel_agent"]["input"]),
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self._assert_contract("cancel_agent", cancelled)

            steered = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action=dict(records["steer_agent"]["input"]),
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            steer_output = steered.get("agent_result")
            assert isinstance(steer_output, dict)
            self._assert_contract("steer_agent", steer_output)

    async def test_spawn_list_get_wait_cancel_tool_run_contracts(self) -> None:
        records = self._records()
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = self._build_runtime(
                project_dir,
                session_id="session-tool-run-tools",
            )

            async def _fake_spawn_child_with_timeout(**_kwargs):  # type: ignore[no-untyped-def]
                return "agent-child", {}

            with (
                mock.patch.object(orchestrator, "_new_tool_run_id", return_value="toolrun-spawn"),
                mock.patch.object(orchestrator, "_spawn_child_with_timeout", side_effect=_fake_spawn_child_with_timeout),
                mock.patch.object(orchestrator, "_ensure_worker_tasks_started", return_value=None),
            ):
                spawned = await orchestrator._submit_tool_run(
                    session=session,
                    agent=root,
                    action=dict(records["spawn_agent"]["input"]),
                    agents=agents,
                    workspace_manager=workspace_manager,
                    root_loop=0,
                    tracked_pending_ids=[],
                )
            spawn_output = spawned.get("agent_result")
            assert isinstance(spawn_output, dict)
            self._assert_contract("spawn_agent", spawn_output)

            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id="toolrun-sample",
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="shell",
                    arguments={"type": "shell", "command": "echo ok"},
                    status=ToolRunStatus.COMPLETED,
                    blocking=True,
                    created_at=utc_now(),
                    started_at=utc_now(),
                    completed_at=utc_now(),
                    result={"stdout": "ok"},
                )
            )

            listed = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action=dict(records["list_tool_runs"]["input"]),
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            listed_output = listed.get("agent_result")
            assert isinstance(listed_output, dict)
            self._assert_contract("list_tool_runs", listed_output)

            got = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action=dict(records["get_tool_run"]["input"]),
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            got_output = got.get("agent_result")
            assert isinstance(got_output, dict)
            self._assert_contract("get_tool_run", got_output)

            waited = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action=dict(records["wait_run"]["input"]),
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            waited_output = waited.get("agent_result")
            assert isinstance(waited_output, dict)
            self._assert_contract("wait_run", waited_output)

            queued_id = "toolrun-queued"
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=queued_id,
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="shell",
                    arguments={"type": "shell", "command": "sleep 1"},
                    status=ToolRunStatus.QUEUED,
                    blocking=True,
                    created_at=utc_now(),
                )
            )
            cancelled = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "cancel_tool_run", "tool_run_id": queued_id},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            cancelled_output = cancelled.get("agent_result")
            assert isinstance(cancelled_output, dict)
            self._assert_contract("cancel_tool_run", cancelled_output)

    async def test_list_tools_use_configured_default_and_max_limits(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir, list_default_limit=1, list_max_limit=2)
            orchestrator, session, workspace_manager, root, agents = self._build_runtime(
                project_dir,
                session_id="session-list-limit-config",
            )
            for child_id in ("agent-a", "agent-b", "agent-c"):
                child_workspace = workspace_manager.fork_workspace(root.workspace_id, child_id)
                child = AgentNode(
                    id=child_id,
                    session_id=root.session_id,
                    name=child_id,
                    role=AgentRole.WORKER,
                    instruction=child_id,
                    workspace_id=child_workspace.id,
                    parent_agent_id=root.id,
                    status=AgentStatus.RUNNING,
                )
                agents[child.id] = child

            default_page = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "list_agent_runs"},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            clamped_page = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "list_agent_runs", "limit": 99},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            default_rows = default_page.get("agent_runs")
            clamped_rows = clamped_page.get("agent_runs")
            assert isinstance(default_rows, list)
            assert isinstance(clamped_rows, list)
            self.assertEqual(len(default_rows), 1)
            self.assertEqual(len(clamped_rows), 2)

            for index in range(3):
                orchestrator.storage.upsert_tool_run(
                    ToolRun(
                        id=f"toolrun-cap-{index}",
                        session_id=session.id,
                        agent_id=root.id,
                        tool_name="shell",
                        arguments={"type": "shell", "command": "echo ok"},
                        status=ToolRunStatus.COMPLETED,
                        blocking=True,
                        created_at=f"2026-03-11T10:00:0{index}Z",
                        started_at=f"2026-03-11T10:00:0{index}Z",
                        completed_at=f"2026-03-11T10:00:0{index}Z",
                    )
                )
            default_submit = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "list_tool_runs"},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            clamped_submit = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "list_tool_runs", "limit": 99},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            default_output = default_submit.get("agent_result")
            clamped_output = clamped_submit.get("agent_result")
            assert isinstance(default_output, dict)
            assert isinstance(clamped_output, dict)
            default_rows = default_output.get("tool_runs")
            clamped_rows = clamped_output.get("tool_runs")
            assert isinstance(default_rows, list)
            assert isinstance(clamped_rows, list)
            self.assertEqual(len(default_rows), 1)
            self.assertEqual(len(clamped_rows), 2)
