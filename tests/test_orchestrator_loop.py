from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.models import AgentNode, AgentRole, AgentStatus, RunSession, SessionStatus
from opencompany.orchestrator import Orchestrator
from opencompany.storage import Storage
from opencompany.workspace import WorkspaceManager
from test_orchestrator import (
    MultiRootConcurrencyLLMClient,
    RecordingLLMClient,
    RootConcurrencyLLMClient,
    RoutedLLMClient,
    SiblingWorkerConcurrencyLLMClient,
    WorkerSemaphoreLLMClient,
    WorkerConcurrencyLLMClient,
    build_test_project,
    tool_call_result,
)


class OrchestratorLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_worker_finalize_flow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = RoutedLLMClient(
                {
                    "root": [
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "spawn_agent",
                                        "name": "Inspect",
                                        "instruction": "Inspect the repository",
                                        "blocking": True,
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "wait_time",
                                        "seconds": 10,
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "finish",
                                        "status": "completed",
                                        "summary": "All work completed.",
                                    }
                                ]
                            }
                        ),
                    ],
                    "worker:Inspect the repository": [
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "finish",
                                        "status": "completed",
                                        "summary": "Inspection finished",
                                        "next_recommendation": "Finalize",
                                    }
                                ]
                            }
                        )
                    ],
                }
            )
            session = await orchestrator.run_task(
                "Inspect this project",
                workspace_mode="staged",
            )
            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "All work completed.")
            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            agents = storage.load_agents(session.id)
            self.assertEqual(len(agents), 2)
            self.assertTrue(any(agent["diff_artifact"] for agent in agents if agent["role"] == "worker"))

    async def test_spawn_agent_never_auto_injects_child_summary(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            client = RoutedLLMClient(
                {
                    "root": [
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "spawn_agent",
                                        "name": "Inspect",
                                        "instruction": "Inspect the repository",
                                        "blocking": True,
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "wait_time",
                                        "seconds": 10,
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "finish",
                                        "status": "completed",
                                        "summary": "Done.",
                                    }
                                ]
                            }
                        ),
                    ],
                    "worker:Inspect the repository": [
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "finish",
                                        "status": "completed",
                                        "summary": "Inspection finished.",
                                        "next_recommendation": "Report to root.",
                                    }
                                ]
                            }
                        )
                    ],
                }
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect this project")
            self.assertEqual(session.status.value, "completed")

            root_requests = [
                request
                for route, request in zip(client.calls, client.requests)
                if route == "root"
            ]
            self.assertGreaterEqual(len(root_requests), 2)
            messages = root_requests[1]["messages"]
            assert isinstance(messages, list)
            self.assertFalse(
                any(
                    "Completed child summaries." in str(message.get("content", ""))
                    for message in messages
                    if isinstance(message, dict)
                )
            )

    async def test_spawn_agent_rejects_legacy_inject_child_summary_argument(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            client = RoutedLLMClient(
                {
                    "root": [
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "spawn_agent",
                                        "name": "Inspect",
                                        "instruction": "Inspect the repository",
                                        "inject_child_summary": False,
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "finish",
                                        "status": "completed",
                                        "summary": "Done.",
                                    }
                                ]
                            }
                        ),
                    ],
                }
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect this project")
            self.assertEqual(session.status.value, "completed")

            root_requests = [
                request
                for route, request in zip(client.calls, client.requests)
                if route == "root"
            ]
            self.assertGreaterEqual(len(root_requests), 2)
            self.assertTrue(
                any(
                    "inject_child_summary" in str(message.get("content", ""))
                    for request in root_requests[1:]
                    for message in request.get("messages", [])
                    if isinstance(message, dict)
                )
            )

    async def test_root_can_continue_before_explicit_wait_for_children(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            client = RootConcurrencyLLMClient()
            orchestrator.llm_client = client

            session = await asyncio.wait_for(
                orchestrator.run_task("Verify root concurrency"),
                timeout=2,
            )

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(
                session.final_summary,
                "Root continued while the child was running.",
            )
            self.assertGreaterEqual(client.root_calls, 3)
            self.assertEqual(client.worker_calls, 1)

    async def test_worker_can_continue_before_explicit_wait_for_children(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = WorkerConcurrencyLLMClient()
            orchestrator._worker_semaphore = asyncio.Semaphore(
                max(1, orchestrator.config.runtime.limits.max_active_agents)
            )
            orchestrator._active_worker_tasks = {}
            orchestrator._background_worker_failures = []

            session_dir = orchestrator.paths.session_dir("session-worker-concurrency")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            parent_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-parent")
            session = RunSession(
                id="session-worker-concurrency",
                project_dir=project_dir,
                task="Parent worker task",
                locale="en",
                root_agent_id="agent-root",
                status=SessionStatus.RUNNING,
                created_at="2026-03-09T00:00:00+00:00",
                updated_at="2026-03-09T00:00:00+00:00",
                config_snapshot={},
            )
            parent = AgentNode(
                id="agent-parent",
                session_id=session.id,
                name="Parent worker",
                role=AgentRole.WORKER,
                instruction="Parent worker task",
                workspace_id=parent_workspace.id,
                parent_agent_id="agent-root",
            )
            parent.conversation = [
                {
                    "role": "user",
                    "content": orchestrator._worker_initial_message(
                        "Parent worker task",
                        parent_workspace.path,
                    ),
                }
            ]
            agents = {parent.id: parent}
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(parent)

            await asyncio.wait_for(
                orchestrator._run_worker(parent, agents, session, workspace_manager, 0),
                timeout=2,
            )

            self.assertEqual(parent.status, AgentStatus.COMPLETED)
            self.assertEqual(
                parent.summary,
                "Parent worker continued before waiting for its child.",
            )
            child = next(agent for agent in agents.values() if agent.id != parent.id)
            self.assertEqual(child.status, AgentStatus.COMPLETED)
            self.assertEqual(child.summary, "Nested child finished.")

    async def test_sibling_workers_do_not_wait_for_slowest_worker(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.limits.max_root_steps = 10
            client = SiblingWorkerConcurrencyLLMClient()
            orchestrator.llm_client = client

            session = await asyncio.wait_for(
                orchestrator.run_task("Verify sibling worker concurrency"),
                timeout=2,
            )

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(
                session.final_summary,
                "Fast sibling finished while slow sibling was still running.",
            )
            self.assertEqual(client.completion_order, ["fast", "slow"])

    async def test_multiple_roots_are_not_serialized_behind_slowest_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = MultiRootConcurrencyLLMClient()

            session_dir = orchestrator.paths.session_dir("session-multi-root-concurrency")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            now = "2026-03-09T00:00:00+00:00"
            session = RunSession(
                id="session-multi-root-concurrency",
                project_dir=project_dir,
                task="Verify multi-root concurrency",
                locale="en",
                root_agent_id="agent-root-slow",
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
            slow_root = AgentNode(
                id="agent-root-slow",
                session_id=session.id,
                name="Root Slow",
                role=AgentRole.ROOT,
                instruction="Slow root task",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
            )
            fast_root = AgentNode(
                id="agent-root-fast",
                session_id=session.id,
                name="Root Fast",
                role=AgentRole.ROOT,
                instruction="Fast root task",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
            )
            slow_root.conversation = [
                {
                    "role": "user",
                    "content": orchestrator._with_agent_identity_prompt(
                        agent_name=slow_root.name,
                        agent_id=slow_root.id,
                        content=orchestrator._root_initial_message(slow_root.instruction),
                    ),
                }
            ]
            fast_root.conversation = [
                {
                    "role": "user",
                    "content": orchestrator._with_agent_identity_prompt(
                        agent_name=fast_root.name,
                        agent_id=fast_root.id,
                        content=orchestrator._root_initial_message(fast_root.instruction),
                    ),
                }
            ]
            agents = {
                slow_root.id: slow_root,
                fast_root.id: fast_root,
            }
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(slow_root)
            orchestrator.storage.upsert_agent(fast_root)
            orchestrator._sync_agent_messages(slow_root)
            orchestrator._sync_agent_messages(fast_root)

            await asyncio.wait_for(
                orchestrator._run_session(
                    session=session,
                    agents=agents,
                    workspace_manager=workspace_manager,
                    pending_agent_ids=[],
                    root_loop=0,
                ),
                timeout=2,
            )

            self.assertEqual(session.status, SessionStatus.COMPLETED)
            self.assertEqual(session.final_summary, "Root Slow finished after Root Fast.")
            self.assertEqual(slow_root.status, AgentStatus.COMPLETED)
            self.assertEqual(fast_root.status, AgentStatus.COMPLETED)

    async def test_max_active_agents_limit_blocks_second_worker_until_first_finishes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.limits.max_active_agents = 1
            orchestrator.config.runtime.limits.max_root_steps = 10
            client = WorkerSemaphoreLLMClient()
            orchestrator.llm_client = client

            session = await asyncio.wait_for(
                orchestrator.run_task("Verify worker semaphore limit"),
                timeout=2,
            )

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Worker semaphore limit was enforced.")
            self.assertEqual(client.completion_order, ["one", "two"])

    async def test_root_uses_forced_summary_step_after_reaching_step_limit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.limits.max_agent_steps = 1
            client = RecordingLLMClient(
                [
                    json.dumps({"actions": [{"type": "list_agent_runs", "path": "."}]}),
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "partial",
                                    "summary": "Current repository structure was inspected.",
                                }
                            ]
                        }
                    ),
                ]
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect the repository and summarize")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Current repository structure was inspected.")
            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            agents = storage.load_agents(session.id)
            root = next(agent for agent in agents if agent["role"] == "root")
            self.assertEqual(root["step_count"], 2)
            self.assertEqual(len(client.calls), 2)
            events = storage.load_events(session.id)
            root_step_limit_controls = [
                json.loads(event["payload_json"])
                for event in events
                if event["event_type"] == "control_message"
                and json.loads(event["payload_json"]).get("kind") == "step_limit_summary"
                and event["agent_id"] == root["id"]
            ]
            self.assertEqual(root_step_limit_controls, [])

    async def test_root_soft_limit_uses_step_count_and_injects_reminder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.limits.max_root_steps = 1
            orchestrator.config.runtime.limits.root_soft_limit_reminder_interval = 1
            client = RecordingLLMClient(
                [
                    json.dumps({"actions": [{"type": "wait_time", "seconds": 10}]}),
                    json.dumps({"actions": [{"type": "wait_time", "seconds": 10}]}),
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "partial",
                                    "summary": "Root closed after reminders.",
                                }
                            ]
                        }
                    ),
                ]
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect the repository and summarize")
            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Root closed after reminders.")

            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            events = storage.load_events(session.id)
            reminder_events: list[dict[str, object]] = []
            for event in events:
                if event["event_type"] != "control_message":
                    continue
                payload = json.loads(event["payload_json"])
                if payload.get("kind") == "root_loop_force_finalize":
                    reminder_events.append(payload)
            self.assertGreaterEqual(len(reminder_events), 1)
            agents = storage.load_agents(session.id)
            root = next(agent for agent in agents if agent["role"] == "root")
            metadata = json.loads(str(root.get("metadata_json") or "{}"))
            self.assertGreaterEqual(
                len(metadata.get("compression_excluded_message_indices", [])),
                1,
            )

    async def test_root_soft_limit_reminder_interval_is_respected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.limits.max_root_steps = 1
            orchestrator.config.runtime.limits.root_soft_limit_reminder_interval = 2
            client = RecordingLLMClient(
                [
                    json.dumps({"actions": [{"type": "wait_time", "seconds": 10}]}),
                    json.dumps({"actions": [{"type": "wait_time", "seconds": 10}]}),
                    json.dumps({"actions": [{"type": "wait_time", "seconds": 10}]}),
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "partial",
                                    "summary": "Root closed after interval reminders.",
                                }
                            ]
                        }
                    ),
                ]
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect the repository and summarize")
            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Root closed after interval reminders.")

            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            events = storage.load_events(session.id)
            reminder_count = 0
            for event in events:
                if event["event_type"] != "control_message":
                    continue
                payload = json.loads(event["payload_json"])
                if payload.get("kind") == "root_loop_force_finalize":
                    reminder_count += 1
            self.assertEqual(reminder_count, 2)

    async def test_worker_soft_step_threshold_allows_additional_turns(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.limits.max_agent_steps = 1
            orchestrator.config.runtime.limits.worker_soft_limit_reminder_interval = 2
            client = RoutedLLMClient(
                {
                    "root": [
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "spawn_agent",
                                        "name": "Inspect",
                                        "instruction": "Inspect the repository",
                                        "blocking": True,
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "wait_time",
                                        "seconds": 10,
                                    },
                                    {
                                        "type": "finish",
                                        "status": "partial",
                                        "summary": "Worker summary captured after soft threshold reminders.",
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "wait_time",
                                        "seconds": 10,
                                    },
                                    {
                                        "type": "finish",
                                        "status": "partial",
                                        "summary": "Worker summary captured after soft threshold reminders.",
                                    }
                                ]
                            }
                        ),
                    ],
                    "worker:Inspect the repository": [
                        json.dumps({"actions": [{"type": "list_agent_runs", "path": "."}]}),
                        json.dumps({"actions": [{"type": "wait_time", "seconds": 10}]}),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "finish",
                                        "status": "partial",
                                        "summary": "Inspected the workspace and summarized the current state.",
                                        "next_recommendation": "Resume from the summary if more detail is needed.",
                                    }
                                ]
                            }
                        ),
                    ],
                }
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect the repository")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(
                session.final_summary,
                "Worker summary captured after soft threshold reminders.",
            )
            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            agents = storage.load_agents(session.id)
            worker = next(agent for agent in agents if agent["role"] == "worker")
            self.assertEqual(worker["summary"], "Inspected the workspace and summarized the current state.")
            self.assertEqual(worker["step_count"], 3)
            self.assertGreaterEqual(client.calls.count("root"), 2)

            events = storage.load_events(session.id)
            reminder_count = 0
            for event in events:
                if event["event_type"] != "control_message":
                    continue
                payload = json.loads(event["payload_json"])
                if payload.get("kind") == "worker_soft_limit_reminder":
                    reminder_count += 1
            self.assertEqual(reminder_count, 1)
            metadata = json.loads(str(worker.get("metadata_json") or "{}"))
            self.assertGreaterEqual(
                len(metadata.get("compression_excluded_message_indices", [])),
                1,
            )

    async def test_root_worker_finalize_flow_with_tool_calls(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = RoutedLLMClient(
                {
                    "root": [
                        tool_call_result(
                            "spawn_agent",
                            {"name": "Inspect", "instruction": "Inspect the repository", "blocking": True},
                            "call-root-spawn",
                        ),
                        tool_call_result(
                            "wait",
                            {
                                "seconds": 10,
                            },
                            "call-root-wait",
                        ),
                        tool_call_result(
                            "finish",
                            {
                                "status": "completed",
                                "summary": "All work completed.",
                            },
                            "call-root-finish",
                        ),
                    ],
                    "worker:Inspect the repository": [
                        tool_call_result(
                            "finish",
                            {
                                "status": "completed",
                                "summary": "Inspection finished",
                                "next_recommendation": "Finalize",
                            },
                            "call-worker-finish",
                        )
                    ],
                }
            )

            session = await orchestrator.run_task("Inspect this project")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "All work completed.")

    async def test_root_protocol_fallback_wait_is_suppressed_without_pending_children(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            client = RecordingLLMClient(
                [
                    "",
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "partial",
                                    "summary": "Recovered after protocol error.",
                                }
                            ]
                        }
                    ),
                ]
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect this project")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Recovered after protocol error.")
            self.assertEqual(len(client.calls), 2)

            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            agents = storage.load_agents(session.id)
            root = next(agent for agent in agents if agent["role"] == "root")
            self.assertEqual(root["step_count"], 1)

            events = storage.load_events(session.id)
            protocol_errors = [
                event for event in events if event["event_type"] == "protocol_error"
            ]
            self.assertEqual(len(protocol_errors), 1)
            llm_retries = [
                event for event in events if event["event_type"] == "llm_retry"
            ]
            self.assertEqual(len(llm_retries), 1)
            llm_retry_payload = json.loads(llm_retries[0]["payload_json"])
            self.assertEqual(
                str(llm_retry_payload.get("retry_reason", "")),
                "empty_protocol_response",
            )
            self.assertEqual(int(llm_retry_payload.get("overall_retry_attempt", -1)), 1)
            self.assertEqual(
                str(llm_retry_payload.get("overall_retry_category", "")),
                "empty_protocol",
            )
            wait_tool_calls = []
            for event in events:
                if event["event_type"] != "tool_call" or event["phase"] != "tool":
                    continue
                payload = json.loads(event["payload_json"])
                if payload.get("action", {}).get("type") == "list_tool_runs":
                    wait_tool_calls.append(payload)
            self.assertEqual(wait_tool_calls, [])

    async def test_worker_protocol_fallback_uses_progress_review_recommendation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = RoutedLLMClient(
                {
                    "root": [
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "spawn_agent",
                                        "name": "Inspect",
                                        "instruction": "Inspect the repository",
                                        "blocking": True,
                                    }
                                ]
                            }
                        ),
                        json.dumps(
                            {
                                "actions": [
                                    {
                                        "type": "finish",
                                        "status": "partial",
                                        "summary": "Root finalized after worker protocol fallback.",
                                    }
                                ]
                            }
                        ),
                    ],
                    "worker:Inspect the repository": [
                        "This is a plain text response and not a valid protocol action payload."
                    ],
                }
            )

            session = await orchestrator.run_task("Inspect this project")
            self.assertEqual(session.status.value, "completed")
            self.assertEqual(
                session.final_summary,
                "Root finalized after worker protocol fallback.",
            )

            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            agents = storage.load_agents(session.id)
            worker = next(agent for agent in agents if agent["role"] == "worker")
            self.assertEqual(worker["status"], "failed")
            self.assertEqual(
                worker["summary"],
                "The agent produced an invalid protocol response.",
            )
            self.assertEqual(
                worker["next_recommendation"],
                "Review this agent's progress first, then plan and take the next steps.",
            )

    async def test_root_protocol_fallback_exhausted_empty_responses_does_not_crash_runtime(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            client = RecordingLLMClient(["", ""])
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect this project")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.completion_state, "partial")
            self.assertEqual(session.final_summary, "The agent produced an invalid protocol response.")
            self.assertEqual(len(client.calls), 2)

            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            events = storage.load_events(session.id)
            protocol_errors = [
                event for event in events if event["event_type"] == "protocol_error"
            ]
            self.assertEqual(len(protocol_errors), 2)
            llm_retries = [
                event for event in events if event["event_type"] == "llm_retry"
            ]
            self.assertEqual(len(llm_retries), 1)
            llm_retry_payload = json.loads(llm_retries[0]["payload_json"])
            self.assertEqual(
                str(llm_retry_payload.get("retry_reason", "")),
                "empty_protocol_response",
            )
            self.assertEqual(int(llm_retry_payload.get("overall_retry_attempt", -1)), 1)
            self.assertEqual(
                str(llm_retry_payload.get("overall_retry_category", "")),
                "empty_protocol",
            )
            session_failed = [
                event for event in events if event["event_type"] == "session_failed"
            ]
            self.assertEqual(session_failed, [])
            invalid_response_controls = []
            for event in events:
                if event["event_type"] != "control_message":
                    continue
                payload = json.loads(event["payload_json"])
                if payload.get("kind") == "invalid_response":
                    invalid_response_controls.append(payload)
            self.assertEqual(len(invalid_response_controls), 1)
