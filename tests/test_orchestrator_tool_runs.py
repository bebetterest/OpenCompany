from __future__ import annotations

import asyncio
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
from opencompany.utils import utc_now
from opencompany.workspace import WorkspaceManager


def build_test_project(project_dir: Path) -> None:
    shutil.copytree(default_app_dir() / "prompts", project_dir / "prompts")
    (project_dir / "opencompany.toml").write_text(
        """
[project]
name = "OpenCompany"
default_locale = "en"
data_dir = ".opencompany"

[llm.openrouter]
model = "fake/model"
max_tokens = 1000

[runtime.limits]
max_children_per_agent = 3
max_active_agents = 2
max_root_steps = 3
max_agent_steps = 4

[sandbox]
backend = "anthropic"
timeout_seconds = 10
""".strip(),
        encoding="utf-8",
    )
    (project_dir / "README.md").write_text("demo\n", encoding="utf-8")


def bootstrap_runtime(
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


class OrchestratorToolRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_agent_runs_pagination_and_messages_count(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-list-agent-runs",
            )

            for idx, status in enumerate((AgentStatus.RUNNING, AgentStatus.COMPLETED, AgentStatus.FAILED), start=1):
                child_workspace = workspace_manager.fork_workspace(root.workspace_id, f"agent-{idx}")
                child = AgentNode(
                    id=f"agent-{idx}",
                    session_id=session.id,
                    name=f"child-{idx}",
                    role=AgentRole.WORKER,
                    instruction="work",
                    workspace_id=child_workspace.id,
                    parent_agent_id=root.id,
                    status=status,
                    metadata={"created_at": f"2026-03-11T10:00:0{idx}Z"},
                    conversation=[{"role": "assistant", "content": "ok"}] * idx,
                )
                agents[child.id] = child

            first = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "list_agent_runs", "status": ["running", "completed"], "limit": 2},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            rows = first.get("agent_runs")
            assert isinstance(rows, list)
            self.assertEqual(first.get("agent_runs_count"), len(rows))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("messages_count" in row for row in rows))
            self.assertTrue(all(str(row.get("status", "")).lower() in {"running", "completed"} for row in rows))
            if first.get("next_cursor"):
                second = orchestrator.tool_executor.execute_read_only(
                    agent=root,
                    action={
                        "type": "list_agent_runs",
                        "status": ["running", "completed"],
                        "limit": 2,
                        "cursor": first.get("next_cursor"),
                    },
                    agents=agents,
                    workspace_manager=workspace_manager,
                )
                second_rows = second.get("agent_runs")
                assert isinstance(second_rows, list)

    async def test_get_agent_run_message_slicing_default_and_cap(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-get-agent-run",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="work",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
                metadata={"created_at": "2026-03-11T10:00:05Z"},
                conversation=[
                    {
                        "role": "assistant",
                        "content": f"m-{idx}",
                        "reasoning": f"r-{idx}",
                        "tool_calls": [{"id": f"call-{idx}"}],
                        "tool_call_id": f"call-{idx}",
                        "debug_only": f"debug-{idx}",
                    }
                    for idx in range(40)
                ],
            )
            agents[child.id] = child

            default_result = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            default_messages = default_result.get("messages")
            assert isinstance(default_messages, list)
            self.assertEqual(len(default_messages), 1)
            self.assertEqual(default_messages[0].get("content"), "m-39")
            self.assertNotIn("debug_only", default_messages[0])
            self.assertEqual(
                set(default_messages[0].keys()),
                {"content", "reasoning", "role", "tool_calls", "tool_call_id"},
            )

            capped = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": 5, "messages_end": 40},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            capped_messages = capped.get("messages")
            assert isinstance(capped_messages, list)
            self.assertEqual(len(capped_messages), 5)
            self.assertEqual(capped_messages[0].get("content"), "m-5")
            self.assertEqual(capped.get("next_messages_start"), 10)
            self.assertIn("only the first 5 messages", str(capped.get("warning", "")))
            self.assertTrue(all("debug_only" not in item for item in capped_messages))
            self.assertTrue(
                all(
                    set(item.keys()) <= {"content", "reasoning", "role", "tool_calls", "tool_call_id"}
                    for item in capped_messages
                )
            )

            sliced = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": 2, "messages_end": 4},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            sliced_messages = sliced.get("messages")
            assert isinstance(sliced_messages, list)
            self.assertEqual([item.get("content") for item in sliced_messages], ["m-2", "m-3"])
            self.assertNotIn("next_messages_start", sliced)
            self.assertNotIn("warning", sliced)

            negative_slice = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": -3, "messages_end": -1},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            negative_messages = negative_slice.get("messages")
            assert isinstance(negative_messages, list)
            self.assertEqual([item.get("content") for item in negative_messages], ["m-37", "m-38"])

            latest_only = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": -1},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            latest_messages = latest_only.get("messages")
            assert isinstance(latest_messages, list)
            self.assertEqual([item.get("content") for item in latest_messages], ["m-39"])

            start_out_of_range = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": -41},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self.assertIn("messages_start", str(start_out_of_range.get("error", "")))
            self.assertIn("out of range", str(start_out_of_range.get("error", "")))

            end_out_of_range = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_end": 41},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self.assertIn("messages_end", str(end_out_of_range.get("error", "")))
            self.assertIn("out of range", str(end_out_of_range.get("error", "")))

            invalid_order = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": -1, "messages_end": -3},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self.assertIn("messages_start", str(invalid_order.get("error", "")))
            self.assertIn("messages_end", str(invalid_order.get("error", "")))
            self.assertIn("must be >=", str(invalid_order.get("error", "")))

    async def test_get_agent_run_skips_soft_injected_messages(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-get-agent-run-skip-soft-injected",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="work",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
                metadata={
                    "created_at": "2026-03-11T10:00:05Z",
                    "compression_excluded_message_indices": [1, 3],
                },
                conversation=[
                    {"role": "assistant", "content": "work-0"},
                    {"role": "user", "content": "context pressure reminder"},
                    {"role": "assistant", "content": "work-1"},
                    {"role": "user", "content": "step limit reminder"},
                    {"role": "assistant", "content": "work-2"},
                ],
            )
            agents[child.id] = child

            all_visible = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": 0, "messages_end": 3},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            visible_messages = all_visible.get("messages")
            assert isinstance(visible_messages, list)
            self.assertEqual(
                [item.get("content") for item in visible_messages],
                ["work-0", "work-1", "work-2"],
            )
            self.assertNotIn("warning", all_visible)
            self.assertNotIn("next_messages_start", all_visible)

            latest_only = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            latest_messages = latest_only.get("messages")
            assert isinstance(latest_messages, list)
            self.assertEqual([item.get("content") for item in latest_messages], ["work-2"])

            negative_slice = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "get_agent_run", "agent_id": child.id, "messages_start": -2, "messages_end": 3},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            negative_messages = negative_slice.get("messages")
            assert isinstance(negative_messages, list)
            self.assertEqual([item.get("content") for item in negative_messages], ["work-1", "work-2"])

    async def test_cancel_agent_non_recursive_only_cancels_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-cancel-agent-non-recursive",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="child",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
            )
            grandchild_workspace = workspace_manager.fork_workspace(child.workspace_id, "agent-grandchild")
            grandchild = AgentNode(
                id="agent-grandchild",
                session_id=session.id,
                name="Grandchild",
                role=AgentRole.WORKER,
                instruction="grandchild",
                workspace_id=grandchild_workspace.id,
                parent_agent_id=child.id,
                status=AgentStatus.RUNNING,
            )
            root.children.append(child.id)
            child.children.append(grandchild.id)
            agents[child.id] = child
            agents[grandchild.id] = grandchild

            result = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "cancel_agent", "agent_id": child.id, "recursive": False},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self.assertEqual(result, {"cancel_agent_status": True})
            self.assertEqual(child.status, AgentStatus.CANCELLED)
            self.assertEqual(child.completion_status, "cancelled")
            self.assertEqual(grandchild.status, AgentStatus.RUNNING)

    async def test_cancel_agent_recursive_cancels_whole_descendant_subtree(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-cancel-agent-recursive",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="child",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
            )
            grandchild_workspace = workspace_manager.fork_workspace(child.workspace_id, "agent-grandchild")
            grandchild = AgentNode(
                id="agent-grandchild",
                session_id=session.id,
                name="Grandchild",
                role=AgentRole.WORKER,
                instruction="grandchild",
                workspace_id=grandchild_workspace.id,
                parent_agent_id=child.id,
                status=AgentStatus.RUNNING,
            )
            root.children.append(child.id)
            child.children.append(grandchild.id)
            agents[child.id] = child
            agents[grandchild.id] = grandchild

            result = orchestrator.tool_executor.execute_read_only(
                agent=root,
                action={"type": "cancel_agent", "agent_id": child.id},
                agents=agents,
                workspace_manager=workspace_manager,
            )
            self.assertEqual(result, {"cancel_agent_status": True})
            self.assertEqual(child.status, AgentStatus.CANCELLED)
            self.assertEqual(child.completion_status, "cancelled")
            self.assertEqual(grandchild.status, AgentStatus.CANCELLED)
            self.assertEqual(grandchild.completion_status, "cancelled")

    async def test_submit_cancel_agent_stops_worker_and_pending_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-submit-cancel-agent",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="child",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
            )
            root.children.append(child.id)
            agents[child.id] = child
            orchestrator.storage.upsert_agent(child)

            worker_task = asyncio.create_task(asyncio.sleep(60))
            orchestrator._active_worker_tasks[child.id] = worker_task

            running_child_tool = ToolRun(
                id="toolrun-child-running",
                session_id=session.id,
                agent_id=child.id,
                tool_name="wait_time",
                arguments={"type": "wait_time", "seconds": 60},
                status=ToolRunStatus.RUNNING,
                blocking=True,
                created_at=utc_now(),
                started_at=utc_now(),
            )
            orchestrator.storage.upsert_tool_run(running_child_tool)
            running_child_task = asyncio.create_task(asyncio.sleep(60))
            orchestrator._active_tool_run_tasks[running_child_tool.id] = running_child_task

            pending_ids = [child.id]
            submit_result = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "cancel_agent", "agent_id": child.id},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=pending_ids,
            )

            projected = submit_result.get("agent_result")
            assert isinstance(projected, dict)
            self.assertEqual(projected, {"cancel_agent_status": True})
            self.assertEqual(child.status, AgentStatus.CANCELLED)
            self.assertEqual(child.completion_status, "cancelled")
            self.assertEqual(pending_ids, [])

            await asyncio.sleep(0)
            self.assertTrue(worker_task.done())
            self.assertTrue(running_child_task.done())
            self.assertNotIn(child.id, orchestrator._active_worker_tasks)
            self.assertNotIn(running_child_tool.id, orchestrator._active_tool_run_tasks)

            refreshed_run = orchestrator.storage.load_tool_run(running_child_tool.id)
            assert isinstance(refreshed_run, dict)
            self.assertEqual(refreshed_run.get("status"), ToolRunStatus.CANCELLED.value)
            events = orchestrator.storage.load_events(session.id)
            self.assertTrue(
                any(
                    event.get("event_type") == "agent_cancelled"
                    and event.get("agent_id") == child.id
                    and str(json.loads(event.get("payload_json", "{}")).get("agent_status", ""))
                    == AgentStatus.CANCELLED.value
                    for event in events
                )
            )

    async def test_submit_cancel_agent_keeps_terminal_descendants_unchanged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-submit-cancel-agent-terminal-descendant",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="child",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
            )
            grandchild_workspace = workspace_manager.fork_workspace(child.workspace_id, "agent-grandchild")
            grandchild = AgentNode(
                id="agent-grandchild",
                session_id=session.id,
                name="Grandchild",
                role=AgentRole.WORKER,
                instruction="grandchild",
                workspace_id=grandchild_workspace.id,
                parent_agent_id=child.id,
                status=AgentStatus.TERMINATED,
                status_reason="terminated_by_interrupt",
            )
            root.children.append(child.id)
            child.children.append(grandchild.id)
            agents[child.id] = child
            agents[grandchild.id] = grandchild
            orchestrator.storage.upsert_agent(child)
            orchestrator.storage.upsert_agent(grandchild)

            pending_ids = [child.id]
            submit_result = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "cancel_agent", "agent_id": child.id},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=pending_ids,
            )

            projected = submit_result.get("agent_result")
            assert isinstance(projected, dict)
            self.assertEqual(projected, {"cancel_agent_status": True})
            self.assertEqual(child.status, AgentStatus.CANCELLED)
            self.assertEqual(grandchild.status, AgentStatus.TERMINATED)
            self.assertEqual(pending_ids, [])

    async def test_terminate_agent_subtree_cascades_and_cancels_related_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-ui-terminate-agent-subtree",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="child",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
            )
            grandchild_workspace = workspace_manager.fork_workspace(child.workspace_id, "agent-grandchild")
            grandchild = AgentNode(
                id="agent-grandchild",
                session_id=session.id,
                name="Grandchild",
                role=AgentRole.WORKER,
                instruction="grandchild",
                workspace_id=grandchild_workspace.id,
                parent_agent_id=child.id,
                status=AgentStatus.RUNNING,
            )
            root.children.append(child.id)
            child.children.append(grandchild.id)
            agents[child.id] = child
            agents[grandchild.id] = grandchild
            orchestrator.storage.upsert_agent(root)
            orchestrator.storage.upsert_agent(child)
            orchestrator.storage.upsert_agent(grandchild)
            orchestrator._live_session_contexts[session.id] = (session, agents, workspace_manager)

            child_worker_task = asyncio.create_task(asyncio.sleep(60))
            grandchild_worker_task = asyncio.create_task(asyncio.sleep(60))
            orchestrator._active_worker_tasks[child.id] = child_worker_task
            orchestrator._active_worker_tasks[grandchild.id] = grandchild_worker_task

            running_child_tool = ToolRun(
                id="toolrun-child-running",
                session_id=session.id,
                agent_id=child.id,
                tool_name="wait_time",
                arguments={"type": "wait_time", "seconds": 60},
                status=ToolRunStatus.RUNNING,
                blocking=True,
                created_at=utc_now(),
                started_at=utc_now(),
            )
            orchestrator.storage.upsert_tool_run(running_child_tool)
            running_child_tool_task = asyncio.create_task(asyncio.sleep(60))
            orchestrator._active_tool_run_tasks[running_child_tool.id] = running_child_tool_task

            result = await orchestrator.terminate_agent_subtree(
                session_id=session.id,
                agent_id=child.id,
                source="tui",
            )
            self.assertEqual(result.get("session_id"), session.id)
            self.assertEqual(result.get("agent_id"), child.id)
            self.assertEqual(result.get("source"), "tui")
            self.assertEqual(set(result.get("target_agent_ids", [])), {child.id, grandchild.id})
            self.assertEqual(set(result.get("cancelled_agent_ids", [])), {child.id, grandchild.id})
            self.assertIn(running_child_tool.id, result.get("cancelled_tool_run_ids", []))

            await asyncio.sleep(0)
            self.assertTrue(child_worker_task.done())
            self.assertTrue(grandchild_worker_task.done())
            self.assertTrue(running_child_tool_task.done())
            self.assertNotIn(child.id, orchestrator._active_worker_tasks)
            self.assertNotIn(grandchild.id, orchestrator._active_worker_tasks)
            self.assertNotIn(running_child_tool.id, orchestrator._active_tool_run_tasks)

            self.assertEqual(root.status, AgentStatus.RUNNING)
            self.assertEqual(child.status, AgentStatus.CANCELLED)
            self.assertEqual(child.completion_status, "cancelled")
            self.assertEqual(grandchild.status, AgentStatus.CANCELLED)
            self.assertEqual(grandchild.completion_status, "cancelled")
            refreshed_run = orchestrator.storage.load_tool_run(running_child_tool.id)
            assert isinstance(refreshed_run, dict)
            self.assertEqual(str(refreshed_run.get("status", "")), ToolRunStatus.CANCELLED.value)

    async def test_wait_run_supports_tool_and_agent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-wait-run",
            )

            tool_run_id = "toolrun-target"
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=tool_run_id,
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="shell",
                    arguments={"type": "shell", "command": "echo ok"},
                    status=ToolRunStatus.COMPLETED,
                    blocking=True,
                    created_at=utc_now(),
                    started_at=utc_now(),
                    completed_at=utc_now(),
                )
            )
            tool_wait = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "wait_run", "tool_run_id": tool_run_id},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            tool_wait_result = tool_wait.get("agent_result")
            assert isinstance(tool_wait_result, dict)
            self.assertEqual(tool_wait_result, {"wait_run_status": True})

            paused_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-paused")
            paused = AgentNode(
                id="agent-paused",
                session_id=session.id,
                name="Paused",
                role=AgentRole.WORKER,
                instruction="wait",
                workspace_id=paused_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.PAUSED,
            )
            agents[paused.id] = paused
            paused_wait = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "wait_run", "agent_id": paused.id},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            paused_wait_result = paused_wait.get("agent_result")
            assert isinstance(paused_wait_result, dict)
            self.assertEqual(paused_wait_result.get("wait_run_status"), False)
            self.assertIn("paused", str(paused_wait_result.get("error", "")))

    async def test_cancel_tool_run_completed_spawn_noop_and_running_spawn_terminates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-cancel-run",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="work",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
            )
            root.children.append(child.id)
            agents[child.id] = child

            child_running_run = "toolrun-child-running-before-noop"
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=child_running_run,
                    session_id=session.id,
                    agent_id=child.id,
                    tool_name="shell",
                    arguments={"type": "shell", "command": "sleep 10"},
                    status=ToolRunStatus.RUNNING,
                    blocking=True,
                    created_at=utc_now(),
                    started_at=utc_now(),
                )
            )
            completed_spawn = "toolrun-spawn-completed"
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=completed_spawn,
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="spawn_agent",
                    arguments={"type": "spawn_agent", "instruction": "work", "child_agent_id": child.id},
                    status=ToolRunStatus.COMPLETED,
                    blocking=True,
                    created_at=utc_now(),
                    started_at=utc_now(),
                    completed_at=utc_now(),
                    result={"child_agent_id": child.id},
                )
            )
            noop = await orchestrator._tool_run_cancel_result(
                session=session,
                agent=root,
                action={"type": "cancel_tool_run", "tool_run_id": completed_spawn},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            self.assertEqual(noop, {"final_status": "completed", "cancelled_agents_count": 0})
            self.assertEqual(child.status, AgentStatus.RUNNING)
            self.assertEqual(
                str(orchestrator.storage.load_tool_run(child_running_run).get("status", "")).lower(),
                ToolRunStatus.RUNNING.value,
            )

            running_spawn = "toolrun-spawn-running"
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=running_spawn,
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="spawn_agent",
                    arguments={"type": "spawn_agent", "instruction": "work", "child_agent_id": child.id},
                    status=ToolRunStatus.RUNNING,
                    blocking=True,
                    created_at=utc_now(),
                    started_at=utc_now(),
                )
            )
            task = asyncio.create_task(asyncio.sleep(60))
            orchestrator._active_tool_run_tasks[running_spawn] = task
            try:
                cancelled = await orchestrator._tool_run_cancel_result(
                    session=session,
                    agent=root,
                    action={"type": "cancel_tool_run", "tool_run_id": running_spawn},
                    agents=agents,
                    workspace_manager=workspace_manager,
                    root_loop=0,
                    tracked_pending_ids=[],
                )
            finally:
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(cancelled.get("final_status"), "cancelled")
            self.assertEqual(cancelled.get("cancelled_agents_count"), 1)
            self.assertEqual(child.status, AgentStatus.CANCELLED)

    async def test_cancel_tool_run_running_spawn_cascades_to_subtree_tool_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-cancel-run-subtree-tool-runs",
            )

            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-child")
            child = AgentNode(
                id="agent-child",
                session_id=session.id,
                name="Child",
                role=AgentRole.WORKER,
                instruction="child",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
            )
            grandchild_workspace = workspace_manager.fork_workspace(child.workspace_id, "agent-grandchild")
            grandchild = AgentNode(
                id="agent-grandchild",
                session_id=session.id,
                name="Grandchild",
                role=AgentRole.WORKER,
                instruction="grandchild",
                workspace_id=grandchild_workspace.id,
                parent_agent_id=child.id,
                status=AgentStatus.RUNNING,
            )
            root.children.append(child.id)
            child.children.append(grandchild.id)
            agents[child.id] = child
            agents[grandchild.id] = grandchild

            running_spawn = "toolrun-spawn-running"
            child_shell = "toolrun-child-shell"
            grandchild_wait = "toolrun-grandchild-wait"
            unrelated_root = "toolrun-root-unrelated"
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=running_spawn,
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="spawn_agent",
                    arguments={"type": "spawn_agent", "instruction": "child", "child_agent_id": child.id},
                    status=ToolRunStatus.RUNNING,
                    blocking=True,
                    created_at=utc_now(),
                    started_at=utc_now(),
                )
            )
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=child_shell,
                    session_id=session.id,
                    agent_id=child.id,
                    tool_name="shell",
                    arguments={"type": "shell", "command": "sleep 30"},
                    status=ToolRunStatus.QUEUED,
                    blocking=True,
                    created_at=utc_now(),
                )
            )
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=grandchild_wait,
                    session_id=session.id,
                    agent_id=grandchild.id,
                    tool_name="wait_time",
                    arguments={"type": "wait_time", "seconds": 30},
                    status=ToolRunStatus.RUNNING,
                    blocking=True,
                    created_at=utc_now(),
                    started_at=utc_now(),
                )
            )
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=unrelated_root,
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="shell",
                    arguments={"type": "shell", "command": "echo keep"},
                    status=ToolRunStatus.QUEUED,
                    blocking=True,
                    created_at=utc_now(),
                )
            )
            task = asyncio.create_task(asyncio.sleep(60))
            orchestrator._active_tool_run_tasks[running_spawn] = task
            tracked_pending_ids = [child.id, grandchild.id, "agent-keep"]
            try:
                cancelled = await orchestrator._tool_run_cancel_result(
                    session=session,
                    agent=root,
                    action={"type": "cancel_tool_run", "tool_run_id": running_spawn},
                    agents=agents,
                    workspace_manager=workspace_manager,
                    root_loop=0,
                    tracked_pending_ids=tracked_pending_ids,
                )
            finally:
                await asyncio.gather(task, return_exceptions=True)

            self.assertEqual(cancelled.get("final_status"), "cancelled")
            self.assertEqual(cancelled.get("cancelled_agents_count"), 2)
            self.assertEqual(child.status, AgentStatus.CANCELLED)
            self.assertEqual(grandchild.status, AgentStatus.CANCELLED)
            self.assertEqual(
                str(orchestrator.storage.load_tool_run(child_shell).get("status", "")).lower(),
                ToolRunStatus.CANCELLED.value,
            )
            self.assertEqual(
                str(orchestrator.storage.load_tool_run(grandchild_wait).get("status", "")).lower(),
                ToolRunStatus.CANCELLED.value,
            )
            self.assertEqual(
                str(orchestrator.storage.load_tool_run(unrelated_root).get("status", "")).lower(),
                ToolRunStatus.QUEUED.value,
            )
            self.assertEqual(tracked_pending_ids, ["agent-keep"])

    async def test_wait_time_finish_and_cancel_projection_contract(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-projection",
            )

            waited = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "wait_time", "seconds": 10},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            waited_result = waited.get("agent_result")
            assert isinstance(waited_result, dict)
            self.assertEqual(waited_result, {"wait_time_status": True})

            queued_id = "toolrun-queued"
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id=queued_id,
                    session_id=session.id,
                    agent_id=root.id,
                    tool_name="shell",
                    arguments={"type": "shell", "command": "sleep 10"},
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
            cancelled_result = cancelled.get("agent_result")
            assert isinstance(cancelled_result, dict)
            self.assertEqual(set(cancelled_result.keys()), {"final_status", "cancelled_agents_count"})

            finished = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "finish", "status": "completed", "summary": "done"},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            finished_result = finished.get("agent_result")
            assert isinstance(finished_result, dict)
            self.assertEqual(set(finished_result.keys()), {"accepted"})

    async def test_shell_inline_wait_completes_within_threshold(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-shell-inline-complete",
            )
            orchestrator.config.runtime.tools.shell_inline_wait_seconds = 0.2

            async def _fast_shell(  # type: ignore[no-untyped-def]
                _agent,
                _action,
                _workspace_manager,
                *,
                stream_listener=None,
            ):
                if stream_listener is not None:
                    await stream_listener("stdout", "inline-fast\n")
                await asyncio.sleep(0.01)
                return {
                    "exit_code": 0,
                    "stdout": "inline-fast\n",
                    "stderr": "",
                    "duration_ms": 10,
                }

            orchestrator.tool_executor.execute_shell = _fast_shell  # type: ignore[method-assign]

            submitted = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "shell", "command": "echo inline-fast"},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            projected = submitted.get("agent_result")
            assert isinstance(projected, dict)
            self.assertEqual(projected.get("exit_code"), 0)
            self.assertNotIn("status", projected)
            self.assertNotIn("background", projected)
            self.assertEqual(projected.get("stdout"), "inline-fast\n")

            latest = orchestrator.storage.list_tool_runs(session_id=session.id, limit=1)
            self.assertEqual(len(latest), 1)
            self.assertEqual(str(latest[0].get("status", "")).lower(), ToolRunStatus.COMPLETED.value)

    async def test_shell_inline_wait_returns_running_and_get_tool_run_includes_accumulated_output(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-shell-inline-running",
            )
            orchestrator.config.runtime.tools.shell_inline_wait_seconds = 0.05

            async def _slow_shell(  # type: ignore[no-untyped-def]
                _agent,
                _action,
                _workspace_manager,
                *,
                stream_listener=None,
            ):
                if stream_listener is not None:
                    await stream_listener("stdout", "partial-stdout\n")
                    await stream_listener("stderr", "partial-stderr\n")
                await asyncio.sleep(0.2)
                return {
                    "exit_code": 0,
                    "stdout": "final-stdout\n",
                    "stderr": "final-stderr\n",
                    "duration_ms": 200,
                }

            orchestrator.tool_executor.execute_shell = _slow_shell  # type: ignore[method-assign]

            submitted = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "shell", "command": "sleep 1"},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            projected = submitted.get("agent_result")
            assert isinstance(projected, dict)
            self.assertEqual(projected.get("status"), ToolRunStatus.RUNNING.value)
            self.assertEqual(projected.get("background"), True)
            shell_run_id = str(projected.get("tool_run_id", "")).strip()
            self.assertTrue(shell_run_id.startswith("toolrun-"))
            self.assertEqual(projected.get("stdout"), "partial-stdout\n")
            self.assertEqual(projected.get("stderr"), "partial-stderr\n")
            self.assertIn("background", str(projected.get("warning", "")).lower())

            running_details = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "get_tool_run", "tool_run_id": shell_run_id},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            running_payload = running_details.get("agent_result")
            assert isinstance(running_payload, dict)
            running_tool = running_payload.get("tool_run")
            assert isinstance(running_tool, dict)
            self.assertEqual(running_tool.get("status"), ToolRunStatus.RUNNING.value)
            self.assertEqual(running_tool.get("stdout"), "partial-stdout\n")
            self.assertEqual(running_tool.get("stderr"), "partial-stderr\n")

            await asyncio.sleep(0.25)

            final_record = orchestrator.storage.load_tool_run(shell_run_id)
            assert isinstance(final_record, dict)
            self.assertEqual(
                str(final_record.get("status", "")).lower(),
                ToolRunStatus.COMPLETED.value,
            )
            self.assertNotIn(shell_run_id, orchestrator._tool_run_shell_streams)

            final_details = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "get_tool_run", "tool_run_id": shell_run_id},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            final_payload = final_details.get("agent_result")
            assert isinstance(final_payload, dict)
            final_tool = final_payload.get("tool_run")
            assert isinstance(final_tool, dict)
            self.assertEqual(final_tool.get("status"), ToolRunStatus.COMPLETED.value)
            self.assertEqual(final_tool.get("stdout"), "final-stdout\n")
            self.assertEqual(final_tool.get("stderr"), "final-stderr\n")

    async def test_execute_agent_actions_defers_compress_until_shell_finishes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-compress-after-shell",
            )
            orchestrator.config.runtime.tools.shell_inline_wait_seconds = 0.01
            root.step_count = 1
            root.metadata["message_index_to_step"] = [1]

            execution_order: list[str] = []
            compression_snapshots: list[str] = []

            async def _slow_shell(  # type: ignore[no-untyped-def]
                _agent,
                _action,
                _workspace_manager,
                *,
                stream_listener=None,
            ):
                execution_order.append("shell")
                if stream_listener is not None:
                    await stream_listener("stdout", "partial-stdout\n")
                await asyncio.sleep(0.05)
                return {
                    "exit_code": 0,
                    "stdout": "final-stdout\n",
                    "stderr": "",
                    "duration_ms": 50,
                }

            async def _fake_compress_context(  # type: ignore[no-untyped-def]
                agent,
                *,
                llm_client,
                reason,
                overflow_detail=None,
            ):
                del llm_client, overflow_detail
                execution_order.append("compress_context")
                compression_snapshots.append(json.dumps(agent.conversation, ensure_ascii=False))
                return {
                    "compressed": True,
                    "reason": reason,
                    "summary_version": 1,
                    "message_range": {"start": 0, "end": len(agent.conversation) - 1},
                    "step_range": {"start": 1, "end": 1},
                    "context_tokens_before": 10,
                    "context_tokens_after": 5,
                    "context_limit_tokens": 100,
                }

            orchestrator.tool_executor.execute_shell = _slow_shell  # type: ignore[method-assign]
            orchestrator.agent_runtime.compress_context = _fake_compress_context  # type: ignore[method-assign]

            result = await orchestrator._execute_agent_actions(
                session=session,
                agent=root,
                actions=[
                    {"type": "compress_context"},
                    {"type": "shell", "command": "sleep 1"},
                ],
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )

            self.assertIsNone(result.finish_payload)
            self.assertEqual(execution_order, ["shell", "compress_context"])
            self.assertEqual(len(compression_snapshots), 1)
            self.assertIn("final-stdout", compression_snapshots[0])
            self.assertNotIn('"status": "running"', compression_snapshots[0])
            self.assertNotIn('"background": true', compression_snapshots[0].lower())

    async def test_execute_agent_actions_runs_compress_before_finish_even_if_finish_is_first(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-finish-before-compress",
            )
            root.step_count = 1
            root.metadata["message_index_to_step"] = [1]

            execution_order: list[str] = []

            async def _fake_shell(  # type: ignore[no-untyped-def]
                _agent,
                _action,
                _workspace_manager,
                *,
                stream_listener=None,
            ):
                del stream_listener
                execution_order.append("shell")
                return {
                    "exit_code": 0,
                    "stdout": "shell-done\n",
                    "stderr": "",
                    "duration_ms": 1,
                }

            async def _fake_compress_context(  # type: ignore[no-untyped-def]
                _agent,
                *,
                llm_client,
                reason,
                overflow_detail=None,
            ):
                del llm_client, reason, overflow_detail
                execution_order.append("compress_context")
                return {
                    "compressed": True,
                    "reason": "manual",
                    "summary_version": 1,
                    "message_range": {"start": 0, "end": 2},
                    "step_range": {"start": 1, "end": 1},
                    "context_tokens_before": 10,
                    "context_tokens_after": 5,
                    "context_limit_tokens": 100,
                }

            orchestrator.tool_executor.execute_shell = _fake_shell  # type: ignore[method-assign]
            orchestrator.agent_runtime.compress_context = _fake_compress_context  # type: ignore[method-assign]

            result = await orchestrator._execute_agent_actions(
                session=session,
                agent=root,
                actions=[
                    {
                        "type": "finish",
                        "status": "completed",
                        "summary": "done",
                    },
                    {"type": "compress_context"},
                    {"type": "shell", "command": "echo ok"},
                ],
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )

            assert result.finish_payload is not None
            self.assertEqual(result.finish_payload.get("completion_state"), "completed")
            self.assertEqual(result.finish_payload.get("user_summary"), "done")
            self.assertEqual(execution_order, ["shell", "compress_context"])

    async def test_shell_inline_wait_running_output_is_truncated(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, session, workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-shell-inline-truncate",
            )
            orchestrator.config.runtime.tools.shell_inline_wait_seconds = 0.0
            huge_stdout = "x" * 9005

            async def _slow_shell(  # type: ignore[no-untyped-def]
                _agent,
                _action,
                _workspace_manager,
                *,
                stream_listener=None,
            ):
                if stream_listener is not None:
                    await stream_listener("stdout", huge_stdout)
                await asyncio.sleep(0.15)
                return {
                    "exit_code": 0,
                    "stdout": "done\n",
                    "stderr": "",
                    "duration_ms": 150,
                }

            orchestrator.tool_executor.execute_shell = _slow_shell  # type: ignore[method-assign]

            submitted = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={"type": "shell", "command": "python -c 'print(1)'"},
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            projected = submitted.get("agent_result")
            assert isinstance(projected, dict)
            self.assertEqual(projected.get("status"), ToolRunStatus.RUNNING.value)
            running_stdout = str(projected.get("stdout", ""))
            self.assertLessEqual(len(running_stdout), 8000)
            self.assertIn("[truncated]", running_stdout)

    async def test_has_runnable_agents_is_true_while_tool_run_task_is_active(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator, _session, _workspace_manager, root, agents = bootstrap_runtime(
                project_dir,
                session_id="session-tool-task-runnable",
            )
            root.status = AgentStatus.COMPLETED
            agents[root.id] = root
            task = asyncio.create_task(asyncio.sleep(5))
            orchestrator._active_tool_run_tasks["toolrun-active"] = task
            try:
                self.assertTrue(orchestrator._has_runnable_agents(agents, run_root_agent=True))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                orchestrator._active_tool_run_tasks = {}
