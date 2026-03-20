from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from opencompany.llm.openrouter import ChatResult
from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    CheckpointState,
    EventRecord,
    RunSession,
    SessionStatus,
    SteerRun,
    SteerRunStatus,
)
from opencompany.orchestrator import Orchestrator
from opencompany.utils import utc_now
from opencompany.workspace import WorkspaceManager
from test_orchestrator import build_test_project


class RecordingLLMClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def stream_chat(self, **kwargs) -> ChatResult:
        self.calls.append(kwargs)
        on_token = kwargs.get("on_token")
        if on_token and self.response:
            maybe = on_token(self.response)
            if hasattr(maybe, "__await__"):
                await maybe
        return ChatResult(content=self.response, raw_events=[])


class OrchestratorSteerRunTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _signed_user_steer(content: str, *, locale: str = "en") -> str:
        intro = "*你收到一条消息：*" if locale == "zh" else "*You received a message:*"
        source = "用户" if locale == "zh" else "user"
        prefix = "来自于" if locale == "zh" else "from"
        return f"{intro}\n\n{content}\n\n--- {prefix} {source}"

    @staticmethod
    def _signed_agent_steer(
        content: str,
        *,
        agent_name: str,
        agent_id: str,
        locale: str = "en",
    ) -> str:
        intro = "*你收到一条消息：*" if locale == "zh" else "*You received a message:*"
        prefix = "来自于" if locale == "zh" else "from"
        return f"{intro}\n\n{content}\n\n--- {prefix} {agent_name} ({agent_id})"

    def _bootstrap_session(
        self,
        orchestrator: Orchestrator,
        *,
        project_dir: Path,
        session_id: str,
    ) -> tuple[RunSession, AgentNode, WorkspaceManager]:
        session_dir = orchestrator.paths.session_dir(session_id, create=True)
        workspace_manager = WorkspaceManager(session_dir)
        root_workspace = workspace_manager.create_root_workspace(project_dir)
        now = utc_now()
        root = AgentNode(
            id="agent-root",
            session_id=session_id,
            name="Root Coordinator",
            role=AgentRole.ROOT,
            instruction="Inspect the project",
            workspace_id=root_workspace.id,
            status=AgentStatus.RUNNING,
            conversation=[{"role": "user", "content": "Inspect the project"}],
        )
        session = RunSession(
            id=session_id,
            project_dir=project_dir,
            task="steer test",
            locale=orchestrator.locale,
            root_agent_id=root.id,
            status=SessionStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
        orchestrator.storage.upsert_session(session)
        orchestrator.storage.upsert_agent(root)
        orchestrator._sync_agent_messages(root)
        return session, root, workspace_manager

    async def test_submit_steer_run_persists_waiting_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-submit",
            )

            submitted = orchestrator.submit_steer_run(
                session_id=session.id,
                agent_id=root.id,
                content="Focus on test coverage",
                source="tui",
            )
            self.assertEqual(str(submitted.get("status")), SteerRunStatus.WAITING.value)
            self.assertEqual(str(submitted.get("source")), "tui")

            stored = orchestrator.storage.load_steer_run(str(submitted.get("id", "")))
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(str(stored.get("status")), SteerRunStatus.WAITING.value)
            self.assertEqual(
                str(stored.get("content")),
                self._signed_user_steer("Focus on test coverage"),
            )
            self.assertEqual(str(stored.get("source_agent_id", "")), "user")
            self.assertEqual(str(stored.get("source_agent_name", "")), "user")

            metrics = orchestrator.steer_run_metrics(session.id)
            self.assertEqual(int(metrics.get("total_runs", 0)), 1)
            counts = metrics.get("status_counts")
            assert isinstance(counts, dict)
            self.assertEqual(int(counts.get("waiting", 0)), 1)

            events = orchestrator.storage.load_events(session.id)
            self.assertTrue(
                any(str(event.get("event_type", "")) == "steer_run_submitted" for event in events)
            )

    async def test_submit_steer_run_reactivates_terminal_worker_and_injects_message(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-reactivate-terminal-worker",
            )
            worker = AgentNode(
                id="agent-worker-finished",
                session_id=session.id,
                name="Worker 1",
                role=AgentRole.WORKER,
                instruction="Do worker task",
                workspace_id=root.workspace_id,
                parent_agent_id=root.id,
                status=AgentStatus.COMPLETED,
                completion_status="completed",
                conversation=[{"role": "user", "content": "Do worker task"}],
            )
            root.children = [worker.id]
            orchestrator.storage.upsert_agent(root)
            orchestrator.storage.upsert_agent(worker)
            orchestrator._sync_agent_messages(worker)
            agents = {root.id: root, worker.id: worker}
            orchestrator._live_session_contexts[session.id] = (session, agents, workspace_manager)

            scheduled_agent_batches: list[list[str]] = []

            def _capture_worker_start(
                *,
                session: RunSession,
                agents: dict[str, AgentNode],
                workspace_manager: WorkspaceManager,
                root_loop: int,
                agent_ids: list[str] | None = None,
            ) -> None:
                del session, agents, workspace_manager, root_loop
                scheduled_agent_batches.append(list(agent_ids or []))

            orchestrator._ensure_worker_tasks_started = _capture_worker_start  # type: ignore[method-assign]

            steer_content = "reopen this worker via steer"
            submitted = orchestrator.submit_steer_run(
                session_id=session.id,
                agent_id=worker.id,
                content=steer_content,
                source="webui",
            )

            self.assertEqual(str(submitted.get("status")), SteerRunStatus.WAITING.value)
            self.assertEqual(worker.status, AgentStatus.RUNNING)
            self.assertIsNone(worker.completion_status)
            self.assertEqual(scheduled_agent_batches, [[worker.id]])

            worker_row = orchestrator._agent_row_for_session(session.id, worker.id)
            self.assertIsNotNone(worker_row)
            assert worker_row is not None
            self.assertEqual(str(worker_row.get("status", "")), AgentStatus.RUNNING.value)
            self.assertIsNone(worker_row.get("completion_status"))

            steer_id = str(submitted.get("id", ""))
            steer_row_before = orchestrator.storage.load_steer_run(steer_id)
            self.assertIsNotNone(steer_row_before)
            assert steer_row_before is not None
            self.assertEqual(str(steer_row_before.get("status", "")), SteerRunStatus.WAITING.value)
            expected_steer_content = (
                f"{self._signed_user_steer(steer_content)}\n\n"
                "After completing this instruction, call the finish tool again to end the agent."
            )
            self.assertEqual(str(steer_row_before.get("content", "")), expected_steer_content)

            orchestrator._consume_waiting_steers_for_agent(worker)

            steer_row_after = orchestrator.storage.load_steer_run(steer_id)
            self.assertIsNotNone(steer_row_after)
            assert steer_row_after is not None
            self.assertEqual(str(steer_row_after.get("status", "")), SteerRunStatus.COMPLETED.value)
            self.assertEqual(
                str(worker.conversation[-1].get("content", "")),
                expected_steer_content,
            )

    async def test_submit_steer_run_localizes_intro_and_signature_for_chinese(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="zh", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-submit-zh",
            )

            submitted = orchestrator.submit_steer_run(
                session_id=session.id,
                agent_id=root.id,
                content="请优先处理测试覆盖",
                source="webui",
            )
            stored = orchestrator.storage.load_steer_run(str(submitted.get("id", "")))
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(
                str(stored.get("content", "")),
                self._signed_user_steer("请优先处理测试覆盖", locale="zh"),
            )
            self.assertEqual(str(stored.get("source_agent_name", "")), "用户")

    async def test_steer_agent_tool_persists_agent_source_and_signature(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-agent-tool",
            )
            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-worker")
            worker = AgentNode(
                id="agent-worker",
                session_id=session.id,
                name="Worker One",
                role=AgentRole.WORKER,
                instruction="Do worker task",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "Do worker task"}],
            )
            root.children = [worker.id]
            orchestrator.storage.upsert_agent(root)
            orchestrator.storage.upsert_agent(worker)
            agents = {root.id: root, worker.id: worker}

            result = await orchestrator._submit_tool_run(
                session=session,
                agent=root,
                action={
                    "type": "steer_agent",
                    "agent_id": worker.id,
                    "content": "Refocus on test coverage",
                },
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            agent_result = result.get("agent_result")
            self.assertIsInstance(agent_result, dict)
            assert isinstance(agent_result, dict)
            self.assertTrue(bool(agent_result.get("steer_agent_status")))
            steer_run_id = str(agent_result.get("steer_run_id", ""))
            self.assertTrue(steer_run_id)
            row = orchestrator.storage.load_steer_run(steer_run_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(str(row.get("source_agent_id", "")), root.id)
            self.assertEqual(str(row.get("source_agent_name", "")), root.name)
            self.assertEqual(
                str(row.get("content", "")),
                self._signed_agent_steer(
                    "Refocus on test coverage",
                    agent_name=root.name,
                    agent_id=root.id,
                ),
            )

    async def test_steer_agent_tool_rejects_self_and_out_of_scope_targets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.tools.steer_agent_scope = "descendants"
            session, root, workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-agent-scope",
            )
            child_workspace = workspace_manager.fork_workspace(root.workspace_id, "agent-worker")
            worker = AgentNode(
                id="agent-worker",
                session_id=session.id,
                name="Worker One",
                role=AgentRole.WORKER,
                instruction="Do worker task",
                workspace_id=child_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "Do worker task"}],
            )
            root.children = [worker.id]
            orchestrator.storage.upsert_agent(root)
            orchestrator.storage.upsert_agent(worker)
            agents = {root.id: root, worker.id: worker}

            self_result = await orchestrator._submit_tool_run(
                session=session,
                agent=worker,
                action={
                    "type": "steer_agent",
                    "agent_id": worker.id,
                    "content": "self steer is invalid",
                },
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            self_payload = self_result.get("agent_result")
            self.assertIsInstance(self_payload, dict)
            assert isinstance(self_payload, dict)
            self.assertFalse(bool(self_payload.get("steer_agent_status")))
            self.assertIn("cannot target the current agent itself", str(self_payload.get("error", "")))

            out_of_scope = await orchestrator._submit_tool_run(
                session=session,
                agent=worker,
                action={
                    "type": "steer_agent",
                    "agent_id": root.id,
                    "content": "steer parent",
                },
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=0,
                tracked_pending_ids=[],
            )
            scope_payload = out_of_scope.get("agent_result")
            self.assertIsInstance(scope_payload, dict)
            assert isinstance(scope_payload, dict)
            self.assertFalse(bool(scope_payload.get("steer_agent_status")))
            self.assertEqual(str(scope_payload.get("configured_scope", "")), "descendants")
            self.assertEqual(orchestrator.steer_run_metrics(session.id).get("total_runs"), 0)

    async def test_waiting_steers_are_consumed_in_order_before_llm_request(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-consume",
            )
            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id="steerrun-1",
                    session_id=session.id,
                    agent_id=root.id,
                    content="First steer message",
                    source="webui",
                    status=SteerRunStatus.WAITING,
                    created_at="2026-03-13T10:00:00Z",
                )
            )
            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id="steerrun-2",
                    session_id=session.id,
                    agent_id=root.id,
                    content="Second steer message",
                    source="webui",
                    status=SteerRunStatus.WAITING,
                    created_at="2026-03-13T10:00:01Z",
                )
            )

            llm = RecordingLLMClient(
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "done",
                            }
                        ]
                    }
                )
            )
            orchestrator.llm_client = llm

            actions = await orchestrator._ask_agent(root)
            self.assertTrue(actions)
            self.assertEqual(actions[0].get("type"), "finish")

            self.assertEqual(len(llm.calls), 1)
            request_messages = llm.calls[0].get("messages")
            self.assertIsInstance(request_messages, list)
            assert isinstance(request_messages, list)
            user_contents = [
                str(message.get("content", ""))
                for message in request_messages
                if isinstance(message, dict) and str(message.get("role", "")) == "user"
            ]
            self.assertGreaterEqual(len(user_contents), 3)
            self.assertEqual(
                user_contents[-2:],
                ["First steer message", "Second steer message"],
            )

            run_1 = orchestrator.storage.load_steer_run("steerrun-1")
            run_2 = orchestrator.storage.load_steer_run("steerrun-2")
            self.assertIsNotNone(run_1)
            self.assertIsNotNone(run_2)
            assert run_1 is not None
            assert run_2 is not None
            self.assertEqual(str(run_1.get("status")), SteerRunStatus.COMPLETED.value)
            self.assertEqual(str(run_2.get("status")), SteerRunStatus.COMPLETED.value)
            self.assertEqual(int(run_1.get("delivered_step", 0) or 0), 1)
            self.assertEqual(int(run_2.get("delivered_step", 0) or 0), 1)

            page = orchestrator.list_session_messages(
                session.id,
                agent_id=root.id,
                tail=20,
            )
            records = page.get("messages")
            self.assertIsInstance(records, list)
            assert isinstance(records, list)
            steer_records = [record for record in records if record.get("source") == "steer"]
            self.assertEqual(
                [str(record.get("steer_run_id", "")) for record in steer_records],
                ["steerrun-1", "steerrun-2"],
            )

    async def test_soft_limit_message_is_before_steer_and_steer_is_last_llm_user_message(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-order",
            )
            root.step_count = int(orchestrator.config.runtime.limits.max_root_steps)
            max_step_message = orchestrator._runtime_message("root_loop_force_finalize")
            steer_content = "Final steer should be the last user message"
            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id="steerrun-order",
                    session_id=session.id,
                    agent_id=root.id,
                    content=steer_content,
                    source="webui",
                    status=SteerRunStatus.WAITING,
                    created_at="2026-03-13T12:00:00Z",
                )
            )
            orchestrator._maybe_append_root_soft_limit_reminder(
                session=session,
                root_agent=root,
                root_loop=4,
            )
            llm = RecordingLLMClient(
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "done",
                            }
                        ]
                    }
                )
            )
            orchestrator.llm_client = llm

            await orchestrator._ask_agent(root)
            self.assertEqual(len(llm.calls), 1)
            request_messages = llm.calls[0].get("messages")
            self.assertIsInstance(request_messages, list)
            assert isinstance(request_messages, list)
            user_contents = [
                str(message.get("content", ""))
                for message in request_messages
                if isinstance(message, dict) and str(message.get("role", "")) == "user"
            ]
            self.assertGreaterEqual(len(user_contents), 3)
            self.assertEqual(user_contents[-1], steer_content)
            self.assertIn(max_step_message, user_contents)
            self.assertLess(user_contents.index(max_step_message), user_contents.index(steer_content))

    async def test_cancel_steer_run_waiting_and_completed_semantics(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-cancel",
            )

            waiting = orchestrator.submit_steer_run(
                session_id=session.id,
                agent_id=root.id,
                content="cancel me",
                source="webui",
            )
            waiting_id = str(waiting.get("id", ""))
            cancelled = orchestrator.cancel_steer_run(
                session_id=session.id,
                steer_run_id=waiting_id,
            )
            self.assertEqual(str(cancelled.get("final_status")), SteerRunStatus.CANCELLED.value)
            self.assertTrue(bool(cancelled.get("cancelled")))

            already_cancelled = orchestrator.cancel_steer_run(
                session_id=session.id,
                steer_run_id=waiting_id,
            )
            self.assertEqual(
                str(already_cancelled.get("final_status")),
                SteerRunStatus.CANCELLED.value,
            )
            self.assertFalse(bool(already_cancelled.get("cancelled")))

            completed = orchestrator.submit_steer_run(
                session_id=session.id,
                agent_id=root.id,
                content="already completed",
                source="webui",
            )
            completed_id = str(completed.get("id", ""))
            orchestrator.storage.complete_waiting_steer_run(
                session_id=session.id,
                steer_run_id=completed_id,
                completed_at=utc_now(),
                delivered_step=2,
            )
            blocked = orchestrator.cancel_steer_run(
                session_id=session.id,
                steer_run_id=completed_id,
            )
            self.assertEqual(
                str(blocked.get("final_status")),
                SteerRunStatus.COMPLETED.value,
            )
            self.assertFalse(bool(blocked.get("cancelled")))

            completed_row = orchestrator.storage.load_steer_run(completed_id)
            self.assertIsNotNone(completed_row)
            assert completed_row is not None
            self.assertEqual(
                str(completed_row.get("status", "")),
                SteerRunStatus.COMPLETED.value,
            )

    async def test_cancel_during_consume_stays_completed_and_message_is_injected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-cancel-during-consume",
            )
            steer_id = "steerrun-cancel-during-consume"
            steer_content = "cancel during consume race"
            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id=steer_id,
                    session_id=session.id,
                    agent_id=root.id,
                    content=steer_content,
                    source="webui",
                    status=SteerRunStatus.WAITING,
                    created_at="2026-03-13T13:00:00Z",
                )
            )

            llm = RecordingLLMClient(
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "done",
                            }
                        ]
                    }
                )
            )
            orchestrator.llm_client = llm

            cancel_result: dict[str, Any] = {}
            original_complete_waiting = orchestrator.storage.complete_waiting_steer_run

            def wrapped_complete_waiting_steer_run(
                *,
                session_id: str,
                steer_run_id: str,
                completed_at: str,
                delivered_step: int | None,
            ) -> dict[str, Any] | None:
                transitioned = original_complete_waiting(
                    session_id=session_id,
                    steer_run_id=steer_run_id,
                    completed_at=completed_at,
                    delivered_step=delivered_step,
                )
                if transitioned is not None and not cancel_result:
                    cancel_result.update(
                        orchestrator.cancel_steer_run(
                            session_id=session_id,
                            steer_run_id=steer_run_id,
                        )
                    )
                return transitioned

            orchestrator.storage.complete_waiting_steer_run = wrapped_complete_waiting_steer_run  # type: ignore[method-assign]

            await orchestrator._ask_agent(root)
            self.assertEqual(len(llm.calls), 1)
            request_messages = llm.calls[0].get("messages")
            self.assertIsInstance(request_messages, list)
            assert isinstance(request_messages, list)
            user_contents = [
                str(message.get("content", ""))
                for message in request_messages
                if isinstance(message, dict) and str(message.get("role", "")) == "user"
            ]
            self.assertEqual(user_contents[-1], steer_content)

            self.assertTrue(cancel_result)
            self.assertEqual(
                str(cancel_result.get("final_status", "")),
                SteerRunStatus.COMPLETED.value,
            )
            self.assertFalse(bool(cancel_result.get("cancelled")))

            row = orchestrator.storage.load_steer_run(steer_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(str(row.get("status", "")), SteerRunStatus.COMPLETED.value)

    async def test_cancelled_steer_will_not_be_injected_into_messages(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-cancel-before-consume",
            )
            steer_id = "steerrun-cancel-before-consume"
            steer_content = "this cancelled steer must not be injected"
            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id=steer_id,
                    session_id=session.id,
                    agent_id=root.id,
                    content=steer_content,
                    source="webui",
                    status=SteerRunStatus.WAITING,
                    created_at="2026-03-13T14:00:00Z",
                )
            )
            cancel_result = orchestrator.cancel_steer_run(
                session_id=session.id,
                steer_run_id=steer_id,
            )
            self.assertEqual(
                str(cancel_result.get("final_status", "")),
                SteerRunStatus.CANCELLED.value,
            )
            self.assertTrue(bool(cancel_result.get("cancelled")))

            llm = RecordingLLMClient(
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "done",
                            }
                        ]
                    }
                )
            )
            orchestrator.llm_client = llm
            await orchestrator._ask_agent(root)

            self.assertEqual(len(llm.calls), 1)
            request_messages = llm.calls[0].get("messages")
            self.assertIsInstance(request_messages, list)
            assert isinstance(request_messages, list)
            user_contents = [
                str(message.get("content", ""))
                for message in request_messages
                if isinstance(message, dict) and str(message.get("role", "")) == "user"
            ]
            self.assertNotIn(steer_content, user_contents)

            row = orchestrator.storage.load_steer_run(steer_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(str(row.get("status", "")), SteerRunStatus.CANCELLED.value)

            page = orchestrator.list_session_messages(
                session.id,
                agent_id=root.id,
                tail=20,
            )
            records = page.get("messages")
            self.assertIsInstance(records, list)
            assert isinstance(records, list)
            self.assertFalse(
                any(
                    str(record.get("steer_run_id", "")) == steer_id
                    for record in records
                    if isinstance(record, dict)
                )
            )

    async def test_storage_cas_guards_prevent_status_overwrite(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session, root, _workspace_manager = self._bootstrap_session(
                orchestrator,
                project_dir=project_dir,
                session_id="session-steer-cas",
            )

            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id="steerrun-cas-complete-first",
                    session_id=session.id,
                    agent_id=root.id,
                    content="cas complete first",
                    source="test",
                    status=SteerRunStatus.WAITING,
                    created_at=utc_now(),
                )
            )
            completed = orchestrator.storage.complete_waiting_steer_run(
                session_id=session.id,
                steer_run_id="steerrun-cas-complete-first",
                completed_at=utc_now(),
                delivered_step=1,
            )
            self.assertIsNotNone(completed)
            cancelled_after_complete = orchestrator.storage.cancel_waiting_steer_run(
                session_id=session.id,
                steer_run_id="steerrun-cas-complete-first",
                cancelled_at=utc_now(),
            )
            self.assertIsNone(cancelled_after_complete)
            final_completed = orchestrator.storage.load_steer_run("steerrun-cas-complete-first")
            self.assertIsNotNone(final_completed)
            assert final_completed is not None
            self.assertEqual(
                str(final_completed.get("status", "")),
                SteerRunStatus.COMPLETED.value,
            )

            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id="steerrun-cas-cancel-first",
                    session_id=session.id,
                    agent_id=root.id,
                    content="cas cancel first",
                    source="test",
                    status=SteerRunStatus.WAITING,
                    created_at=utc_now(),
                )
            )
            cancelled = orchestrator.storage.cancel_waiting_steer_run(
                session_id=session.id,
                steer_run_id="steerrun-cas-cancel-first",
                cancelled_at=utc_now(),
            )
            self.assertIsNotNone(cancelled)
            completed_after_cancel = orchestrator.storage.complete_waiting_steer_run(
                session_id=session.id,
                steer_run_id="steerrun-cas-cancel-first",
                completed_at=utc_now(),
                delivered_step=2,
            )
            self.assertIsNone(completed_after_cancel)
            final_cancelled = orchestrator.storage.load_steer_run("steerrun-cas-cancel-first")
            self.assertIsNotNone(final_cancelled)
            assert final_cancelled is not None
            self.assertEqual(
                str(final_cancelled.get("status", "")),
                SteerRunStatus.CANCELLED.value,
            )

    async def test_clone_session_clones_steer_runs_and_rewrites_event_ids(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "opencompany-app"
            project_dir = root / "target-project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_project(app_dir)
            (project_dir / "README.md").write_text("target\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            session_id = "session-steer-clone"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)

            root_agent = AgentNode(
                id="agent-root-steer-clone",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Import context",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                conversation=[{"role": "user", "content": "Import context"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Clone steer runs",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.INTERRUPTED,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            orchestrator._sync_agent_messages(root_agent)

            source_steer_id = "steerrun-source-clone"
            orchestrator.storage.upsert_steer_run(
                SteerRun(
                    id=source_steer_id,
                    session_id=session_id,
                    agent_id=root_agent.id,
                    content="carry this steer into cloned context",
                    source="webui",
                    status=SteerRunStatus.WAITING,
                    created_at=now,
                )
            )
            orchestrator.storage.append_event(
                EventRecord(
                    timestamp=now,
                    session_id=session_id,
                    agent_id=root_agent.id,
                    parent_agent_id=None,
                    event_type="steer_run_submitted",
                    phase="steer",
                    payload={
                        "steer_run_id": source_steer_id,
                        "status": SteerRunStatus.WAITING.value,
                        "source": "webui",
                    },
                    workspace_id=root_workspace.id,
                    checkpoint_seq=1,
                )
            )

            checkpoint_state = CheckpointState(
                session=orchestrator._session_state(session),
                agents={root_agent.id: orchestrator._agent_state(root_agent)},
                workspaces=workspace_manager.serialize(),
                pending_agent_ids=[],
                pending_tool_run_ids=[],
                root_loop=1,
            )
            orchestrator.storage.save_checkpoint(session_id, now, checkpoint_state)

            loaded = orchestrator.clone_session(session_id)
            cloned_runs_page = orchestrator.list_steer_runs_page(loaded.id, limit=20)
            cloned_runs = cloned_runs_page.get("steer_runs")
            self.assertIsInstance(cloned_runs, list)
            assert isinstance(cloned_runs, list)
            self.assertEqual(len(cloned_runs), 1)
            cloned_run = cloned_runs[0]
            cloned_steer_id = str(cloned_run.get("id", ""))
            self.assertTrue(cloned_steer_id)
            self.assertNotEqual(cloned_steer_id, source_steer_id)
            self.assertEqual(
                str(cloned_run.get("status", "")),
                SteerRunStatus.WAITING.value,
            )
            self.assertEqual(
                str(cloned_run.get("content", "")),
                "carry this steer into cloned context",
            )
            self.assertEqual(str(cloned_run.get("source_agent_id", "")), "user")
            self.assertEqual(str(cloned_run.get("source_agent_name", "")), "user")

            source_run = orchestrator.storage.load_steer_run(source_steer_id)
            self.assertIsNotNone(source_run)
            assert source_run is not None
            self.assertEqual(str(source_run.get("status", "")), SteerRunStatus.WAITING.value)

            loaded_events = orchestrator.load_session_events(loaded.id)
            steer_ids_in_loaded_events = [
                str((event.get("payload") or {}).get("steer_run_id", ""))
                for event in loaded_events
                if str(event.get("event_type", "")) == "steer_run_submitted"
            ]
            self.assertIn(cloned_steer_id, steer_ids_in_loaded_events)
            self.assertNotIn(source_steer_id, steer_ids_in_loaded_events)
