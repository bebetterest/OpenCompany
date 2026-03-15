from __future__ import annotations

import asyncio
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    CheckpointState,
    EventRecord,
    RunSession,
    ShellCommandResult,
    SessionStatus,
    ToolRun,
    ToolRunStatus,
    WorkspaceMode,
)
from opencompany.orchestrator import Orchestrator
from opencompany.storage import Storage
from opencompany.utils import utc_now
from opencompany.workspace import WorkspaceManager
from test_orchestrator import BlockingWorkerLLMClient, FakeLLMClient, build_test_project


class OrchestratorResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_stops_active_run_without_following_later_steps(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            llm = BlockingWorkerLLMClient()
            orchestrator.llm_client = llm

            task = asyncio.create_task(orchestrator.run_task("Inspect this project"))
            await asyncio.wait_for(llm.started.wait(), timeout=1)
            orchestrator.request_interrupt()
            session = await asyncio.wait_for(task, timeout=1)

            self.assertEqual(session.status.value, "interrupted")
            self.assertEqual(session.completion_state, None)
            self.assertLessEqual(llm.calls, 3)
            stored = Storage(project_dir / ".opencompany" / "opencompany.db").load_session(session.id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["status"], "interrupted")
            agent_rows = Storage(project_dir / ".opencompany" / "opencompany.db").load_agents(session.id)
            self.assertTrue(agent_rows)
            active_statuses = {"running", "pending"}
            self.assertFalse(any(str(row.get("status")) in active_statuses for row in agent_rows))
            self.assertTrue(any(str(row.get("status")) == "terminated" for row in agent_rows))

    async def test_run_task_in_session_appends_new_root_agent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-run-existing"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root = AgentNode(
                id="agent-root-initial",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="initial task",
                workspace_id=root_workspace.id,
                status=AgentStatus.COMPLETED,
                completion_status="completed",
                conversation=[{"role": "user", "content": "initial task"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="initial task",
                locale="en",
                root_agent_id=root.id,
                status=SessionStatus.COMPLETED,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root)
            orchestrator._sync_agent_messages(root)
            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={root.id: orchestrator._agent_state(root)},
                workspaces=workspace_manager.serialize(),
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=0,
                interrupted=False,
            )
            orchestrator.storage.save_checkpoint(session_id, now, checkpoint_state)

            captured: dict[str, object] = {}

            async def fake_run_session(
                *,
                session: RunSession,
                agents: dict[str, AgentNode],
                workspace_manager: WorkspaceManager,
                pending_agent_ids: list[str],
                root_loop: int,
                run_root_agent: bool = True,
                focus_agent_id: str | None = None,
            ) -> None:
                del workspace_manager, pending_agent_ids, root_loop, run_root_agent, focus_agent_id
                captured["new_root_agent_id"] = session.root_agent_id
                captured["root_agent_count"] = len(
                    [node for node in agents.values() if node.role == AgentRole.ROOT]
                )
                new_root = agents[session.root_agent_id]
                captured["new_root_agent_name"] = new_root.name
                captured["new_root_first_message"] = (
                    str(new_root.conversation[0].get("content", ""))
                    if new_root.conversation
                    else ""
                )
                session.status = SessionStatus.INTERRUPTED
                session.updated_at = utc_now()
                orchestrator.storage.upsert_session(session)

            orchestrator._run_session = fake_run_session  # type: ignore[method-assign]
            resumed = await orchestrator.run_task_in_session(
                session_id,
                "second run task",
                model="openai/gpt-4.1",
            )
            self.assertEqual(resumed.id, session_id)
            self.assertEqual(resumed.status, SessionStatus.INTERRUPTED)
            self.assertIsNotNone(captured.get("new_root_agent_id"))
            new_root_agent_id = str(captured.get("new_root_agent_id"))
            self.assertNotEqual(new_root_agent_id, root.id)
            self.assertEqual(captured.get("root_agent_count"), 2)
            self.assertEqual(captured.get("new_root_agent_name"), "Root Coordinator (2)")
            self.assertTrue(
                str(captured.get("new_root_first_message", "")).startswith(
                    f"You are Root Coordinator (2) (agent id: {new_root_agent_id}).\n"
                    "You have no parent agent.\n"
                )
            )
            session_row = orchestrator.storage.load_session(session_id)
            self.assertIsNotNone(session_row)
            assert session_row is not None
            self.assertEqual(str(session_row.get("root_agent_id", "")), new_root_agent_id)
            self.assertEqual(str(session_row.get("task", "")), "second run task")

    async def test_submit_run_in_active_session_appends_running_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-active-live-run"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root = AgentNode(
                id="agent-root-current",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="current task",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "current task"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="current task",
                locale="en",
                root_agent_id=root.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root)
            orchestrator._sync_agent_messages(root)
            agents = {root.id: root}
            orchestrator._live_session_contexts[session.id] = (session, agents, workspace_manager)

            submitted = orchestrator.submit_run_in_active_session(
                session_id,
                "new live root task",
                model="openai/gpt-4.1",
                source="webui",
            )
            new_root_id = str(submitted.get("root_agent_id", ""))
            self.assertTrue(new_root_id)
            self.assertNotEqual(new_root_id, root.id)
            self.assertIn(new_root_id, agents)
            self.assertEqual(agents[new_root_id].role, AgentRole.ROOT)
            self.assertEqual(agents[new_root_id].status, AgentStatus.RUNNING)
            self.assertEqual(agents[new_root_id].name, "Root Coordinator (2)")
            self.assertEqual(agents[new_root_id].instruction, "new live root task")
            self.assertTrue(agents[new_root_id].conversation)
            self.assertTrue(
                str(agents[new_root_id].conversation[0].get("content", "")).startswith(
                    f"You are Root Coordinator (2) (agent id: {new_root_id}).\n"
                    "You have no parent agent.\n"
                )
            )
            self.assertEqual(session.root_agent_id, new_root_id)
            self.assertEqual(session.task, "new live root task")
            session_row = orchestrator.storage.load_session(session_id)
            self.assertIsNotNone(session_row)
            assert session_row is not None
            self.assertEqual(str(session_row.get("root_agent_id", "")), new_root_id)

    async def test_submit_run_in_active_session_uses_custom_root_agent_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-active-custom-root-name"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root = AgentNode(
                id="agent-root-current",
                session_id=session_id,
                name="Planner Root",
                role=AgentRole.ROOT,
                instruction="current task",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "current task"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="current task",
                locale="en",
                root_agent_id=root.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root)
            orchestrator._sync_agent_messages(root)
            agents = {root.id: root}
            orchestrator._live_session_contexts[session.id] = (session, agents, workspace_manager)

            submitted = orchestrator.submit_run_in_active_session(
                session_id,
                "new live root task",
                model="openai/gpt-4.1",
                root_agent_name="Planner Root",
                source="webui",
            )
            new_root_id = str(submitted.get("root_agent_id", ""))
            self.assertTrue(new_root_id)
            self.assertNotEqual(new_root_id, root.id)
            self.assertIn(new_root_id, agents)
            self.assertEqual(agents[new_root_id].name, "Planner Root (2)")
            self.assertTrue(
                str(agents[new_root_id].conversation[0].get("content", "")).startswith(
                    f"You are Planner Root (2) (agent id: {new_root_id}).\n"
                    "You have no parent agent.\n"
                )
            )

    async def test_finalize_root_keeps_session_running_when_other_root_is_active(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-multi-root-finalize"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            finishing_root = AgentNode(
                id="agent-root-finishing",
                session_id=session_id,
                name="Root Finishing",
                role=AgentRole.ROOT,
                instruction="finishing task",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "finishing task"}],
            )
            active_root = AgentNode(
                id="agent-root-active",
                session_id=session_id,
                name="Root Active",
                role=AgentRole.ROOT,
                instruction="active task",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "active task"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="multi root task",
                locale="en",
                root_agent_id=finishing_root.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
            agents = {
                finishing_root.id: finishing_root,
                active_root.id: active_root,
            }
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(finishing_root)
            orchestrator.storage.upsert_agent(active_root)
            orchestrator._sync_agent_messages(finishing_root)
            orchestrator._sync_agent_messages(active_root)

            await orchestrator._finalize_root(
                session=session,
                root_agent=finishing_root,
                payload={
                    "user_summary": "finishing root summary",
                    "completion_state": "completed",
                },
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=0,
            )

            self.assertEqual(session.status, SessionStatus.RUNNING)
            self.assertEqual(finishing_root.status, AgentStatus.COMPLETED)
            self.assertEqual(active_root.status, AgentStatus.RUNNING)
            session_row = orchestrator.storage.load_session(session_id)
            self.assertIsNotNone(session_row)
            assert session_row is not None
            self.assertEqual(str(session_row.get("status", "")), SessionStatus.RUNNING.value)

    async def test_resume_with_root_target_switches_executing_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-resume-root-target"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            current_root = AgentNode(
                id="agent-root-current",
                session_id=session_id,
                name="Root Current",
                role=AgentRole.ROOT,
                instruction="current root instruction",
                workspace_id=root_workspace.id,
                status=AgentStatus.PAUSED,
                conversation=[{"role": "user", "content": "current root instruction"}],
            )
            steered_root = AgentNode(
                id="agent-root-steered",
                session_id=session_id,
                name="Root Steered",
                role=AgentRole.ROOT,
                instruction="steered root instruction",
                workspace_id=root_workspace.id,
                status=AgentStatus.COMPLETED,
                completion_status="completed",
                conversation=[{"role": "user", "content": "steered root instruction"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="resume root target",
                locale="en",
                root_agent_id=current_root.id,
                status=SessionStatus.INTERRUPTED,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(current_root)
            orchestrator.storage.upsert_agent(steered_root)
            orchestrator._sync_agent_messages(current_root)
            orchestrator._sync_agent_messages(steered_root)
            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={
                    current_root.id: orchestrator._agent_state(current_root),
                    steered_root.id: orchestrator._agent_state(steered_root),
                },
                workspaces=workspace_manager.serialize(),
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=0,
                interrupted=True,
            )
            orchestrator.storage.save_checkpoint(session_id, now, checkpoint_state)

            captured: dict[str, object] = {}

            async def fake_run_session(
                *,
                session: RunSession,
                agents: dict[str, AgentNode],
                workspace_manager: WorkspaceManager,
                pending_agent_ids: list[str],
                root_loop: int,
                run_root_agent: bool = True,
                focus_agent_id: str | None = None,
            ) -> None:
                del workspace_manager, pending_agent_ids, root_loop, focus_agent_id
                captured["run_root_agent"] = run_root_agent
                captured["executing_root_agent_id"] = session.root_agent_id
                captured["current_root_status"] = agents[current_root.id].status
                captured["steered_root_status"] = agents[steered_root.id].status
                session.status = SessionStatus.INTERRUPTED
                session.updated_at = utc_now()
                orchestrator.storage.upsert_session(session)

            orchestrator._run_session = fake_run_session  # type: ignore[method-assign]
            resumed = await orchestrator.resume(
                session_id,
                "resume steered root",
                reactivate_agent_id=steered_root.id,
                run_root_agent=True,
            )
            self.assertEqual(resumed.status, SessionStatus.INTERRUPTED)
            self.assertEqual(captured.get("run_root_agent"), True)
            self.assertEqual(captured.get("executing_root_agent_id"), steered_root.id)
            self.assertEqual(captured.get("current_root_status"), AgentStatus.PAUSED)
            self.assertEqual(captured.get("steered_root_status"), AgentStatus.RUNNING)

    async def test_resume_requeues_pending_agent_from_checkpoint(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "opencompany-app"
            project_dir = root / "target-project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_project(app_dir)
            (project_dir / "README.md").write_text("target\n", encoding="utf-8")
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "spawn_agent",
                                    "name": "Prepare",
                                    "instruction": "Prepare files",
                                }
                            ]
                        }
                    )
                ]
            )
            original_spawn_child = orchestrator._spawn_child

            def interrupting_spawn(*args, **kwargs):
                agent_id = original_spawn_child(*args, **kwargs)
                orchestrator.request_interrupt()
                return agent_id

            orchestrator._spawn_child = interrupting_spawn  # type: ignore[method-assign]
            interrupted = await orchestrator.run_task("Prepare this project")
            self.assertEqual(interrupted.status.value, "interrupted")

            resumed = Orchestrator(root / "unused-target", locale="en", app_dir=app_dir)
            resumed.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Resume succeeded.",
                                }
                            ]
                        }
                    )
                ]
                * 10
            )
            resume_instruction = "Continue from checkpoint with latest user instruction."
            session = await resumed.resume(interrupted.id, resume_instruction)
            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Resume succeeded.")
            self.assertEqual(resumed.project_dir, project_dir.resolve())
            page = resumed.list_session_messages(
                session.id,
                agent_id=session.root_agent_id,
                tail=100,
            )
            messages = page.get("messages", [])
            self.assertTrue(
                any(
                    str(item.get("role", "")) == "user"
                    and str((item.get("message") or {}).get("content", "")) == resume_instruction
                    for item in messages
                )
            )

    async def test_resume_reactivates_requested_agent_before_scheduling(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-resume-reactivate-agent"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root_agent = AgentNode(
                id="agent-root-resume-reactivate",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Continue session",
                workspace_id=root_workspace.id,
                status=AgentStatus.PAUSED,
                conversation=[{"role": "user", "content": "continue"}],
            )
            worker = AgentNode(
                id="agent-worker-resume-reactivate",
                session_id=session_id,
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Do worker task",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.COMPLETED,
                completion_status="completed",
                conversation=[{"role": "assistant", "content": "done"}],
            )
            root_agent.children = [worker.id]
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Resume with steer activation",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.INTERRUPTED,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator.storage.upsert_agent(worker)
            orchestrator._sync_agent_messages(root_agent)
            orchestrator._sync_agent_messages(worker)
            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={
                    root_agent.id: orchestrator._agent_state(root_agent),
                    worker.id: orchestrator._agent_state(worker),
                },
                workspaces=workspace_manager.serialize(),
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=0,
                interrupted=True,
            )
            orchestrator.storage.save_checkpoint(session_id, now, checkpoint_state)

            captured: dict[str, object] = {}

            async def fake_run_session(
                *,
                session: RunSession,
                agents: dict[str, AgentNode],
                workspace_manager: WorkspaceManager,
                pending_agent_ids: list[str],
                root_loop: int,
                run_root_agent: bool = True,
                focus_agent_id: str | None = None,
            ) -> None:
                del workspace_manager, pending_agent_ids, root_loop
                captured["worker_status"] = agents[worker.id].status
                captured["worker_completion_status"] = agents[worker.id].completion_status
                captured["run_root_agent"] = run_root_agent
                captured["focus_agent_id"] = focus_agent_id
                session.status = SessionStatus.INTERRUPTED
                session.updated_at = utc_now()
                orchestrator.storage.upsert_session(session)

            orchestrator._run_session = fake_run_session  # type: ignore[method-assign]
            resumed = await orchestrator.resume(
                session_id,
                "Resume after inactive-session steer",
                reactivate_agent_id=worker.id,
            )
            self.assertEqual(resumed.status, SessionStatus.INTERRUPTED)
            self.assertEqual(captured.get("worker_status"), AgentStatus.RUNNING)
            self.assertIsNone(captured.get("worker_completion_status"))
            self.assertEqual(captured.get("run_root_agent"), True)
            self.assertIsNone(captured.get("focus_agent_id"))
            worker_row = next(
                (
                    row
                    for row in orchestrator.storage.load_agents(session_id)
                    if str(row.get("id", "")) == worker.id
                ),
                None,
            )
            self.assertIsNotNone(worker_row)
            assert worker_row is not None
            self.assertEqual(str(worker_row.get("status", "")), AgentStatus.RUNNING.value)
            self.assertIsNone(worker_row.get("completion_status"))

    async def test_resume_focus_non_root_skips_root_reactivation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-resume-focus-worker"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root_agent = AgentNode(
                id="agent-root-focus",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Continue session",
                workspace_id=root_workspace.id,
                status=AgentStatus.PAUSED,
                conversation=[{"role": "user", "content": "continue"}],
            )
            worker = AgentNode(
                id="agent-worker-focus",
                session_id=session_id,
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Do worker task",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.COMPLETED,
                completion_status="completed",
                conversation=[{"role": "assistant", "content": "done"}],
            )
            root_agent.children = [worker.id]
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Resume focused worker",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.INTERRUPTED,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator.storage.upsert_agent(worker)
            orchestrator._sync_agent_messages(root_agent)
            orchestrator._sync_agent_messages(worker)
            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={
                    root_agent.id: orchestrator._agent_state(root_agent),
                    worker.id: orchestrator._agent_state(worker),
                },
                workspaces=workspace_manager.serialize(),
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=0,
                interrupted=True,
            )
            orchestrator.storage.save_checkpoint(session_id, now, checkpoint_state)

            captured: dict[str, object] = {}

            async def fake_run_session(
                *,
                session: RunSession,
                agents: dict[str, AgentNode],
                workspace_manager: WorkspaceManager,
                pending_agent_ids: list[str],
                root_loop: int,
                run_root_agent: bool = True,
                focus_agent_id: str | None = None,
            ) -> None:
                del workspace_manager, pending_agent_ids, root_loop
                captured["run_root_agent"] = run_root_agent
                captured["focus_agent_id"] = focus_agent_id
                captured["root_status"] = agents[root_agent.id].status
                captured["root_messages"] = len(agents[root_agent.id].conversation)
                captured["worker_status"] = agents[worker.id].status
                session.status = SessionStatus.INTERRUPTED
                session.updated_at = utc_now()
                orchestrator.storage.upsert_session(session)

            orchestrator._run_session = fake_run_session  # type: ignore[method-assign]
            resumed = await orchestrator.resume(
                session_id,
                "Resume worker only",
                reactivate_agent_id=worker.id,
                run_root_agent=False,
            )
            self.assertEqual(resumed.status, SessionStatus.INTERRUPTED)
            self.assertEqual(captured.get("run_root_agent"), False)
            self.assertEqual(captured.get("focus_agent_id"), worker.id)
            self.assertEqual(captured.get("root_status"), AgentStatus.PAUSED)
            self.assertEqual(captured.get("root_messages"), 1)
            self.assertEqual(captured.get("worker_status"), AgentStatus.RUNNING)

    async def test_save_checkpoint_derives_pending_workers_from_live_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-checkpoint-derived-pending"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root_agent = AgentNode(
                id="agent-root-derived",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Continue session",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "continue"}],
            )
            running_worker = AgentNode(
                id="agent-worker-running",
                session_id=session_id,
                name="Running Worker",
                role=AgentRole.WORKER,
                instruction="Still running",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "work"}],
            )
            completed_worker = AgentNode(
                id="agent-worker-completed",
                session_id=session_id,
                name="Completed Worker",
                role=AgentRole.WORKER,
                instruction="Already done",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.COMPLETED,
                completion_status="completed",
                conversation=[{"role": "assistant", "content": "done"}],
            )
            root_agent.children = [running_worker.id, completed_worker.id]
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Checkpoint pending workers",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator.storage.upsert_agent(running_worker)
            orchestrator.storage.upsert_agent(completed_worker)

            orchestrator._save_checkpoint(
                session=session,
                agents={
                    root_agent.id: root_agent,
                    running_worker.id: running_worker,
                    completed_worker.id: completed_worker,
                },
                workspace_manager=workspace_manager,
                pending_agent_ids=["agent-stale", completed_worker.id],
                root_loop=3,
            )

            latest_checkpoint = orchestrator.storage.latest_checkpoint(session_id)
            self.assertIsNotNone(latest_checkpoint)
            assert latest_checkpoint is not None
            self.assertEqual(
                latest_checkpoint["state"]["pending_agent_ids"],
                [running_worker.id],
            )

    async def test_load_session_context_pauses_active_agents_and_cancels_pending_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "opencompany-app"
            project_dir = root / "target-project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_project(app_dir)
            (project_dir / "README.md").write_text("target\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            session_id = "session-context-import"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            (root_workspace.path / "sandbox-marker.txt").write_text(
                "sandbox state",
                encoding="utf-8",
            )
            root_agent = AgentNode(
                id="agent-root-context",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Initial task",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                children=[
                    "agent-worker-waiting",
                    "agent-worker-pending",
                    "agent-worker-completed",
                ],
                conversation=[
                    {"role": "user", "content": "Initial task"},
                    {"role": "assistant", "content": '{"actions":[{"type": "list_agent_runs"}]}'},
                ],
            )
            waiting_worker = AgentNode(
                id="agent-worker-waiting",
                session_id=session_id,
                name="Waiting Worker",
                role=AgentRole.WORKER,
                instruction="Wait for parent",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "wait"}],
            )
            pending_worker = AgentNode(
                id="agent-worker-pending",
                session_id=session_id,
                name="Pending Worker",
                role=AgentRole.WORKER,
                instruction="Pending action",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.PENDING,
                conversation=[{"role": "user", "content": "pending"}],
            )
            completed_worker = AgentNode(
                id="agent-worker-completed",
                session_id=session_id,
                name="Completed Worker",
                role=AgentRole.WORKER,
                instruction="Done",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.COMPLETED,
                summary="already done",
                completion_status="completed",
                conversation=[{"role": "assistant", "content": "done"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Import context",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
                loop_index=2,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator.storage.upsert_agent(waiting_worker)
            orchestrator.storage.upsert_agent(pending_worker)
            orchestrator.storage.upsert_agent(completed_worker)
            orchestrator._sync_agent_messages(root_agent)
            orchestrator._sync_agent_messages(waiting_worker)
            orchestrator._sync_agent_messages(pending_worker)
            orchestrator._sync_agent_messages(completed_worker)

            queued_run = ToolRun(
                id="tool-run-queued-context",
                session_id=session_id,
                agent_id=root_agent.id,
                tool_name="list_agent_runs",
                arguments={"type": "list_agent_runs"},
                status=ToolRunStatus.QUEUED,
                blocking=False,
                created_at=now,
            )
            running_run = ToolRun(
                id="tool-run-running-context",
                session_id=session_id,
                agent_id=pending_worker.id,
                tool_name="list_agent_runs",
                arguments={"type": "list_agent_runs", "path": "."},
                status=ToolRunStatus.RUNNING,
                blocking=True,
                created_at=now,
                started_at=now,
            )
            orchestrator.storage.upsert_tool_run(queued_run)
            orchestrator.storage.upsert_tool_run(running_run)

            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={
                    root_agent.id: orchestrator._agent_state(root_agent),
                    waiting_worker.id: orchestrator._agent_state(waiting_worker),
                    pending_worker.id: orchestrator._agent_state(pending_worker),
                    completed_worker.id: orchestrator._agent_state(completed_worker),
                },
                workspaces=workspace_manager.serialize(),
                pending_agent_ids=[waiting_worker.id, pending_worker.id],
                pending_tool_run_ids=[queued_run.id, running_run.id],
                root_loop=2,
            )
            orchestrator.storage.save_checkpoint(session_id, utc_now(), checkpoint_state)

            loaded = orchestrator.load_session_context(session_id)
            self.assertNotEqual(loaded.id, session_id)
            self.assertEqual(loaded.status, SessionStatus.INTERRUPTED)
            self.assertEqual(
                str(loaded.config_snapshot.get("continued_from_session_id", "")),
                session_id,
            )
            stored_agents = {
                str(row.get("id")): str(row.get("status"))
                for row in orchestrator.storage.load_agents(loaded.id)
            }
            self.assertEqual(stored_agents[root_agent.id], AgentStatus.PAUSED.value)
            self.assertEqual(stored_agents[waiting_worker.id], AgentStatus.PAUSED.value)
            self.assertEqual(stored_agents[pending_worker.id], AgentStatus.PAUSED.value)
            self.assertEqual(stored_agents[completed_worker.id], AgentStatus.COMPLETED.value)

            cloned_cancelled_runs = orchestrator.list_tool_runs(
                loaded.id,
                status=ToolRunStatus.CANCELLED.value,
                limit=20,
            )
            self.assertEqual(len(cloned_cancelled_runs), 2)
            source_queued = orchestrator.storage.load_tool_run(queued_run.id)
            source_running = orchestrator.storage.load_tool_run(running_run.id)
            assert source_queued is not None
            assert source_running is not None
            self.assertEqual(str(source_queued.get("status")), ToolRunStatus.QUEUED.value)
            self.assertEqual(str(source_running.get("status")), ToolRunStatus.RUNNING.value)

            latest_checkpoint = orchestrator.storage.latest_checkpoint(loaded.id)
            self.assertIsNotNone(latest_checkpoint)
            assert latest_checkpoint is not None
            self.assertEqual(
                latest_checkpoint["state"]["agents"][root_agent.id]["status"],
                AgentStatus.PAUSED.value,
            )
            workspace_payload = latest_checkpoint["state"]["workspaces"][root_workspace.id]
            cloned_workspace_path = Path(str(workspace_payload["path"]))
            self.assertTrue(cloned_workspace_path.is_relative_to(orchestrator.paths.session_dir(loaded.id)))
            self.assertTrue((cloned_workspace_path / "sandbox-marker.txt").exists())
            messages = orchestrator.list_session_messages(loaded.id, agent_id=root_agent.id, tail=20)
            self.assertTrue(messages["messages"])
            self.assertTrue(
                all(str(row.get("session_id", "")) == loaded.id for row in messages["messages"])
            )

    async def test_load_session_context_recovers_legacy_workspace_paths_for_sandbox(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "opencompany-app"
            project_dir = root / "target-project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_project(app_dir)
            (project_dir / "README.md").write_text("target\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            session_id = "session-legacy-workspaces"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            marker_path = root_workspace.path / "sandbox-marker.txt"
            marker_path.write_text("legacy sandbox state\n", encoding="utf-8")
            root_agent = AgentNode(
                id="agent-root-legacy",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Continue legacy sandbox",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "legacy"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Legacy import",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
                loop_index=1,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator._sync_agent_messages(root_agent)

            legacy_workspaces = workspace_manager.serialize()
            legacy_workspaces[root_workspace.id]["path"] = ""
            legacy_workspaces[root_workspace.id]["base_snapshot_path"] = ""
            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={root_agent.id: orchestrator._agent_state(root_agent)},
                workspaces=legacy_workspaces,
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=1,
            )
            orchestrator.storage.save_checkpoint(session_id, now, checkpoint_state)

            loaded = orchestrator.load_session_context(session_id)
            (
                imported_session,
                imported_agents,
                imported_workspace_manager,
                _pending_agent_ids,
                _root_loop,
                _checkpoint_seq,
            ) = orchestrator._import_session_context(loaded.id, source="resume")
            imported_root = imported_agents[imported_session.root_agent_id]

            captured: dict[str, Path] = {}

            async def fake_run_command(self, request, on_event=None):
                del self, on_event
                captured["cwd"] = request.cwd
                marker = request.cwd / "sandbox-marker.txt"
                if not marker.exists():
                    return ShellCommandResult(
                        exit_code=1,
                        stdout="",
                        stderr="sandbox marker missing",
                        command=request.command,
                    )
                return ShellCommandResult(
                    exit_code=0,
                    stdout=marker.read_text(encoding="utf-8"),
                    stderr="",
                    command=request.command,
                )

            with mock.patch(
                "opencompany.orchestrator.AnthropicSandboxBackend.run_command",
                new=fake_run_command,
            ):
                result = await orchestrator._execute_shell_action(
                    imported_root,
                    {"type": "shell", "command": "cat sandbox-marker.txt"},
                    imported_workspace_manager,
                )

            self.assertEqual(result["exit_code"], 0)
            self.assertIn("legacy sandbox state", result["stdout"])
            resolved_cwd = captured["cwd"].resolve()
            self.assertTrue(resolved_cwd.is_relative_to(orchestrator.paths.session_dir(loaded.id)))

    async def test_load_session_context_preserves_direct_mode_and_live_project_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "opencompany-app"
            project_dir = root / "target-project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_project(app_dir)
            (project_dir / "README.md").write_text("target\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            session_id = "session-direct-import"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir, mode="direct")
            root_agent = AgentNode(
                id="agent-root-direct",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Continue direct workspace",
                workspace_id=root_workspace.id,
                status=AgentStatus.COMPLETED,
                conversation=[{"role": "user", "content": "direct"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Direct import",
                locale="en",
                root_agent_id=root_agent.id,
                workspace_mode=WorkspaceMode.DIRECT,
                status=SessionStatus.COMPLETED,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator._sync_agent_messages(root_agent)
            serialized_workspaces = workspace_manager.serialize()
            serialized_workspaces[root_workspace.id]["base_snapshot_path"] = str(
                orchestrator.paths.session_dir(session_id) / "snapshots" / "root_base"
            )
            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={root_agent.id: orchestrator._agent_state(root_agent)},
                workspaces=serialized_workspaces,
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=0,
            )
            orchestrator.storage.save_checkpoint(session_id, now, checkpoint_state)

            loaded = orchestrator.load_session_context(session_id)
            self.assertEqual(loaded.workspace_mode, WorkspaceMode.DIRECT)
            self.assertEqual(loaded.project_dir.resolve(), project_dir.resolve())

            (
                imported_session,
                _imported_agents,
                imported_workspace_manager,
                _pending_agent_ids,
                _root_loop,
                _checkpoint_seq,
            ) = orchestrator._import_session_context(loaded.id, source="resume")
            self.assertEqual(imported_session.workspace_mode, WorkspaceMode.DIRECT)
            self.assertEqual(
                imported_workspace_manager.root_workspace().path.resolve(),
                project_dir.resolve(),
            )
            self.assertEqual(
                imported_workspace_manager.root_workspace().base_snapshot_path.resolve(),
                project_dir.resolve(),
            )

    async def test_loaded_session_remains_usable_after_source_session_deleted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "opencompany-app"
            project_dir = root / "target-project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_project(app_dir)
            (project_dir / "README.md").write_text("target\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            source_session_id = "session-source-independence"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(source_session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            marker_path = root_workspace.path / "sandbox-marker.txt"
            marker_path.write_text("independent sandbox state\n", encoding="utf-8")

            root_agent = AgentNode(
                id="agent-root-independence",
                session_id=source_session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Independence check",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                children=["agent-worker-independence"],
                conversation=[{"role": "user", "content": "source session task"}],
            )
            child_agent = AgentNode(
                id="agent-worker-independence",
                session_id=source_session_id,
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Worker task",
                workspace_id=root_workspace.id,
                parent_agent_id=root_agent.id,
                status=AgentStatus.COMPLETED,
                summary="done",
                completion_status="completed",
                conversation=[{"role": "assistant", "content": "worker done"}],
            )
            source_session = RunSession(
                id=source_session_id,
                project_dir=project_dir,
                task="Import and detach",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
                loop_index=1,
            )
            orchestrator.storage.upsert_session(source_session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator.storage.upsert_agent(child_agent)
            orchestrator._sync_agent_messages(root_agent)
            orchestrator._sync_agent_messages(child_agent)
            orchestrator.storage.upsert_tool_run(
                ToolRun(
                    id="tool-run-source-completed",
                    session_id=source_session_id,
                    agent_id=root_agent.id,
                    tool_name="list_agent_runs",
                    arguments={"type": "list_agent_runs"},
                    status=ToolRunStatus.COMPLETED,
                    blocking=True,
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                    result={"agents": [root_agent.id, child_agent.id]},
                )
            )
            orchestrator.storage.append_event(
                EventRecord(
                    timestamp=now,
                    session_id=source_session_id,
                    agent_id=root_agent.id,
                    parent_agent_id=None,
                    event_type="agent_prompt",
                    phase="llm",
                    payload={"step_count": 1, "agent_name": root_agent.name},
                    workspace_id=root_workspace.id,
                    checkpoint_seq=1,
                )
            )
            source_checkpoint_state = CheckpointState(
                session=orchestrator._session_state(source_session),
                agents={
                    root_agent.id: orchestrator._agent_state(root_agent),
                    child_agent.id: orchestrator._agent_state(child_agent),
                },
                workspaces=workspace_manager.serialize(),
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=1,
            )
            orchestrator.storage.save_checkpoint(source_session_id, now, source_checkpoint_state)

            loaded = orchestrator.load_session_context(source_session_id)
            loaded_session_id = loaded.id
            self.assertNotEqual(loaded_session_id, source_session_id)
            loaded_session_dir = orchestrator.paths.session_dir(loaded_session_id)
            self.assertTrue((loaded_session_dir / f"{root_agent.id}_messages.jsonl").exists())
            self.assertTrue((loaded_session_dir / f"{child_agent.id}_messages.jsonl").exists())
            self.assertTrue((loaded_session_dir / "snapshots" / "root" / "sandbox-marker.txt").exists())

            source_messages = orchestrator.list_session_messages(
                source_session_id,
                agent_id=root_agent.id,
                tail=100,
            )["messages"]
            loaded_messages_before = orchestrator.list_session_messages(
                loaded_session_id,
                agent_id=root_agent.id,
                tail=200,
            )["messages"]
            source_child_messages = orchestrator.list_session_messages(
                source_session_id,
                agent_id=child_agent.id,
                tail=100,
            )["messages"]
            loaded_child_messages_before = orchestrator.list_session_messages(
                loaded_session_id,
                agent_id=child_agent.id,
                tail=200,
            )["messages"]
            source_contents = {
                str((item.get("message") or {}).get("content", "")).strip()
                for item in source_messages
                if str((item.get("message") or {}).get("content", "")).strip()
            }
            loaded_contents_before = {
                str((item.get("message") or {}).get("content", "")).strip()
                for item in loaded_messages_before
                if str((item.get("message") or {}).get("content", "")).strip()
            }
            source_child_contents = {
                str((item.get("message") or {}).get("content", "")).strip()
                for item in source_child_messages
                if str((item.get("message") or {}).get("content", "")).strip()
            }
            loaded_child_contents_before = {
                str((item.get("message") or {}).get("content", "")).strip()
                for item in loaded_child_messages_before
                if str((item.get("message") or {}).get("content", "")).strip()
            }
            self.assertTrue(source_contents.issubset(loaded_contents_before))
            self.assertTrue(source_child_contents.issubset(loaded_child_contents_before))
            self.assertTrue(
                all(
                    str(item.get("session_id", "")).strip() == loaded_session_id
                    for item in [*loaded_messages_before, *loaded_child_messages_before]
                )
            )

            source_events = orchestrator.load_session_events(source_session_id)
            loaded_events_before = orchestrator.load_session_events(loaded_session_id)
            source_event_types = {str(item.get("event_type", "")) for item in source_events}
            loaded_event_types_before = {str(item.get("event_type", "")) for item in loaded_events_before}
            self.assertTrue(source_event_types.issubset(loaded_event_types_before))
            self.assertTrue(
                any(
                    str(item.get("event_type", "")) == "agent_prompt"
                    and str((item.get("payload") or {}).get("agent_name", "")) == root_agent.name
                    and int((item.get("payload") or {}).get("step_count", 0) or 0) == 1
                    for item in loaded_events_before
                )
            )
            loaded_tool_runs_before = orchestrator.list_tool_runs(loaded_session_id, limit=50)
            self.assertTrue(
                any(
                    str(run.get("status", "")) == ToolRunStatus.COMPLETED.value
                    and str(run.get("tool_name", "")) == "list_agent_runs"
                    and str(run.get("agent_id", "")) == root_agent.id
                    for run in loaded_tool_runs_before
                )
            )

            source_session_dir = orchestrator.paths.session_dir(source_session_id)
            if source_session_dir.exists():
                shutil.rmtree(source_session_dir)
            for table in ("events", "checkpoints", "pending_actions", "tool_runs", "agents"):
                orchestrator.storage.connection.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",
                    (source_session_id,),
                )
            orchestrator.storage.connection.execute(
                "DELETE FROM sessions WHERE id = ?",
                (source_session_id,),
            )
            orchestrator.storage.connection.commit()

            self.assertFalse(source_session_dir.exists())
            self.assertIsNone(orchestrator.storage.load_session(source_session_id))

            loaded_events_after = orchestrator.load_session_events(loaded_session_id)
            loaded_messages_after = orchestrator.list_session_messages(
                loaded_session_id,
                agent_id=root_agent.id,
                tail=200,
            )["messages"]
            loaded_child_messages_after = orchestrator.list_session_messages(
                loaded_session_id,
                agent_id=child_agent.id,
                tail=200,
            )["messages"]
            loaded_tool_runs_after = orchestrator.list_tool_runs(loaded_session_id, limit=50)
            self.assertGreaterEqual(len(loaded_events_after), len(loaded_events_before))
            self.assertEqual(len(loaded_messages_after), len(loaded_messages_before))
            self.assertEqual(len(loaded_child_messages_after), len(loaded_child_messages_before))
            self.assertGreaterEqual(len(loaded_tool_runs_after), len(loaded_tool_runs_before))
            self.assertTrue((loaded_session_dir / "snapshots" / "root" / "sandbox-marker.txt").exists())

            (
                imported_session,
                imported_agents,
                imported_workspace_manager,
                _pending_agent_ids,
                _root_loop,
                _checkpoint_seq,
            ) = orchestrator._import_session_context(loaded_session_id, source="resume")
            imported_root = imported_agents[imported_session.root_agent_id]
            imported_child = imported_agents[child_agent.id]
            self.assertEqual(imported_child.parent_agent_id, imported_root.id)
            self.assertIn(imported_child.id, imported_root.children)
            self.assertEqual(imported_root.status, AgentStatus.PAUSED)
            self.assertEqual(imported_child.status, AgentStatus.COMPLETED)

            async def fake_run_command(self, request, on_event=None):
                del self, on_event
                marker = request.cwd / "sandbox-marker.txt"
                if not marker.exists():
                    return ShellCommandResult(
                        exit_code=1,
                        stdout="",
                        stderr="sandbox marker missing",
                        command=request.command,
                    )
                return ShellCommandResult(
                    exit_code=0,
                    stdout=marker.read_text(encoding="utf-8"),
                    stderr="",
                    command=request.command,
                )

            with mock.patch(
                "opencompany.orchestrator.AnthropicSandboxBackend.run_command",
                new=fake_run_command,
            ):
                shell_result = await orchestrator._execute_shell_action(
                    imported_root,
                    {"type": "shell", "command": "cat sandbox-marker.txt"},
                    imported_workspace_manager,
                )

            self.assertEqual(shell_result["exit_code"], 0)
            self.assertIn("independent sandbox state", shell_result["stdout"])
